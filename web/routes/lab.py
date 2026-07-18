"""Strategy Lab — Otonom strateji üretici.

Tek tıkla: Claude bir strateji fikri üretir → custom signal block'lar yaratır →
blokları birleştirir → backtest çalıştırır → KPI + equity curve gösterir.

Endpoints:
    GET  /lab                    Sayfa
    POST /lab/run                Otonom pipeline'ı başlat (hemen döner)
    GET  /lab/progress/{run_id}  Durum polling (HTMX every 1s)

Wiki References
---------------
Bkz: [[strategy_and_actor]], [[order_flow_pipeline]], [[backtesting_guide]]
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/lab")

from web.shared import ProgressStore  # noqa: E402
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402

# Phase state per run. Separate from backtest.py's _RUN_PROGRESS so the two
# features don't interfere. ProgressStore holds dict+lock+capped eviction (M11:
# unbounded dict kept full IterationResults for runs whose tab closed).
_LAB_STORE = ProgressStore(50)
_LAB_PROGRESS = _LAB_STORE.raw()
_LAB_LOCK = _LAB_STORE.lock

_PHASES = [
    "Strateji fikri üretiliyor",
    "Entry block oluşturuluyor",
    "Exit block oluşturuluyor",
    "Bloklar kaydediliyor",
    "Strateji derleniyor",
    "Backtest çalışıyor",
]


def _set_phase(run_id: str, phase_idx: int, detail: str = "") -> None:
    """Mark phase as running and update detail text."""
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _LAB_LOCK:
        state = _LAB_PROGRESS.get(run_id)
        if state is None:
            return
        for i, p in enumerate(state["phases"]):
            if i < phase_idx:
                p["status"] = "done"
            elif i == phase_idx:
                p["status"] = "running"
                p["detail"] = detail
                p["ts"] = ts
            else:
                p["status"] = "pending"
                p["detail"] = ""


def _done_phase(run_id: str, phase_idx: int, detail: str = "") -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _LAB_LOCK:
        state = _LAB_PROGRESS.get(run_id)
        if state is None:
            return
        p = state["phases"][phase_idx]
        p["status"] = "done"
        p["detail"] = detail
        p["ts"] = ts


def _add_backtest_step(run_id: str, msg: str) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _LAB_LOCK:
        state = _LAB_PROGRESS.get(run_id)
        if state is not None:
            state["backtest_steps"].append({"ts": ts, "msg": msg})


def _lab_worker(
    run_id: str,
    hint: str,
    symbol: str,
    category: str,
    interval: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    from agent import GeneratedCodeError, propose_custom_block
    from composer import (
        ComposedStrategySpec,
        SignalBlock,
        new_spec_id,
        register_custom_from_disk,
    )
    from custom_block_store import is_valid_name, save_custom
    from data import load_bybit_bars

    try:
        # ── Data ──────────────────────────────────────────────────────────────
        cache_path = None
        try:
            from data import _bybit_cache_path

            cache_path = _bybit_cache_path(category, symbol, interval)
        except Exception:
            pass

        if cache_path is not None and cache_path.exists():
            import pandas as _pd

            cached = _pd.read_parquet(cache_path)
            cache_start = (
                cached.index[0].to_pydatetime().replace(tzinfo=UTC)
                if not cached.empty
                else None
            )
            cache_end = (
                cached.index[-1].to_pydatetime().replace(tzinfo=UTC)
                if not cached.empty
                else None
            )
        else:
            cache_start = cache_end = None

        end = end_date if end_date is not None else cache_end or datetime.now(UTC)
        start = (
            start_date
            if start_date is not None
            else cache_start or end - timedelta(days=7)
        )
        bars = load_bybit_bars(
            symbol=symbol, interval=interval, category=category, start=start, end=end
        )
        if bars.empty:
            with _LAB_LOCK:
                if run_id in _LAB_PROGRESS:
                    _LAB_PROGRESS[run_id]["error"] = (
                        f"{symbol}/{category}/{interval} için cache'de veri yok. "
                        "Önce /data ekranından fetch edin."
                    )
            return

        # ── Faz 0: Strateji fikri üret ─────────────────────────────────────
        _set_phase(run_id, 0, "Claude'dan fikir isteniyor…")
        idea = _generate_idea(hint)
        _done_phase(run_id, 0, f"Fikir: {idea['label']}")

        # ── Faz 1: Entry block ──────────────────────────────────────────────
        entry_name = f"lab_entry_{run_id}"
        _set_phase(run_id, 1, f"Entry block yazılıyor: {idea['entry_label']}…")
        try:
            entry_block = propose_custom_block(
                idea["entry_label"], idea["entry_desc"], role_hint="entry"
            )
        except GeneratedCodeError as e:
            raise RuntimeError(f"Entry block üretilemedi: {e}") from e
        entry_block["name"] = entry_name
        _done_phase(run_id, 1, f"✓ {entry_name}")

        # ── Faz 2: Exit block ───────────────────────────────────────────────
        exit_name = f"lab_exit_{run_id}"
        _set_phase(run_id, 2, f"Exit block yazılıyor: {idea['exit_label']}…")
        try:
            exit_block = propose_custom_block(
                idea["exit_label"], idea["exit_desc"], role_hint="exit"
            )
        except GeneratedCodeError:
            # Fallback: use ATR stop as exit
            exit_block = _atr_stop_fallback()
        exit_block["name"] = exit_name
        _done_phase(run_id, 2, f"✓ {exit_name}")

        # ── Faz 3: Kaydet ───────────────────────────────────────────────────
        _set_phase(run_id, 3, "Bloklar diske yazılıyor…")
        for blk_name, blk in [(entry_name, entry_block), (exit_name, exit_block)]:
            if not is_valid_name(blk_name):
                raise RuntimeError(f"Geçersiz blok adı: {blk_name}")
            save_custom(blk_name, blk["meta"], blk["code"], prompt=hint)
            register_custom_from_disk(blk_name)
        _done_phase(run_id, 3, "Bloklar Strategy Composer'da görünür")

        # ── Faz 4: Strateji derle ───────────────────────────────────────────
        _set_phase(run_id, 4, "Strateji derleniyor…")

        def _extract_params(blk: dict) -> dict:
            """LLM meta'dan param default'larını güvenli çıkar."""
            raw = blk["meta"].get("params") or {}
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[k] = v.get("default")
                else:
                    out[k] = v  # scalar default doğrudan
            return out

        entry_params = _extract_params(entry_block)
        exit_params = _extract_params(exit_block)
        spec = ComposedStrategySpec(
            id=new_spec_id(),
            name=idea["label"],
            description=idea["description"],
            blocks=[
                SignalBlock(type=entry_name, role="entry", params=entry_params),
                SignalBlock(type=exit_name, role="exit", params=exit_params),
            ],
            trade_size=0.01,
            allow_short=False,
            entry_logic="OR",
            exit_logic="OR",
        )
        err = spec.validate()
        if err:
            raise RuntimeError(f"Spec hatası: {err}")
        # M14: kilitsiz load→append→save, eşzamanlı koşuda son-yazan-kazanır
        # ile stratejiyi sessizce kaybettiriyordu — kilitli yardımcı kullan.
        from composer import append_to_catalog

        append_to_catalog(spec)
        with _LAB_LOCK:
            if run_id in _LAB_PROGRESS:
                _LAB_PROGRESS[run_id]["strategy_name"] = spec.name
        _done_phase(run_id, 4, f"✓ {spec.name} (Backtest seçicide görünür)")

        # ── Faz 5: Backtest ─────────────────────────────────────────────────
        _set_phase(run_id, 5, f"BacktestEngine çalışıyor: {spec.name}…")
        # Sandbox: her spec killable child'da — Nautilus backtest GIL'i tutar,
        # in-process koşarsa sunucunun event loop'u donar (agent bug'ı sınıfı).
        from sandbox import run_backtest_guarded

        result = run_backtest_guarded(
            spec,
            bars,
            recipe={"symbol": symbol, "category": category, "interval": interval},
            iteration_id=0,
            rationale=f"Strategy Lab · {spec.name}",
            progress_fn=lambda m: _add_backtest_step(run_id, m),
            force_subprocess=True,
        )
        _done_phase(
            run_id,
            5,
            (
                f"✓ {result.metrics.get('n_trades', '?')} trade · "
                f"PnL {result.metrics.get('pnl', 0):+.2f} USDT"
            )
            if not result.error
            else f"✗ {result.error}",
        )

        # Log to shared backtest_log.jsonl so /reports picks this up
        try:
            _log_backtest(
                spec,
                result,
                "Bybit",
                {"symbol": symbol, "category": category, "interval": interval},
            )
        except Exception:
            pass

        with _LAB_LOCK:
            if run_id in _LAB_PROGRESS:
                _LAB_PROGRESS[run_id]["result"] = result
                _LAB_PROGRESS[run_id]["bars_info"] = {
                    "symbol": symbol,
                    "category": category,
                    "interval": interval,
                }

    except Exception as e:
        with _LAB_LOCK:
            if run_id in _LAB_PROGRESS:
                _LAB_PROGRESS[run_id]["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _LAB_LOCK:
            if run_id in _LAB_PROGRESS:
                _LAB_PROGRESS[run_id]["done"] = True


def _generate_idea(hint: str) -> dict:
    """Ask Claude for a trading strategy idea, or expand the user's hint."""
    from agent import _get_client

    if hint.strip():
        prompt = (
            f"Kullanıcı şu strateji fikrini verdi: '{hint}'\n\n"
            "Bu fikri detaylandır ve aşağıdaki JSON formatında döndür:\n"
            '{"label": "kısa isim (max 40 kar)", '
            '"description": "1 cümle genel açıklama", '
            '"entry_label": "entry block için kısa isim", '
            '"entry_desc": "entry sinyalini tarif et (TAM OHLCV: closes + highs + lows + volumes hizalı serilerde çalışacak)", '
            '"exit_label": "exit block için kısa isim", '
            '"exit_desc": "exit sinyalini tarif et"}\n'
            "Sadece JSON döndür, başka bir şey yazma."
        )
    else:
        prompt = (
            "Bir kripto trading stratejisi fikri üret. "
            "Teknik indikatör tabanlı olsun; tam OHLCV veri (closes/highs/lows/volumes) "
            "kullanılabilir, ATR/ADX/Stochastic gibi high-low gerektiren indikatörler de geçerli. "
            "Aşağıdaki JSON formatında döndür:\n"
            '{"label": "kısa isim (max 40 kar)", '
            '"description": "1 cümle genel açıklama", '
            '"entry_label": "entry block için kısa isim", '
            '"entry_desc": "entry sinyalini tarif et (closes/highs/lows/volumes serilerinde çalışacak)", '
            '"exit_label": "exit block için kısa isim", '
            '"exit_desc": "exit sinyalini tarif et"}\n'
            "Sadece JSON döndür, başka bir şey yazma."
        )
    import json

    try:
        from agent import MODEL, _extract_json_object, _get_client

        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (
            resp.content[0].text.strip()
            if resp.content and hasattr(resp.content[0], "text")
            else ""
        )
        return json.loads(_extract_json_object(text))
    except Exception:
        # Fallback idea
        return {
            "label": "EMA Crossover Lab",
            "description": "EMA hızlı/yavaş kesişme sinyali.",
            "entry_label": "EMA Cross Entry",
            "entry_desc": "EMA9 yukarı EMA21'i kestiğinde long sinyali üret",
            "exit_label": "EMA Cross Exit",
            "exit_desc": "EMA9 aşağı EMA21'i kestiğinde exit sinyali üret",
        }


def _atr_stop_fallback() -> dict:
    """Return a minimal ATR-based exit block as fallback."""
    return {
        "name": "atr_exit_fallback",
        "meta": {
            "label": "ATR Exit (fallback)",
            "params": {
                "atr_period": {"type": "int", "min": 5, "max": 50, "default": 14},
                "multiplier": {"type": "float", "min": 0.5, "max": 5.0, "default": 2.0},
            },
            "help": "ATR tabanlı fallback çıkış.",
        },
        "code": (
            "def atr_approx(closes, period):\n"
            "    if len(closes) < period + 1:\n"
            "        return None\n"
            "    ranges = [abs(closes[i] - closes[i-1]) for i in range(-period, 0)]\n"
            "    return sum(ranges) / period\n\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    period = block.params.get('atr_period', 14)\n"
            "    mult = block.params.get('multiplier', 2.0)\n"
            "    if len(closes) < period + 2:\n"
            "        return None\n"
            "    atr = atr_approx(closes, period)\n"
            "    if atr is None:\n"
            "        return None\n"
            "    entry = state.get('entry_price')\n"
            "    if entry is None:\n"
            "        state['entry_price'] = closes[-1]\n"
            "        return None\n"
            "    if closes[-1] < entry - mult * atr:\n"
            "        state['entry_price'] = None\n"
            "        return 'exit'\n"
            "    return None\n"
        ),
    }


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    from server import get_market_info, templates

    return templates.TemplateResponse(
        request,
        "lab.html",
        {"active": "lab", "page_title": "Strategy Lab", "market": get_market_info()},
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    hint: str = Form(default=""),
    symbol: str = Form(default="BTCUSDT"),
    category: str = Form(default="linear"),
    interval: str = Form(default="1"),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
):
    from server import get_market_info, templates

    def _parse_date(s: str) -> datetime | None:
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except Exception:
            return None

    parsed_start = _parse_date(start_date)
    parsed_end = _parse_date(end_date)

    run_id = uuid.uuid4().hex[:8]
    # M11: done-first eviction (dropping a still-running run left the live panel
    # on a permanent 'Unknown run ID'; worker writes are 'in dict'-guarded so no
    # crash, just result loss). See ProgressStore.
    _LAB_STORE.create_evicting(
        run_id,
        {
            "phases": [
                {"n": i, "label": lbl, "status": "pending", "detail": "", "ts": ""}
                for i, lbl in enumerate(_PHASES)
            ],
            "backtest_steps": [],
            "done": False,
            "result": None,
            "error": None,
            "strategy_name": "",
            "hint": hint.strip(),
        },
    )

    threading.Thread(
        target=_lab_worker,
        args=(
            run_id,
            hint.strip(),
            symbol,
            category,
            interval,
            parsed_start,
            parsed_end,
        ),
        daemon=True,
    ).start()

    # progress() ile aynı koruma: worker thread başladıktan sonra canlı
    # _LAB_PROGRESS[run_id] referansını kilitsiz template'e geçirmek torn read
    # (ve backtest_steps iterasyonunda 'list changed size') riski taşır —
    # kilit altında snapshot al.
    with _LAB_LOCK:
        raw = _LAB_PROGRESS[run_id]
        initial_state = {
            "phases": [dict(p) for p in raw["phases"]],
            "backtest_steps": list(raw["backtest_steps"]),
            "done": raw["done"],
            "result": raw["result"],
            "error": raw["error"],
            "strategy_name": raw["strategy_name"],
            "hint": raw.get("hint", ""),
        }

    return templates.TemplateResponse(
        request,
        "fragments/lab_progress.html",
        {
            "run_id": run_id,
            "phases": _PHASES,
            "state": initial_state,
            "done": False,
            "error": None,
            "market": get_market_info(),
        },
    )


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    from server import get_market_info, templates
    from web.viewmodels import iteration_row

    with _LAB_LOCK:
        raw = _LAB_PROGRESS.get(run_id)
        if raw is None:
            return HTMLResponse("<div class='empty-state'>Bilinmeyen run ID.</div>")
        state = {
            # Deep copy phase dicts — lock bırakıldıktan sonra worker mutate etmesin
            "phases": [dict(p) for p in raw["phases"]],
            "backtest_steps": list(raw["backtest_steps"]),
            "done": raw["done"],
            "result": raw["result"],
            "error": raw["error"],
            "strategy_name": raw["strategy_name"],
            "bars_info": raw.get("bars_info", {}),
        }

    if state["done"] and state["result"] is not None:
        result = state["result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["strategy_name"]
        last_row["steps"] = state["backtest_steps"]
        last_row["narrative"] = _lab_narrative(last_row, state)

        # Chart URL — use symbol/category/interval from the worker
        bi = state.get("bars_info", {})
        if bi.get("symbol"):
            last_row["chart_url"] = _chart_url(bi)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi["category"]
            last_row["chart_interval"] = bi["interval"]

        with _LAB_LOCK:
            _LAB_PROGRESS.pop(run_id, None)

        resp = templates.TemplateResponse(
            request,
            "fragments/lab_result.html",
            {"last": last_row, "phases": state["phases"], "market": get_market_info()},
        )
        return resp

    if state["done"] and state["error"]:
        with _LAB_LOCK:
            _LAB_PROGRESS.pop(run_id, None)
        return templates.TemplateResponse(
            request,
            "fragments/lab_progress.html",
            {
                "run_id": run_id,
                "phases": state["phases"],
                "state": state,
                "done": True,
                "error": state["error"],
                "market": get_market_info(),
            },
        )

    return templates.TemplateResponse(
        request,
        "fragments/lab_progress.html",
        {
            "run_id": run_id,
            "phases": state["phases"],
            "state": state,
            "done": False,
            "error": None,
            "market": get_market_info(),
        },
    )


def _lab_narrative(last_row: dict, state: dict) -> str:
    """Short Turkish narrative about the lab run result."""
    try:
        from agent import MODEL, _get_client

        m = last_row
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=180,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Strategy Lab sonucu 2 cümleyle özetle:\n"
                        f"Strateji: {state['strategy_name']}\n"
                        f"PnL: {m.get('pnl_fmt', '?')} · Trades: {m.get('n_trades', 0)} · "
                        f"Win Rate: {m.get('win_rate_fmt', '?')} · Sortino: {m.get('sortino_fmt', '?')}\n"
                        "Başında 'Bu lab çalıştırması' ile başla."
                    ),
                }
            ],
        )
        return resp.content[0].text.strip()
    except Exception:
        pnl = last_row.get("pnl", 0) or 0
        return (
            f"Bu lab çalıştırması {state['strategy_name']} stratejisini üretti ve test etti. "
            f"{last_row.get('n_trades', 0)} trade ile {last_row.get('pnl_fmt', '?')} "
            f"{'kazandı' if pnl >= 0 else 'kaybetti'}."
        )
