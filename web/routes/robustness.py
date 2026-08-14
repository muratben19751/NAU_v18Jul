"""Robustness analysis endpoints — WFO, Monte Carlo, In/Out-of-Sample.

POST /robustness/run   → start daemon thread, return progress fragment
GET  /robustness/progress/{run_id} → poll → return result.html when ready

A suite can succeed PARTIALLY: WFO + IS/OOS may be real while the full
backtest that feeds Monte Carlo crashed. That case carries `full_error` from
``sandbox._manual_suite_child`` and must be reported as degraded — in the log,
in the step list, in the result dict and on screen. Presenting it as "no trade
data" told the user their strategy was useless when the truth was that the
infrastructure had failed (2026-08-11 DeepR finding).

Wiki References
---------------
See: [[crash_only_design]]
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/robustness")

from web.shared import (
    ProgressStore,  # noqa: E402
    SessionRunGuard,  # noqa: E402
    error_html,  # noqa: E402
    session_id,  # noqa: E402
)
from web.shared import log_robustness as _log_robustness  # noqa: E402
from web.templating import templates

# ProgressStore holds dict + lock + capped eviction; aliases keep existing
# direct-access sites unchanged. Log writer/path now live in web.shared.
_STORE = ProgressStore(20)  # limit abandoned runs (#21)
_PROGRESS = _STORE.raw()
_LOCK = _STORE.lock

# Per-session single-active-run guard — only client-side protection existed
# before (backtest_scripts.html's querySelector check), so a direct POST
# (curl/automation/two tabs) could start multiple concurrent sandbox
# WFO/Monte Carlo child processes for the same session. Üç kopyanın ortak
# sınıfı `web.shared.SessionRunGuard` (DeepR 2026-08-11 [DÜŞÜK]).
_ROBUSTNESS_GUARD = SessionRunGuard(lambda _kind: _STORE)
_ACTIVE_ROBUSTNESS_RUNS = _ROBUSTNESS_GUARD.raw()  # geriye dönük ad
_ACTIVE_ROBUSTNESS_RUNS_LOCK = _ROBUSTNESS_GUARD.lock


def _session_robustness_busy(sid: str) -> bool:
    return _ROBUSTNESS_GUARD.busy(sid)


def _session_robustness_set_active(sid: str, rid: str) -> None:
    _ROBUSTNESS_GUARD.set_active(sid, rid)


def _add_step(run_id: str, msg: str) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _LOCK:
        s = _PROGRESS.get(run_id)
        if s:
            s["steps"].append({"ts": ts, "msg": msg})


def _set_progress(run_id: str, **fields) -> None:
    """Update a progress entry if it still exists.

    `create_evicting` drops the oldest entry when the store is full, including
    one that is still running. Subscript assignment on an evicted run_id raised
    KeyError inside the worker — and the handler that was supposed to record the
    failure raised the same KeyError again, killing the daemon thread and
    orphaning its child process. A vanished entry means nobody is watching, so
    dropping the update is the whole correct response.
    """
    with _LOCK:
        s = _PROGRESS.get(run_id)
        if s is not None:
            s.update(fields)


# _log_robustness now lives in web.shared (imported above as a re-export alias)
# — single source of truth; the robustness log path + write helper were
# duplicated / cross-imported with backtest.py and reports.py.


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    # Form("") — Form(...) DEĞİL: #spec-picker'ın varsayılan seçeneği boş
    # gönderir ve FastAPI zorunlu bir Form alanında boş dizeyi "eksik" sayıp
    # 422 JSON döner. O JSON kullanıcıya hiçbir şey anlatmaz; boş seçimi
    # rotanın kendisi karşılasın ki ne yapılacağını yazan bir mesaj çıksın.
    spec_id: str = Form(""),
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

    sid = session_id(request)
    if _session_robustness_busy(sid):
        resp = HTMLResponse(
            "<div class='empty-state'>A robustness run is already in progress "
            "for this session — wait for it to finish.</div>",
            status_code=409,
        )
        resp.headers["HX-Toast"] = (
            "err|A robustness run is already running - wait for it to finish."
        )
        return resp

    # L6: the HTML input's min/max is only browser-side — a direct POST
    # (curl/automation) could pass out-of-range values; clamp to [0.5, 0.9].
    split_pct = max(0.5, min(0.9, split_pct))
    # L6 (scope): likewise clamp the other numeric fields server-side to the
    # HTML min/max bounds — a 0/negative month breaks the WFO window,
    # excessive n_sims/n_optimize wastes resources.
    train_months = max(1, min(24, train_months))
    test_months = max(1, min(12, test_months))
    n_sims = max(50, min(1000, n_sims))
    n_optimize = max(1, min(200, n_optimize))

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        # DeepR 2026-08-11 [ORTA]: bu 404 HX-Toast'suzdu ve htmx 4xx'i swap
        # etmediği için ekranda HİÇBİR ŞEY olmuyordu. Formun spec_id'si
        # #result panelinden JS ile doldurulur (btFillPanels); panel yoksa
        # ya da spec silinmişse boş gider — kullanıcıya bunu söyle.
        return error_html(
            "No strategy selected for the reliability test: run a backtest "
            "first, then press this button (or reload the page if the "
            "strategy was deleted in another tab).",
            404,
            toast=True,
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
    _session_robustness_set_active(sid, run_id)

    def _worker():
        from datetime import timedelta

        import pandas as _pd

        from data import _bybit_cache_path, load_bybit_bars

        try:
            _add_step(run_id, f"Starting · {spec.name}")

            # ── Load data ─────────────────────────────────────────────────
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
                f"Reading data · {symbol}/{category}/{interval} "
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
                _set_progress(
                    run_id, error="Data not found. Fetch it from the Data screen."
                )
                return
            _add_step(run_id, f"{len(bars):,} candles loaded")

            # ── Suite: in killable child (WFO + IS/OOS + full backtest + MC) ──
            # Previously this suite ran RAW in this daemon thread; because
            # Nautilus backtests hold the GIL, the server's event loop froze
            # (a copy of the bug fixed in the agent). Now in a sandbox child.
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
                _set_progress(run_id, error=suite["error"])
                return

            # DeepR 2026-08-11 [YÜKSEK]: `_manual_suite_child` has always
            # returned `full_error` (the Monte Carlo full backtest's failure)
            # as part of its documented contract, and NOBODY read it. The run
            # still had WFO + IS/OOS tables, so the `suite["error"]` branch
            # above didn't fire either, and the crash surfaced as a bland
            # "no trade data" in the Monte Carlo tab. A degraded run must be
            # reported as degraded — in the log, in the step list, and on
            # screen (see robustness_result.html's Monte Carlo tab).
            full_error = (suite.get("full_error") or "").strip()
            if full_error:
                logging.getLogger(__name__).warning(
                    "robustness %s (%s %s/%s/%s): full backtest for Monte Carlo "
                    "FAILED — %s",
                    run_id,
                    spec.name,
                    symbol,
                    category,
                    interval,
                    full_error,
                )
                _add_step(
                    run_id,
                    f"⚠ Monte Carlo skipped — the full backtest failed: {full_error}",
                )

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
                "mc": suite.get("mc") or {"error": "No Trade data."},
                # Carried into the result so the template (and the persisted
                # robustness log) can tell "0 trades" apart from "it crashed".
                "full_error": full_error,
                "train_months": train_months,
                "test_months": test_months,
                "n_sims": n_sims,
                "n_optimize": n_optimize,
                "objective": objective,
            }
            _add_step(
                run_id, "Completed (partially degraded)" if full_error else "Completed"
            )
            _log_robustness(spec.id, spec.name, result)
            _set_progress(run_id, result=result)

        except Exception as e:
            _set_progress(run_id, error=f"{type(e).__name__}: {e}")
        finally:
            _set_progress(run_id, done=True)

    threading.Thread(target=_worker, daemon=True).start()

    return templates.TemplateResponse(
        request,
        "fragments/robustness_progress.html",
        {"run_id": run_id, "done": False, "error": None, "steps": []},
    )


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):

    with _LOCK:
        raw = _PROGRESS.get(run_id)
        if raw is None:
            return HTMLResponse("<div class='empty-state'>Unknown run ID.</div>")
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
