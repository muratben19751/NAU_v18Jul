"""Robustness analiz endpoint'leri — WFO, Monte Carlo, In/Out-of-Sample.

POST /robustness/run   → daemon thread başlat, progress fragment dön
GET  /robustness/progress/{run_id} → poll → sonuç gelince result.html
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/robustness")

from web.shared import ProgressStore  # noqa: E402
from web.shared import log_robustness as _log_robustness  # noqa: E402

# ProgressStore holds dict + lock + capped eviction; aliases keep existing
# direct-access sites unchanged. Log writer/path now live in web.shared.
_STORE = ProgressStore(20)  # terkedilmiş run'ları sınırla (#21)
_PROGRESS = _STORE.raw()
_LOCK = _STORE.lock


def _add_step(run_id: str, msg: str) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _LOCK:
        s = _PROGRESS.get(run_id)
        if s:
            s["steps"].append({"ts": ts, "msg": msg})


# _log_robustness now lives in web.shared (imported above as a re-export alias)
# — single source of truth; the robustness log path + write helper were
# duplicated / cross-imported with backtest.py and reports.py.


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    spec_id: str = Form(...),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("1"),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    train_months: int = Form(3),
    test_months: int = Form(1),
    n_sims: int = Form(300),
    split_pct: float = Form(0.7),
    n_optimize: int = Form(20),
    objective: str = Form("sharpe"),
):
    from composer import load_catalog
    from server import templates

    # L6: HTML input'un min/max'ı yalnız tarayıcı tarafında — doğrudan POST
    # (curl/otomasyon) sınır dışı değer geçirebiliyordu; [0.5, 0.9]'a clamp.
    split_pct = max(0.5, min(0.9, split_pct))
    # L6 (kapsam): aynı biçimde diğer sayısal alanları da sunucu tarafında
    # HTML min/max sınırlarına clamp'le — 0/negatif ay WFO penceresini bozar,
    # aşırı n_sims/n_optimize kaynağı israf eder.
    train_months = max(1, min(24, train_months))
    test_months = max(1, min(12, test_months))
    n_sims = max(50, min(1000, n_sims))
    n_optimize = max(1, min(200, n_optimize))

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Spec not found.</div>", status_code=404
        )

    run_id = uuid.uuid4().hex[:8]
    # done-first eviction (#21: dropping a still-running run caused KeyError in
    # the worker's unguarded writes + a permanent 'Unknown run ID' on poll).
    _STORE.create_evicting(
        run_id,
        {
            "steps": [],
            "done": False,
            "result": None,
            "error": None,
            "spec_name": spec.name,
        },
    )

    def _worker():
        from datetime import timedelta

        import pandas as _pd

        from data import _bybit_cache_path, load_bybit_bars

        try:
            _add_step(run_id, f"Başlatılıyor · {spec.name}")

            # ── Veri yükle ────────────────────────────────────────────────
            cache_path = _bybit_cache_path(category, symbol, interval)
            if cache_path.exists():
                df_check = _pd.read_parquet(cache_path)
                cache_end = (
                    df_check.index[-1].to_pydatetime().replace(tzinfo=UTC)
                    if not df_check.empty
                    else None
                )
            else:
                cache_end = None

            now = datetime.now(UTC)
            start_dt = (
                datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                if bybit_start
                else now - timedelta(days=30 * (train_months + test_months * 3))
            )
            end_dt = (
                datetime.fromisoformat(bybit_end).replace(
                    hour=23, minute=59, second=59, tzinfo=UTC
                )
                if bybit_end
                else (cache_end or now)
            )

            _add_step(
                run_id,
                f"Veri okunuyor · {symbol}/{category}/{interval} "
                f"{start_dt.date()} → {end_dt.date()}",
            )
            bars = load_bybit_bars(
                symbol=symbol,
                interval=interval,
                category=category,
                start=start_dt,
                end=end_dt,
            )
            if bars.empty:
                with _LOCK:
                    _PROGRESS[run_id]["error"] = (
                        "Veri bulunamadı. Data ekranından fetch edin."
                    )
                return
            _add_step(run_id, f"{len(bars):,} bar yüklendi")

            # ── Suite: killable child'da (WFO + IS/OOS + tam backtest + MC) ──
            # Daha önce bu suite bu daemon thread'de HAM koşuyordu; Nautilus
            # backtest'leri GIL'i tuttuğu için sunucunun event loop'u donuyordu
            # (agent'ta düzeltilen bug'ın kopyası). Artık sandbox child'ında.
            from sandbox import run_manual_suite_guarded

            suite = run_manual_suite_guarded(
                spec,
                bars,
                {"symbol": symbol, "interval": interval, "category": category},
                {
                    "train_months": train_months,
                    "test_months": test_months,
                    "n_optimize": n_optimize,
                    "objective": objective,
                    "split_pct": split_pct,
                    "n_sims": n_sims,
                },
                progress_fn=lambda m: _add_step(run_id, m),
            )
            if suite.get("error") and "wfo_windows" not in suite:
                with _LOCK:
                    _PROGRESS[run_id]["error"] = suite["error"]
                return

            result = {
                "spec_name": spec.name,
                "symbol": symbol,
                "category": category,
                "interval": interval,
                "start_date": str(start_dt.date()),
                "end_date": str(end_dt.date()),
                "n_bars": len(bars),
                "wfo_windows": suite.get("wfo_windows") or [],
                "wfo_summary": suite.get("wfo_summary") or {},
                "split": suite.get("split") or {},
                "mc": suite.get("mc") or {"error": "Trade verisi yok."},
                "train_months": train_months,
                "test_months": test_months,
                "n_sims": n_sims,
                "n_optimize": n_optimize,
                "objective": objective,
            }
            _add_step(run_id, "Tamamlandı")
            _log_robustness(spec.id, spec.name, result)
            with _LOCK:
                _PROGRESS[run_id]["result"] = result

        except Exception as e:
            with _LOCK:
                _PROGRESS[run_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _LOCK:
                if run_id in _PROGRESS:
                    _PROGRESS[run_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()

    return templates.TemplateResponse(
        request,
        "fragments/robustness_progress.html",
        {"run_id": run_id, "done": False, "error": None, "steps": []},
    )


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    from server import templates

    with _LOCK:
        raw = _PROGRESS.get(run_id)
        if raw is None:
            return HTMLResponse("<div class='empty-state'>Bilinmeyen run ID.</div>")
        state = {
            "done": raw["done"],
            "result": raw["result"],
            "error": raw["error"],
            "steps": list(raw["steps"]),
            "spec_name": raw["spec_name"],
        }

    if state["done"] and state["result"]:
        with _LOCK:
            _PROGRESS.pop(run_id, None)
        return templates.TemplateResponse(
            request,
            "fragments/robustness_result.html",
            {"r": state["result"]},
        )

    if state["done"] and state["error"]:
        with _LOCK:
            _PROGRESS.pop(run_id, None)
        return templates.TemplateResponse(
            request,
            "fragments/robustness_progress.html",
            {
                "run_id": run_id,
                "done": True,
                "error": state["error"],
                "steps": state["steps"],
            },
        )

    return templates.TemplateResponse(
        request,
        "fragments/robustness_progress.html",
        {"run_id": run_id, "done": False, "error": None, "steps": state["steps"]},
    )
