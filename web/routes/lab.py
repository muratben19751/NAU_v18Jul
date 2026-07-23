"""Strategy Lab — Autonomous strategy generator.

One click: Claude generates a strategy idea → creates custom signal blocks →
combines the blocks → runs backtest → shows KPI + equity curve.

Endpoints:
    GET  /lab                    Page
    POST /lab/run                Start the autonomous pipeline (returns immediately)
    GET  /lab/progress/{run_id}  Status polling (HTMX every 1s)

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
    "Generating strategy idea",
    "Creating entry block",
    "Creating exit block",
    "Saving blocks",
    "Compiling strategy",
    "Backtest running",
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
                        f"No data in cache for {symbol}/{category}/{interval}. "
                        "Fetch it first from the /data screen."
                    )
            return

        # ── Phase 0: Generate strategy idea ────────────────────────────────
        _set_phase(run_id, 0, "Requesting idea from Claude…")
        idea = _generate_idea(hint)
        _done_phase(run_id, 0, f"Idea: {idea['label']}")

        # ── Phase 1: Entry block ────────────────────────────────────────────
        entry_name = f"lab_entry_{run_id}"
        _set_phase(run_id, 1, f"Writing entry block: {idea['entry_label']}…")
        try:
            entry_block = propose_custom_block(
                idea["entry_label"], idea["entry_desc"], role_hint="entry"
            )
        except GeneratedCodeError as e:
            raise RuntimeError(f"Failed to generate entry block: {e}") from e
        entry_block["name"] = entry_name
        _done_phase(run_id, 1, f"✓ {entry_name}")

        # ── Phase 2: Exit block ─────────────────────────────────────────────
        exit_name = f"lab_exit_{run_id}"
        _set_phase(run_id, 2, f"Writing exit block: {idea['exit_label']}…")
        try:
            exit_block = propose_custom_block(
                idea["exit_label"], idea["exit_desc"], role_hint="exit"
            )
        except GeneratedCodeError:
            # Fallback: use ATR stop as exit
            exit_block = _atr_stop_fallback()
        exit_block["name"] = exit_name
        _done_phase(run_id, 2, f"✓ {exit_name}")

        # ── Phase 3: Save ───────────────────────────────────────────────────
        _set_phase(run_id, 3, "Writing blocks to disk…")
        for blk_name, blk in [(entry_name, entry_block), (exit_name, exit_block)]:
            if not is_valid_name(blk_name):
                raise RuntimeError(f"Invalid block name: {blk_name}")
            save_custom(blk_name, blk["meta"], blk["code"], prompt=hint)
            register_custom_from_disk(blk_name)
        _done_phase(run_id, 3, "Blocks are visible in Strategy Composer")

        # ── Phase 4: Compile strategy ───────────────────────────────────────
        _set_phase(run_id, 4, "Compiling strategy…")

        def _extract_params(blk: dict) -> dict:
            """Safely extract param defaults from LLM meta."""
            raw = blk["meta"].get("params") or {}
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[k] = v.get("default")
                else:
                    out[k] = v  # scalar default directly
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
            raise RuntimeError(f"Spec error: {err}")
        # M14: lockless load→append→save silently lost the strategy under
        # concurrent runs via last-writer-wins — use the locked helper.
        from composer import append_to_catalog

        append_to_catalog(spec)
        with _LAB_LOCK:
            if run_id in _LAB_PROGRESS:
                _LAB_PROGRESS[run_id]["strategy_name"] = spec.name
        _done_phase(run_id, 4, f"✓ {spec.name} (visible in Backtest selector)")

        # ── Phase 5: Backtest ───────────────────────────────────────────────
        _set_phase(run_id, 5, f"BacktestEngine running: {spec.name}…")
        # Sandbox: each spec in a killable child — Nautilus backtest holds the
        # GIL; running in-process freezes the server's event loop (agent bug
        # class).
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
            f"The user gave this strategy idea: '{hint}'\n\n"
            "Elaborate on this idea and return it in the following JSON format:\n"
            '{"label": "short name (max 40 chars)", '
            '"description": "1 sentence general description", '
            '"entry_label": "short name for the entry block", '
            '"entry_desc": "describe the entry signal (FULL OHLCV: will run on aligned series of closes + highs + lows + volumes)", '
            '"exit_label": "short name for the exit block", '
            '"exit_desc": "describe the exit signal"}\n'
            "Return only JSON, write nothing else."
        )
    else:
        prompt = (
            "Generate a crypto trading strategy idea. "
            "Make it technical-indicator based; full OHLCV data (closes/highs/lows/volumes) "
            "is available, and indicators requiring high-low such as ATR/ADX/Stochastic are also valid. "
            "Return it in the following JSON format:\n"
            '{"label": "short name (max 40 chars)", '
            '"description": "1 sentence general description", '
            '"entry_label": "short name for the entry block", '
            '"entry_desc": "describe the entry signal (will run on closes/highs/lows/volumes series)", '
            '"exit_label": "short name for the exit block", '
            '"exit_desc": "describe the exit signal"}\n'
            "Return only JSON, write nothing else."
        )
    import json

    try:
        from agent import _create_message, _extract_json_object, _get_client

        client = _get_client()
        resp = _create_message(
            client,
            _purpose="lab_idea",
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
            "description": "EMA fast/slow crossover signal.",
            "entry_label": "EMA Cross Entry",
            "entry_desc": "Generate a long signal when EMA9 crosses above EMA21",
            "exit_label": "EMA Cross Exit",
            "exit_desc": "Generate an exit signal when EMA9 crosses below EMA21",
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
            "help": "ATR-based fallback exit.",
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

    # Same protection as progress(): after the worker thread starts, passing
    # the live _LAB_PROGRESS[run_id] reference to the template without a lock
    # risks a torn read (and 'list changed size' during backtest_steps
    # iteration) — take a snapshot under the lock.
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
            return HTMLResponse("<div class='empty-state'>Unknown run ID.</div>")
        state = {
            # Deep copy phase dicts — don't let the worker mutate after the lock is released
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
    """Short English narrative about the lab run result."""
    try:
        from agent import _create_message, _get_client

        m = last_row
        client = _get_client()
        resp = _create_message(
            client,
            _purpose="narrative",
            max_tokens=180,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Summarize the Strategy Lab result in 2 sentences, in English:\n"
                        f"Strategy: {state['strategy_name']}\n"
                        f"PnL: {m.get('pnl_fmt', '?')} · Trades: {m.get('n_trades', 0)} · "
                        f"Win Rate: {m.get('win_rate_fmt', '?')} · Sortino: {m.get('sortino_fmt', '?')}\n"
                        "Start with 'This lab run'."
                    ),
                }
            ],
        )
        return resp.content[0].text.strip()
    except Exception:
        pnl = last_row.get("pnl", 0) or 0
        return (
            f"This lab run generated and tested the {state['strategy_name']} strategy. "
            f"With {last_row.get('n_trades', 0)} trades it "
            f"{'gained' if pnl >= 0 else 'lost'} {last_row.get('pnl_fmt', '?')}."
        )
