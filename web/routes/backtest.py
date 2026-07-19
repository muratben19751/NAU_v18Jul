"""Single-run backtest page — with real-time step progress via polling.

Wiki References
---------------
See: [[backtesting_guide]], [[environment_contexts]]

The Backtest leg of [[environment_contexts]].
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from composer import BLOCK_CATALOG, load_catalog
from data import (
    BYBIT_ALL_INTERVALS,
    BYBIT_CATEGORIES,
    BYBIT_SYMBOLS,
    discover_index_tickers,
    external_instrument_object,
    list_catalog_bybit_symbols,
    list_external_instruments,
    load_external_bars,
    load_index_bars,
)
from web.viewmodels import iteration_row
from wiki_helper import read_wiki_page

try:
    import markdown as _md

    def render_md(txt: str) -> str:
        return _md.markdown(txt, extensions=["fenced_code", "tables"])
except Exception:  # pragma: no cover

    def render_md(txt: str) -> str:
        return f"<pre>{txt}</pre>"


router = APIRouter(prefix="/backtest")

# Leaf shared module — keeps the route→shared dependency arrow one-directional.
from web.shared import BACKTEST_LOG, ProgressStore  # noqa: E402
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402


def _catalog_index_symbols() -> list[str]:
    """Return equity/index tickers present in the Nautilus catalog bar/ directory."""
    from data import NAUTILUS_CATALOG_DIR

    bar_dir = NAUTILUS_CATALOG_DIR / "data" / "bar"
    if not bar_dir.exists():
        return []
    seen: set[str] = set()
    _bybit = {"BYBIT_LINEAR", "BYBIT_SPOT", "BYBIT_INVERSE"}
    for entry in bar_dir.iterdir():
        name = entry.name
        dot = name.find(".")
        if dot < 0:
            continue
        symbol = name[:dot]
        venue = name[dot + 1 :].split("-")[0]
        if venue not in _bybit:
            seen.add(symbol)
    return sorted(seen)


_LAST_RESULT: dict[str, Optional] = {
    "r": None,
    "spec_name": None,
    "narrative": "",
    "bars_info": {},
}
_LAST_RESULT_LOCK = threading.Lock()

# Progress store: run_id → {steps, done, result, error, spec_name}
# ProgressStore holds the dict + lock + capped done-first eviction; the
# _RUN_PROGRESS / _RUN_PROGRESS_LOCK aliases keep every existing direct-access
# site (worker thread ↔ async handler) unchanged.
_RUN_STORE = ProgressStore(50)
_RUN_PROGRESS = _RUN_STORE.raw()
_RUN_PROGRESS_LOCK = _RUN_STORE.lock

# Description-to-strategy generation (Claude → custom Python block). Separate
# store: generation is an LLM-dependent, slow phase that comes BEFORE the
# backtest; when done, the fragment chains into the existing /backtest/run (the
# 280-line run worker stays unchanged).
_GEN_STORE = ProgressStore(20)
_GEN_PROGRESS = _GEN_STORE.raw()
_GEN_LOCK = _GEN_STORE.lock

_GEN_PHASES = [
    "Parsing conditions",
    "Writing blocks (Claude)",
    "Saving blocks",
    "Compiling strategy",
]
_GEN_MAX_BLOCKS = 5  # guard against runaway breakdown (5 conditions = 5 LLM calls)

# Multi-TF sweep: run the SAME strategy SEPARATELY across multiple bar intervals
# and compare (the TF version of the multi-symbol sweep in robustness). Separate
# store; each interval runs sequentially in _worker, the panel fills live.
_SWEEP_STORE = ProgressStore(20)
_SWEEP_PROGRESS = _SWEEP_STORE.raw()
_SWEEP_LOCK = _SWEEP_STORE.lock

# Plan-preview LRU-style cache: keyed on (desc_lower, allow_short_bool).
# Avoids repeated propose_condition_breakdown LLM calls during iterative editing.
_PLAN_CACHE: dict = {}
_PLAN_CACHE_TTL = 300  # seconds
_PLAN_CACHE_MAX = 32  # max entries before evicting oldest

# Perf: a blank date range used to default to the ENTIRE cache (1M+ 1m bars →
# the backtest blows past the sandbox wall). When the user gives NO explicit
# start/end, bound the run to the most recent N bars so the default completes;
# an explicit range is always honored in full. Interval-agnostic (bar count).
_DEFAULT_MAX_BARS = 100_000

# BACKTEST_LOG, _rotate_if_large, _sanitize_floats, _chart_url and _log_backtest
# now live in web.shared (imported above as re-export aliases) — single source
# of truth; they were duplicated / cross-imported across the route modules.


# ── Canonical Nautilus backtest pipeline (for the live progress flow) ────────
# The run worker emits free-text progress messages; this maps them onto a fixed
# ordered pipeline so the UI can render "done → running(blinking) → pending"
# steps. Each phase: (key, label, [substring triggers matched case-insensitively
# against the step message]). The FIRST phase whose trigger matches a message
# advances the flow to (at least) that phase. Standard Nautilus steps + this
# app's additions (Claude block generation happens earlier, in /describe).
_BT_PHASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("start", "Starting · loading strategy", ("starting",)),
    ("read", "Reading data · parquet cache", ("reading data",)),
    ("range", "Determining date range", ("range", "date range", "window")),
    ("prepare", "Preparing bar objects", ("preparing data", "bar")),
    ("trend", "Loading trend filter data", ("trend filter",)),
    (
        "engine",
        "Setting up BacktestEngine · venue + commissions",
        ("backtestnode", "engine", "venue"),
    ),
    (
        "simulate",
        "Simulation running · processing orders",
        ("simulation running", "simulation", "running"),
    ),
    (
        "collect",
        "Collecting results",
        ("collecting", "completed · collecting", "simulation completed"),
    ),
    (
        "metrics",
        "Calculating metrics · PnL/Sharpe/DD",
        ("calculating metrics", "metrics", "positions found"),
    ),
    ("done", "Completed", ("completed · pnl", "completed ·")),
]


def _derive_bt_phases(steps: list[dict], done: bool, error: str | None) -> list[dict]:
    """Map free-text worker steps onto the canonical _BT_PHASES pipeline.

    Returns a list of {label, status, ts} where status ∈ done|running|pending.
    The highest-index phase any message triggered is the 'frontier'; earlier
    phases are 'done', the frontier is 'running' (blinks) unless the whole run
    is finished, later phases are 'pending'. On error the frontier shows as the
    failure point.
    """
    n = len(_BT_PHASES)
    frontier = 0
    phase_ts: list[str] = [""] * n
    for st in steps:
        msg = (st.get("msg") or "").lower()
        for i, (_key, _label, triggers) in enumerate(_BT_PHASES):
            if any(t in msg for t in triggers):
                if i >= frontier:
                    frontier = i
                phase_ts[i] = st.get("ts", "") or phase_ts[i]

    out: list[dict] = []
    for i, (_key, label, _tr) in enumerate(_BT_PHASES):
        if done and not error:
            status = "done"
        elif error and i == frontier:
            status = "error"
        elif i < frontier:
            status = "done"
        elif i == frontier:
            status = "done" if done else "running"
        else:
            status = "pending"
        out.append({"label": label, "status": status, "ts": phase_ts[i]})
    return out


def _recent_runs(limit: int = 6) -> list[dict]:
    """Read the last N backtest runs from the log (for the Run History panel)."""
    if not BACKTEST_LOG.exists():
        return []
    out: list[dict] = []
    try:
        # Read only the last 32 KB to avoid loading the full file into memory
        # as the log grows over thousands of runs.
        with open(BACKTEST_LOG, "rb") as fb:
            fb.seek(0, 2)
            size = fb.tell()
            fb.seek(max(0, size - 32768))
            tail = fb.read().decode("utf-8", errors="replace")
        lines = tail.splitlines()
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            spec = rec.get("spec") or {}
            m = rec.get("metrics") or {}
            ts = rec.get("ts", "")
            hhmm = ts[11:16] if len(ts) >= 16 else ""
            out.append(
                {
                    "time": hhmm,
                    "name": spec.get("name", "?"),
                    "pnl": m.get("pnl"),
                }
            )
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


def _generate_narrative(last_row: dict) -> str:
    """Short English narrative about the completed backtest. Falls back to template."""
    try:
        from agent import MODEL, _get_client

        m = last_row
        prompt = (
            f"Summarize a backtest result in 2-3 sentences in English:\n"
            f"Strategy: {m.get('strategy', '?')}\n"
            f"PnL: {m.get('pnl_fmt', '?')} ({m.get('pnl_pct_fmt', '?')})\n"
            f"Trade: {m.get('n_trades', 0)} · Wins: {m.get('n_wins', 0)} · Losses: {m.get('n_losses', 0)}\n"
            f"Win Rate: {m.get('win_rate_fmt', '?')} · Sharpe: {m.get('sharpe_fmt', '?')} · Sortino: {m.get('sortino_fmt', '?')}\n"
            f"Max Drawdown: {m.get('max_dd_fmt', '?')} · Avg Duration: {m.get('avg_dur_fmt', '?')}\n"
            f"Short, clear, in trader language. Begin with 'This strategy'."
        )
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        pass
    # Fallback template
    pnl_dir = "gained" if (last_row.get("pnl") or 0) >= 0 else "lost"
    return (
        f"This strategy opened {last_row.get('n_trades', 0)} trades and "
        f"{pnl_dir} {last_row.get('pnl_fmt', '?')}. "
        f"Win rate {(last_row.get('win_rate') or 0) * 100:.1f}%, "
        f"Sortino {last_row.get('sortino_fmt', '—')}, "
        f"max drawdown {last_row.get('max_dd_fmt', '—')}."
    )


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    # Sync handler → FastAPI runs it in a threadpool, so its blocking disk I/O
    # (load_catalog + read_wiki_page + _recent_runs) doesn't stall the event
    # loop. (reports/data pages already offload via asyncio.to_thread.)
    from server import get_market_info, templates

    catalog = load_catalog()
    last_row = None
    with _LAST_RESULT_LOCK:
        r = _LAST_RESULT["r"]
        spec_name = _LAST_RESULT["spec_name"]
        narrative = _LAST_RESULT.get("narrative", "")
        bi = dict(_LAST_RESULT.get("bars_info", {}))
    if r is not None:
        last_row = iteration_row(r)
        last_row["rationale"] = r.rationale
        last_row["equity_curve"] = r.equity_curve
        last_row["equity_dates"] = r.equity_dates
        last_row["spec_name"] = spec_name
        last_row["narrative"] = narrative
        last_row["bars_info"] = bi  # required for the robustness panel (#1)
        if bi.get("symbol"):
            _sid = (last_row.get("params") or {}).get("spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

    wiki_html = render_md(read_wiki_page("wiki/concepts/order_flow_pipeline.md"))
    preferred_spec_id = request.query_params.get("spec_id", "")
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "active": "backtest",
            "page_title": "Backtest",
            "market": get_market_info(),
            "catalog": catalog,
            "block_catalog": BLOCK_CATALOG,
            "last": last_row,
            "wiki_html": wiki_html,
            "preferred_spec_id": preferred_spec_id,
            "recent_runs": _recent_runs(6),
            # Main panel: symbol datalist (type-to-find), category selection, multi-TF checkboxes.
            "bybit_symbols": list_catalog_bybit_symbols()
            or [{"symbol": s, "category": "linear"} for s in BYBIT_SYMBOLS],
            "bybit_categories": BYBIT_CATEGORIES,
            "bybit_intervals": BYBIT_ALL_INTERVALS,
            "index_symbols": _catalog_index_symbols(),
        },
    )


@router.get("/tickers", response_class=HTMLResponse)
async def tickers(request: Request):
    import asyncio

    try:
        ts = await asyncio.to_thread(discover_index_tickers)
    except Exception as e:
        return HTMLResponse(
            f"<option value=''>ticker discovery failed: {type(e).__name__}: {e}</option>"
        )
    if not ts:
        return HTMLResponse("<option value=''>no tickers found</option>")
    return HTMLResponse("".join(f'<option value="{t}">{t}</option>' for t in ts))


@router.get("/external_instruments", response_class=HTMLResponse)
async def external_instruments(request: Request):
    """<option> list for the External data-source picker. Each option carries
    its available timeframes in data-grans so the UI can narrow the select."""
    import asyncio

    try:
        rows = await asyncio.to_thread(list_external_instruments)
    except Exception as e:
        return HTMLResponse(
            f"<option value=''>external catalog scan failed: {type(e).__name__}: {e}</option>"
        )
    if not rows:
        return HTMLResponse("<option value=''>no external instruments found</option>")
    return HTMLResponse(
        "".join(
            f'<option value="{r["instrument_id"]}" data-grans="{",".join(r["granularities"])}">'
            f"{r['instrument_id']}</option>"
            for r in rows
        )
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    spec_id: str = Form(...),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("60"),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form(""),
    granularity: str = Form("1d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ext_instrument: str = Form(""),
    ext_granularity: str = Form("1-DAY"),
    ext_start: str = Form(""),
    ext_end: str = Form(""),
):
    """Return a progress panel immediately, run the backtest in a daemon thread."""
    from server import templates

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Spec not found.</div>", status_code=404
        )

    # Capture all form params for the worker (no I/O in this handler)
    run_id = uuid.uuid4().hex[:8]
    # Evict to stay within cap (done-first, else oldest — see ProgressStore).
    _RUN_STORE.create_evicting(
        run_id,
        {
            "steps": [],
            "done": False,
            "result": None,
            "error": None,
            "spec_name": spec.name,
            "bars_info": {},
            "narrative": "",
        },
    )

    def _progress(msg: str) -> None:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        with _RUN_PROGRESS_LOCK:
            state = _RUN_PROGRESS.get(run_id)
            if state is not None:
                state["steps"].append({"ts": ts, "msg": msg})

    # All heavy work — parquet read + Bar construction + engine.run() — in thread
    def _set_error(msg: str) -> None:
        with _RUN_PROGRESS_LOCK:
            if run_id in _RUN_PROGRESS:
                _RUN_PROGRESS[run_id]["error"] = msg

    def _worker() -> None:
        import time as _time
        from datetime import timedelta

        import pandas as _pd

        from data import _bybit_cache_path, load_bybit_bars

        _t_start = _time.perf_counter()
        try:
            _progress(f"Starting · {spec.name} · {instrument_kind}")

            if instrument_kind == "Bybit":
                cache_path = _bybit_cache_path(category, symbol, interval)
                # Always use the actual range in the cache — if no date is
                # entered, the cache start/end stay fixed, so every run gives
                # the same result.
                if cache_path.exists():
                    df_check = _pd.read_parquet(cache_path)
                    cache_start = (
                        df_check.index[0].to_pydatetime().replace(tzinfo=UTC)
                        if not df_check.empty
                        else None
                    )
                    cache_end = (
                        df_check.index[-1].to_pydatetime().replace(tzinfo=UTC)
                        if not df_check.empty
                        else None
                    )
                else:
                    cache_start = cache_end = None

                if bybit_start:
                    start_dt = datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                else:
                    start_dt = cache_start or datetime.now(UTC) - timedelta(days=7)

                if bybit_end:
                    end_dt = datetime.fromisoformat(bybit_end).replace(
                        hour=23, minute=59, second=59, tzinfo=UTC
                    )
                else:
                    end_dt = cache_end or datetime.now(UTC)

                _progress(f"Reading data · {symbol}/{category}/{interval}…")
                bars = load_bybit_bars(
                    symbol=symbol,
                    interval=interval,
                    category=category,
                    start=start_dt,
                    end=end_dt,
                )
                if bars.empty:
                    _set_error(
                        f"No cached bars for {symbol}/{category}/{interval}. "
                        "Fetch them first from the Data catalog."
                    )
                    return
                # Infer base currency from symbol (ETHUSDT → ETH, SOLUSDT → SOL).
                base_guess = "BTC"
                for suffix in ("USDT", "USDC", "USD"):
                    if symbol.endswith(suffix):
                        base_guess = symbol[: -len(suffix)] or "BTC"
                        break
            if instrument_kind == "Bybit":
                # BacktestEngine path — required for per-trade detail (trade list
                # + chart markers). Since BacktestNode is summary-only, it can't
                # support trade-level visualization (v2rc1 constraint).
                bars_info = {
                    "symbol": symbol,
                    "category": category,
                    "interval": interval,
                    "n_bars": len(bars),
                    "start": str(bars.index[0]),
                    "end": str(bars.index[-1]),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                _progress(
                    f"BacktestEngine · {symbol}/{category}/{interval}m · {len(bars):,} bar"
                )
                # Route through the sandbox: builtin-only specs run in-process
                # (zero overhead); specs with custom blocks run in a killable
                # child so runaway user code can't hang the server.
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    spec,
                    bars,
                    recipe={
                        "symbol": symbol,
                        "base": base_guess,
                        "category": category,
                        "interval": interval,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=f"user-run · {symbol} {category} {interval}m",
                    # builtin specs also in child: Nautilus backtest holds the
                    # GIL; running in-process freezes the event loop (agent bug).
                    force_subprocess=True,
                )
            elif instrument_kind == "External":
                if not ext_instrument:
                    _set_error("Instrument required for External catalog.")
                    return
                # Dates optional — if left blank, the entire range in the catalog.
                try:
                    start_dt = (
                        datetime.fromisoformat(ext_start).replace(tzinfo=UTC)
                        if ext_start
                        else None
                    )
                    end_dt = (
                        datetime.fromisoformat(ext_end).replace(
                            hour=23, minute=59, second=59, tzinfo=UTC
                        )
                        if ext_end
                        else None
                    )
                except ValueError:
                    _set_error("ext_start and ext_end must be YYYY-MM-DD.")
                    return
                _progress(f"Reading data · {ext_instrument}/{ext_granularity}…")
                bars = load_external_bars(
                    ext_instrument, ext_granularity, start=start_dt, end=end_dt
                )
                if bars.empty:
                    _set_error(
                        f"No bars for {ext_instrument} {ext_granularity} in the "
                        "selected date range."
                    )
                    return
                instrument = external_instrument_object(ext_instrument)
                if instrument is None:
                    _set_error(
                        f"Instrument definition for {ext_instrument} not found in "
                        "the external catalog."
                    )
                    return
                rationale = f"user-run · External {ext_instrument} {ext_granularity}"
                run_spec = spec
                if float(spec.trade_size) < 1 and int(instrument.size_precision) == 0:
                    # Don't mutate the shared catalog spec — clone only for this run (#39)
                    import copy as _copy

                    run_spec = _copy.copy(spec)
                    run_spec.trade_size = 1.0
                    rationale += " · trade_size clamped to 1"
                # bars_info deliberately has no "symbol" key (Index convention):
                # the Bybit chart URL and robustness panel key on it and would
                # otherwise fetch Bybit klines for a non-Bybit instrument.
                bars_info = {
                    "ticker": ext_instrument,
                    "granularity": ext_granularity,
                    "n_bars": len(bars),
                    # L34: at daily+ granularity the time component is not shown —
                    # the open-time index is the nominal 'close − calendar interval'
                    # value (00:00 NY ≈ 04:00 UTC), NOT the session time.
                    "start": str(bars.index[0].date())
                    if ext_granularity in ("1-DAY", "1-WEEK")
                    else str(bars.index[0]),
                    "end": str(bars.index[-1].date())
                    if ext_granularity in ("1-DAY", "1-WEEK")
                    else str(bars.index[-1]),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                _progress(
                    f"BacktestEngine · {ext_instrument}/{ext_granularity} · {len(bars):,} bar"
                )
                # Sandbox path: builtin-only specs in-process (zero overhead),
                # specs with custom blocks run in a killable child — same
                # guarantee as the Bybit branch (child builds the instrument from recipe).
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "external",
                        "instrument_id": ext_instrument,
                        "granularity": ext_granularity,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=rationale,
                    force_subprocess=True,  # GIL isolation (see Bybit branch)
                )
            else:
                if not ticker:
                    _set_error("Ticker required for Index instruments.")
                    return
                try:
                    start_d, end_d = (
                        date.fromisoformat(start_date),
                        date.fromisoformat(end_date),
                    )
                except ValueError:
                    _set_error("start_date and end_date must be YYYY-MM-DD.")
                    return
                _progress(f"Reading data · {ticker}/{granularity}…")
                bars = load_index_bars(ticker, start_d, end_d, granularity)
                if bars.empty:
                    _set_error(f"No bars for {ticker}.")
                    return
                rationale = f"user-run · Index {ticker} {granularity} {start_d}→{end_d}"
                run_spec = spec
                if float(spec.trade_size) < 1:
                    # Don't mutate the shared catalog spec — clone only for this run (#39)
                    import copy as _copy

                    run_spec = _copy.copy(spec)
                    run_spec.trade_size = 1.0
                    rationale += " · trade_size clamped to 1"
                bars_info = {
                    "ticker": ticker,
                    "granularity": granularity,
                    "start": str(start_d),
                    "end": str(end_d),
                    "n_bars": len(bars),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                # The Index path is also in the sandbox: previously raw
                # run_composed_backtest held the GIL in this daemon thread
                # (freeze risk). The child rebuilds instrument/bar_type from
                # the index recipe.
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "index",
                        "ticker": ticker,
                        "granularity": granularity,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=rationale,
                    force_subprocess=True,
                )

            # Store result first so UI can display it regardless of log I/O outcome.
            narrative = ""
            if result.error is None:
                try:
                    nrow = iteration_row(result)
                    nrow["spec_name"] = spec.name
                    narrative = _generate_narrative(nrow)
                except Exception:
                    narrative = ""

            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:  # don't write if evicted (#7)
                    _RUN_PROGRESS[run_id]["result"] = result
                    _RUN_PROGRESS[run_id]["bars_info"] = bars_info
                    _RUN_PROGRESS[run_id]["narrative"] = narrative
            with _LAST_RESULT_LOCK:  # prevent torn read (#8)
                _LAST_RESULT["r"] = result
                _LAST_RESULT["spec_name"] = spec.name
                _LAST_RESULT["bars_info"] = bars_info
                _LAST_RESULT["narrative"] = narrative  # match with the new result (#23)

            try:
                _log_backtest(
                    run_spec if "run_spec" in locals() else spec,
                    result,
                    instrument_kind,
                    bars_info,
                    elapsed_sec=_time.perf_counter() - _t_start,
                )
            except Exception:
                pass  # log I/O failure must not hide the result already stored above
        except Exception as e:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()

    return templates.TemplateResponse(
        request,
        "fragments/backtest_progress.html",
        {
            "run_id": run_id,
            "steps": [],
            "phases": _derive_bt_phases([], False, None),
            "done": False,
            "error": None,
        },
    )


# ── Vol-Targeted Trend (registry strategy) direct run ────────────────────────


@router.post("/run_vtt", response_class=HTMLResponse)
async def run_vtt(
    request: Request,
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("1"),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form("QQQ"),
    granularity: str = Form("1d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    fast: int = Form(50),
    slow: int = Form(200),
    vol_span: int = Form(10),
    vol_target: float = Form(0.02),
    capital: float = Form(10000.0),
    allow_short: str = Form(
        ""
    ),  # checkbox sends "on" / absent; bool coercion is unreliable
):
    """Run vol_targeted_trend registry strategy; reuses the same progress/result flow."""
    import time as _time

    from server import templates

    _allow_short = bool(allow_short)  # checkbox sends "on" / absent → bool

    run_id = uuid.uuid4().hex[:8]
    _RUN_STORE.create_evicting(
        run_id,
        {
            "steps": [],
            "done": False,
            "result": None,
            "error": None,
            "spec_name": "vol_targeted_trend",
            "bars_info": {},
            "narrative": "",
        },
    )

    def _progress(msg: str) -> None:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        with _RUN_PROGRESS_LOCK:
            state = _RUN_PROGRESS.get(run_id)
            if state is not None:
                state["steps"].append({"ts": ts, "msg": msg})

    def _worker() -> None:
        from datetime import timedelta

        _t_start = _time.perf_counter()
        try:
            params = {
                "fast": fast,
                "slow": slow,
                "vol_span": vol_span,
                "vol_target": vol_target,
                "capital": capital,
                "allow_short": _allow_short,
            }

            if instrument_kind == "Bybit":
                import pandas as _pd

                from data import _bybit_cache_path, load_bybit_bars

                _progress(f"Veri yükleniyor · {symbol}/{category}/{interval}…")
                # Resolve date range from cache when user leaves dates blank
                cp = _bybit_cache_path(category, symbol, interval)
                if cp.exists():
                    _df = _pd.read_parquet(cp)
                    _cache_start = (
                        _df.index[0].to_pydatetime().replace(tzinfo=UTC)
                        if not _df.empty
                        else None
                    )
                    _cache_end = (
                        _df.index[-1].to_pydatetime().replace(tzinfo=UTC)
                        if not _df.empty
                        else None
                    )
                else:
                    _cache_start = _cache_end = None
                start_dt = (
                    datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                    if bybit_start
                    else (_cache_start or datetime.now(UTC) - timedelta(days=7))
                )
                end_dt = (
                    datetime.fromisoformat(bybit_end).replace(
                        hour=23, minute=59, second=59, tzinfo=UTC
                    )
                    if bybit_end
                    else (_cache_end or datetime.now(UTC))
                )
                bars_df = load_bybit_bars(
                    symbol=symbol,
                    category=category,
                    interval=interval,
                    start=start_dt,
                    end=end_dt,
                )
                bars_info = {
                    "symbol": symbol,
                    "category": category,
                    "interval": interval,
                }
            else:
                import pandas as _pd

                _progress(f"Veri yükleniyor · {ticker}/{granularity}…")
                try:
                    from data import INDEX_CACHE_DIR, _ticker_to_filename

                    cache_path = INDEX_CACHE_DIR / _ticker_to_filename(
                        ticker, granularity
                    )
                    if cache_path.exists():
                        _full = _pd.read_parquet(cache_path)
                        _s = (
                            date.fromisoformat(start_date)
                            if start_date
                            else _full.index.min().date()
                        )
                        _e = (
                            date.fromisoformat(end_date)
                            if end_date
                            else _full.index.max().date()
                        )
                    else:
                        _s = (
                            date.fromisoformat(start_date)
                            if start_date
                            else date(2000, 1, 1)
                        )
                        _e = date.fromisoformat(end_date) if end_date else date.today()
                except Exception:
                    _s = (
                        date.fromisoformat(start_date)
                        if start_date
                        else date(2000, 1, 1)
                    )
                    _e = date.fromisoformat(end_date) if end_date else date.today()
                bars_df = load_index_bars(ticker, _s, _e, granularity)
                bars_info = {"ticker": ticker, "granularity": granularity}

            if bars_df is None or bars_df.empty:
                raise RuntimeError("Veri bulunamadı — önce /data sayfasından indirin.")

            _progress(f"{len(bars_df)} bar yüklendi · çalıştırılıyor…")

            from backtest import run_backtest

            result = run_backtest(
                "vol_targeted_trend",
                params,
                bars_df,
                iteration_id=0,
                rationale=f"web vtt fast={fast} slow={slow} vt={vol_target} short={_allow_short}",
            )

            # Propagate engine-level errors to the progress store
            if result.error:
                with _RUN_PROGRESS_LOCK:
                    if run_id in _RUN_PROGRESS:
                        _RUN_PROGRESS[run_id]["error"] = result.error
                return

            narrative = (
                f"EWMA vol-targeted trend · fast={fast} slow={slow} "
                f"vol_span={vol_span} vol_target={vol_target:.3f} "
                f"capital={capital:,.0f} allow_short={_allow_short}"
            )

            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["result"] = result
                    _RUN_PROGRESS[run_id]["bars_info"] = bars_info
                    _RUN_PROGRESS[run_id]["narrative"] = narrative

            # Keep _LAST_RESULT in sync so a page refresh shows the VTT result
            with _LAST_RESULT_LOCK:
                _LAST_RESULT["r"] = result
                _LAST_RESULT["spec_name"] = "vol_targeted_trend"
                _LAST_RESULT["bars_info"] = bars_info
                _LAST_RESULT["narrative"] = narrative

            elapsed = _time.perf_counter() - _t_start
            _progress(f"Tamamlandı ({elapsed:.1f}s)")

            try:
                import json as _json

                from web.shared import BACKTEST_LOG, rotate_if_large, sanitize_floats

                _rec = {
                    "ts": datetime.now(UTC).isoformat(),
                    "elapsed_sec": round(elapsed, 3),
                    "spec": {
                        "id": "vol_targeted_trend",
                        "name": f"VTT fast={fast} slow={slow}",
                        "params": params,
                    },
                    "instrument": instrument_kind,
                    "bars": bars_info,
                    "rationale": result.rationale,
                    "error": result.error,
                    "metrics": sanitize_floats(result.metrics),
                }
                from web.shared import _BACKTEST_LOG_LOCK

                with _BACKTEST_LOG_LOCK:
                    rotate_if_large(BACKTEST_LOG)
                    with open(BACKTEST_LOG, "a") as _f:
                        _f.write(_json.dumps(_rec, default=str) + "\n")
            except Exception:
                pass
        except Exception as e:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()

    return templates.TemplateResponse(
        request,
        "fragments/backtest_progress.html",
        {
            "run_id": run_id,
            "steps": [],
            "phases": _derive_bt_phases([], False, None),
            "done": False,
            "error": None,
        },
    )


# ── Generate strategy from description → chain into /backtest/run ────────────
# The user describes a strategy in natural language; Claude writes NEW Python
# blocks for entry+exit (codegate validation + smoke), the spec is written to the
# catalog, and the fragment triggers the existing /backtest/run with the same
# instrument parameters. This way instrument selection (Bybit/Index/External) and
# the backtest path stay in a single source.


def _gen_state_view(gen_id: str) -> dict | None:
    """Copy under lock — giving the live dict to the template would be a torn read.

    ``chain_vals``: the full form values (instrument parameters + new spec_id)
    to be POSTed to /backtest/run when generation completes. None if no chain.
    """
    with _GEN_LOCK:
        raw = _GEN_PROGRESS.get(gen_id)
        if raw is None:
            return None
        chain = None
        if raw["done"] and raw["spec_id"] and not raw["error"]:
            chain = dict(raw["run_params"], spec_id=raw["spec_id"])
        return {
            "phases": [dict(p) for p in raw["phases"]],
            "done": raw["done"],
            "error": raw["error"],
            "spec_id": raw["spec_id"],
            "spec_name": raw["spec_name"],
            "chain_vals": chain,
        }


def _normalize_intervals(codes: list[str]) -> list[str]:
    """Only supported Bybit interval codes, preserve form order, deduplicate."""
    valid = {code for code, _label in BYBIT_ALL_INTERVALS}
    seen: set[str] = set()
    return [c for c in codes if c in valid and not (c in seen or seen.add(c))]


def _gen_phase(gen_id: str, idx: int, detail: str = "", done: bool = False) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _GEN_LOCK:
        st = _GEN_PROGRESS.get(gen_id)
        if st is None:
            return
        for i, p in enumerate(st["phases"]):
            if i < idx or (i == idx and done):
                p["status"] = "done"
            elif i == idx:
                p["status"] = "running"
                p["detail"] = detail
                p["ts"] = ts


@router.post("/describe", response_class=HTMLResponse)
async def describe(
    request: Request,
    description: str = Form(""),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("60"),
    intervals: list[str] = Form(default=[]),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form(""),
    granularity: list[str] = Form(default=["1d"]),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ext_instrument: str = Form(""),
    ext_granularity: str = Form("1-DAY"),
    ext_start: str = Form(""),
    ext_end: str = Form(""),
    allow_short: str = Form(""),
):
    """Generate custom blocks from a description; when done, the fragment triggers the backtest.

    ``intervals`` (checkboxes) is the multi-TF selection: if 2+, generation
    chains at the end into /backtest/sweep (comparison table); if 1, into
    /backtest/run (full result). If empty, falls back to the single ``interval``
    field (old behavior)."""
    from server import templates

    desc = (description or "").strip()
    if len(desc) < 10:
        return HTMLResponse(
            "<div class='empty-state'>Please describe the strategy in a bit more "
            "detail (at least 10 characters) — Claude will write Python blocks "
            "from this text.</div>",
            status_code=400,
        )

    # Short/sell direction: the Backtest form carries an allow_short checkbox
    # (defaults ON). An unchecked HTML checkbox sends nothing → "" → False.
    _allow_short = bool(allow_short)

    # Multi-TF: checkboxes → normalize; if empty, fall back to single interval; if that's missing too, 1h.
    norm_intervals = (
        _normalize_intervals(intervals) or _normalize_intervals([interval]) or ["60"]
    )

    gen_id = uuid.uuid4().hex[:8]
    # Backtest parameters to be chained — spec_id is added at the end of generation.
    # ``intervals``/``intervals_csv``: the multi-TF list for the sweep chain (csv,
    # a robust path that doesn't rely on hx-vals array encoding). ``interval``: single-TF /run.
    # For Index: granularity is a list of checkboxes; ``granularity_csv`` carries the
    # multi-TF list for the Index sweep chain, ``granularity`` (first) is the single-TF /run.
    gran_list = granularity if isinstance(granularity, list) else [granularity]
    gran_list = [g for g in gran_list if g] or ["1d"]
    gran_first = gran_list[0]
    run_params = {
        "instrument_kind": instrument_kind,
        "symbol": symbol,
        "category": category,
        "interval": norm_intervals[0],
        "intervals": norm_intervals,
        "intervals_csv": ",".join(norm_intervals),
        "bybit_start": bybit_start,
        "bybit_end": bybit_end,
        "ticker": ticker,
        "granularity": gran_first,
        "granularity_csv": ",".join(gran_list),
        "start_date": start_date,
        "end_date": end_date,
        "ext_instrument": ext_instrument,
        "ext_granularity": ext_granularity,
        "ext_start": ext_start,
        "ext_end": ext_end,
    }
    _GEN_STORE.create_evicting(
        gen_id,
        {
            "phases": [
                {"label": p, "status": "pending", "detail": "", "ts": ""}
                for p in _GEN_PHASES
            ],
            "done": False,
            "error": None,
            "spec_id": "",
            "spec_name": "",
            "run_params": run_params,
        },
    )

    def _worker() -> None:
        from agent import (
            GeneratedCodeError,
            propose_condition_breakdown,
            propose_custom_block,
        )
        from composer import (
            ComposedStrategySpec,
            SignalBlock,
            append_to_catalog,
            new_spec_id,
            register_custom_from_disk,
        )
        from custom_block_store import is_valid_name, save_custom
        from web.routes.lab import _atr_stop_fallback

        try:
            # ── Phase 0: split the description into SEPARATE conditions (each
            # becomes a separate, editable block). On failure, fall back to a
            # single entry + single exit (old behavior). ──
            _gen_phase(
                gen_id,
                0,
                "Claude is splitting the description into separate conditions…",
            )
            entry_logic, exit_logic = "OR", "OR"
            try:
                bd = propose_condition_breakdown(desc)
                label = bd["label"]
                entry_logic, exit_logic = bd["entry_logic"], bd["exit_logic"]
                conditions = bd["conditions"][:_GEN_MAX_BLOCKS]
                _gen_phase(
                    gen_id,
                    0,
                    f"✓ {len(conditions)} conditions · entry={entry_logic} / exit={exit_logic}",
                    done=True,
                )
            except Exception as e:  # LLM/parse/schema — fall back to single-block path
                label = desc[:40].strip() or "Described strategy"
                conditions = [
                    {"role": "entry", "label": label, "desc": desc},
                    {"role": "exit", "label": label, "desc": desc},
                ]
                _gen_phase(
                    gen_id,
                    0,
                    f"fell back to single condition ({type(e).__name__})",
                    done=True,
                )

            # ── Phase 1: write a SEPARATE custom Python block for each condition ──
            _gen_phase(gen_id, 1, "Claude is writing the blocks…")
            made: list[tuple[str, dict, str]] = []  # (name, block, role)
            counters = {"entry": 0, "exit": 0}
            # CAUTION: "entry"[0]=="exit"[0]=="e" → DON'T USE role[0] (name would collide).
            _role_tag = {"entry": "e", "exit": "x"}

            # Assign stable names before dispatch so order is deterministic.
            named_conds: list[tuple[str, dict]] = []
            for cond in conditions:
                role = cond["role"]
                counters[role] += 1
                name = f"desc_{_role_tag[role]}{counters[role]}_{gen_id}"
                named_conds.append((name, cond))

            # Generate all blocks in parallel — each LLM call is independent.
            from concurrent.futures import ThreadPoolExecutor
            from concurrent.futures import as_completed as _as_completed

            def _gen_one(args: tuple) -> tuple:
                _name, _cond = args
                _gen_phase(gen_id, 1, f"{_cond['label']} ({_cond['role']})…")
                try:
                    blk = propose_custom_block(
                        _cond["label"], _cond["desc"], _cond["role"]
                    )
                except GeneratedCodeError:
                    blk = None
                return _name, blk, _cond["role"]

            with ThreadPoolExecutor(
                max_workers=min(len(named_conds), _GEN_MAX_BLOCKS)
            ) as _ex:
                futures = {_ex.submit(_gen_one, nc): nc[0] for nc in named_conds}
                results_map: dict[str, tuple] = {}
                for fut in _as_completed(futures):
                    _name, blk, role = fut.result()
                    results_map[_name] = (blk, role)

            # Reconstruct in original order so entry/exit ordering is stable.
            for name, cond in named_conds:
                blk, role = results_map[name]
                if blk is None:
                    if role == "exit":
                        blk = _atr_stop_fallback()
                    else:
                        continue
                made.append((name, blk, role))

            if not any(r == "entry" for _, _, r in made):
                raise RuntimeError(
                    "No entry block could be generated — make the description concrete "
                    "(which indicator, which threshold, when to enter)."
                )
            if not any(r == "exit" for _, _, r in made):
                made.append((f"desc_xf_{gen_id}", _atr_stop_fallback(), "exit"))
            n_e = sum(1 for _, _, r in made if r == "entry")
            n_x = sum(1 for _, _, r in made if r == "exit")
            _gen_phase(gen_id, 1, f"✓ {n_e} entry + {n_x} exit blocks", done=True)

            # ── Phase 2: save + register (visible/editable one by one in Composer) ──
            _gen_phase(gen_id, 2, "Writing blocks to disk…")
            for name, blk, _role in made:
                if not is_valid_name(name):
                    raise RuntimeError(f"Invalid block name: {name}")
                save_custom(name, blk["meta"], blk["code"], prompt=desc)
                register_custom_from_disk(name)
            _gen_phase(gen_id, 2, f"{len(made)} blocks visible in Composer", done=True)

            # ── Phase 3: compile spec (block-level OR/AND) + write to catalog ──
            _gen_phase(gen_id, 3, "Compiling strategy…")

            def _params(blk: dict) -> dict:
                raw = blk["meta"].get("params") or {}
                return {
                    k: (v.get("default") if isinstance(v, dict) else v)
                    for k, v in raw.items()
                }

            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=label,
                description=desc,
                blocks=[
                    SignalBlock(type=name, role=role, params=_params(blk))
                    for name, blk, role in made
                ],
                trade_size=0.01,
                allow_short=_allow_short,
                entry_logic=entry_logic,
                exit_logic=exit_logic,
            )
            err = spec.validate()
            if err:
                raise RuntimeError(f"Spec error: {err}")
            append_to_catalog(spec)  # M14: locked append
            _gen_phase(
                gen_id, 3, f"✓ {spec.name} · {entry_logic}/{exit_logic}", done=True
            )

            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["spec_id"] = spec.id
                    _GEN_PROGRESS[gen_id]["spec_name"] = spec.name
        except Exception as e:
            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    state = _gen_state_view(gen_id)
    return templates.TemplateResponse(
        request,
        "fragments/describe_progress.html",
        {"gen_id": gen_id, "state": state, "done": False},
    )


@router.get("/describe/progress/{gen_id}", response_class=HTMLResponse)
async def describe_progress(request: Request, gen_id: str):
    from server import templates

    state = _gen_state_view(gen_id)
    if state is None:
        return HTMLResponse(
            "<div class='empty-state'>Generation record not found (the server may "
            "have been restarted).</div>"
        )
    return templates.TemplateResponse(
        request,
        "fragments/describe_progress.html",
        {"gen_id": gen_id, "state": state, "done": state["done"]},
    )


# ── Live plan preview (while typing, read-only) ─────────────────────────────
# As the user types a description, a single propose_condition_breakdown call
# interprets the description, maps conditions to built-in blocks with a LOCAL
# heuristic, and predicts likely pitfalls. Generates/saves/backtests nothing.

# Canonical indicator name (agent._HINT_INDICATORS) → similar built-in block type.
# Indicators not in the list (Keltner, CCI, Williams %R, OBV, SuperTrend,
# Ichimoku) have no built-in → always a new custom block.
_INDICATOR_TO_BUILTIN: dict[str, str] = {
    "RSI": "rsi_threshold",
    "ADX/DMI": "adx_threshold",
    "ATR": "atr_stop",  # exit only
    "MACD": "macd_cross",
    "Stochastic": "stoch_rsi_cross",
    "Bollinger": "bollinger_break",
    "EMA": "ema_cross",
    "SMA/MA": "ma_cross",
    "WaveTrend": "wave_trend_cross",
    "Donchian": "donchian_channel",
    "Momentum/ROC": "momentum",
    "Hacim": "volume_spike",
}


def _match_builtin(role: str, label: str, desc: str) -> dict | None:
    """Scan a condition's own text and return the similar built-in block.

    Local (no LLM): returns the built-in equivalent of the first indicator
    recognized in the condition's label+desc. atr_stop is only suggested in the
    exit role. No match returns None → 'a new custom block will be written'.
    """
    from agent import _hint_indicators

    for canon in _hint_indicators(f"{label} {desc}"):
        btype = _INDICATOR_TO_BUILTIN.get(canon)
        if not btype or btype not in BLOCK_CATALOG:
            continue
        if btype == "atr_stop" and role != "exit":
            continue
        meta = BLOCK_CATALOG[btype]
        return {"type": btype, "label": meta.get("label", btype)}
    return None


def _is_equity_target(
    instrument_kind: str, category: str, ticker: str, ext_instrument: str
) -> bool:
    """Is the target instrument a stock/index (for the size_precision=0 pitfall)?

    Bybit spot/linear/inverse crypto → size_precision>0. Index/External →
    equity-like (integer lots). A filled ticker/ext_instrument also implies the
    equity path.
    """
    k = (instrument_kind or "").lower()
    return (
        bool(ticker) or bool(ext_instrument) or k in ("index", "external", "us-index")
    )


def _local_fallback_breakdown(desc: str, inds: list[str]) -> dict:
    """LLM breakdown failed: a local single-entry + single-exit shape.

    Produces the same shape as the worker's single-block fallback, so the
    template renders both the LLM and local paths uniformly.
    """
    label = (
        f"{inds[0]}-based strategy"
        if inds
        else (desc[:40].strip() or "Described strategy")
    )
    return {
        "label": label,
        "entry_logic": "OR",
        "exit_logic": "OR",
        "conditions": [
            {"role": "entry", "label": label, "desc": desc},
            {"role": "exit", "label": "ATR stop (default)", "desc": "ATR-based exit"},
        ],
        "usage": {},
    }


def _predict_plan_warnings(
    desc: str,
    bd: dict,
    rows: list[dict],
    instrument_kind: str,
    symbol: str,
    category: str,
    ticker: str,
    ext_instrument: str,
    allow_short: bool = True,
) -> list[dict]:
    """Predict likely pitfalls from the description + interpretation. Returns a list of {level, text}."""
    w: list[dict] = []
    low = (desc or "").lower()

    # (1) allow_short — if the description implies a short/sell direction but the
    # toggle is OFF, the short leg is silently ignored. When the toggle is ON
    # (the default) shorts are supported, so no warning is needed.
    short_terms = ("short", "açığa", "aciga", "sat", "sell", "aşağı", "asagi", "down")
    if not allow_short and any(t in low for t in short_terms):
        w.append(
            {
                "level": "info",
                "text": "Tarif short/satış yönü içeriyor ama 'Short işlemlere izin ver' "
                "kapalı. Bu haliyle short bacağı yok sayılır — short'ları backtest etmek "
                "için formdaki anahtarı açın (backend MARGIN hesaba geçer).",
            }
        )

    # (2) 3+ conditions with AND → almost zero trades
    n_entry = sum(1 for r in rows if r["role"] == "entry")
    if bd.get("entry_logic") == "AND" and n_entry >= 3:
        w.append(
            {
                "level": "warn",
                "text": f"{n_entry} entry conditions will be combined with AND. A 3+ condition AND "
                "fires on <0.5% of bars → almost zero trades. Loosen the thresholds "
                "or make some conditions OR.",
            }
        )

    # (3) size_precision=0 pitfall — equity/index target
    if _is_equity_target(instrument_kind, category, ticker, ext_instrument):
        w.append(
            {
                "level": "warn",
                "text": "The target instrument looks like a stock/index (size_precision=0). "
                "If trade_size < 1, the trade size is automatically rounded up to 1 lot — "
                "a larger position than expected. Use integer lots.",
            }
        )

    # (4) Bollinger cross → 0-trade risk
    if "bollinger" in low and ("cross" in low or "kesiş" in low or "kesis" in low):
        w.append(
            {
                "level": "warn",
                "text": "A Bollinger band cross usually produces 0 trades (the bands are very "
                "rarely touched exactly). Loosen the threshold or use a 'band touch' logic.",
            }
        )

    # (5) no recognized indicator → all blocks written from scratch as custom
    if not any(r.get("reuse") for r in rows):
        w.append(
            {
                "level": "info",
                "text": "No recognized indicator in the description — all blocks will be written "
                "as custom from scratch. Clarifying which indicator/threshold/condition applies "
                "improves generation success.",
            }
        )

    return w


def _preview_signals(
    rows: list[dict], allow_short: bool, n_bars: int = 400
) -> dict | None:
    """Estimate entry/exit signals for the matched built-in blocks over the last
    N bars of BTC 1m data — a LOCAL, LLM-free approximation for the plan preview.

    Runs each matched built-in's indicator (via indicators.py pure functions,
    default params) bar-by-bar and marks long/short/exit points. This is an
    ESTIMATE: the actually-generated custom blocks and their tuned params may
    differ. Returns {closes, dates, signals:[{i,sig}]} or None if no data / no
    recognized built-ins.
    """
    import pandas as _pd

    from data import BYBIT_CACHE_DIR
    from indicators import calc_atr, calc_rsi_series, ema, sma

    cache = BYBIT_CACHE_DIR / "linear_BTCUSDT_1m.parquet"
    if not cache.exists():
        return None
    try:
        df = _pd.read_parquet(cache)
    except Exception:
        return None
    if df.empty:
        return None
    df = df.iloc[-(n_bars + 60) :]  # +warmup for indicators
    closes = [float(x) for x in df["close"].tolist()]
    highs = [float(x) for x in df["high"].tolist()]
    lows = [float(x) for x in df["low"].tolist()]
    vols = [float(x) for x in df["volume"].tolist()]
    dates = [str(df.index[i])[:16] for i in range(len(df))]
    n = len(closes)
    if n < 40:
        return None

    # Which built-in blocks were matched, split by role.
    entry_types = {
        r["reuse"]["type"] for r in rows if r.get("reuse") and r["role"] == "entry"
    }
    exit_types = {
        r["reuse"]["type"] for r in rows if r.get("reuse") and r["role"] == "exit"
    }
    if not entry_types and not exit_types:
        return None

    # Precompute indicator series (default params). indicators.py returns
    # LEFT-TRUNCATED series (shorter than closes), so left-pad with None to align
    # index i → bar i.
    def _pad(series: list) -> list:
        return (
            [None] * (n - len(series)) + list(series)
            if len(series) < n
            else list(series)
        )

    rsi = _pad(calc_rsi_series(closes, 14))
    ema_fast, ema_slow = _pad(ema(closes, 10)), _pad(ema(closes, 30))
    sma_fast, sma_slow = _pad(sma(closes, 10)), _pad(sma(closes, 30))
    sma20 = _pad(sma(closes, 20))
    vol_sma20 = _pad(sma(vols, 20))

    def _entry_fires(i: int):
        """Return (signal, reason) if any matched entry built-in fires at bar i, else (None, "")."""
        for t in entry_types:
            if (
                t == "rsi_threshold"
                and i > 0
                and rsi[i] is not None
                and rsi[i - 1] is not None
            ):
                if rsi[i - 1] >= 30 > rsi[i]:
                    return "long", "RSI<30 (aşırı satım)"
            elif t in ("ma_cross", "ema_cross") and i > 0:
                f, s = (
                    (ema_fast, ema_slow) if t == "ema_cross" else (sma_fast, sma_slow)
                )
                nm = "EMA" if t == "ema_cross" else "MA"
                if (
                    f[i] is not None
                    and s[i] is not None
                    and f[i - 1] is not None
                    and s[i - 1] is not None
                ):
                    if f[i - 1] <= s[i - 1] and f[i] > s[i]:
                        return "long", f"{nm} yukarı kesişim (10>30)"
                    if allow_short and f[i - 1] >= s[i - 1] and f[i] < s[i]:
                        return "short", f"{nm} aşağı kesişim (10<30)"
            elif t == "bollinger_break" and sma20[i] is not None and i >= 20:
                window = closes[i - 19 : i + 1]
                mean = sma20[i]
                sd = (sum((c - mean) ** 2 for c in window) / len(window)) ** 0.5
                if closes[i] < mean - 2 * sd:
                    return "long", "alt Bollinger bandı altı"
                if allow_short and closes[i] > mean + 2 * sd:
                    return "short", "üst Bollinger bandı üstü"
            elif t == "volume_spike" and vol_sma20[i] is not None and vol_sma20[i] > 0:
                if vols[i] > 2.0 * vol_sma20[i] and i > 0 and closes[i] > closes[i - 1]:
                    return "long", "hacim 2x + fiyat yukarı"
            elif t == "price_breakout" and i >= 20:
                window = closes[i - 20 : i]
                if closes[i] > max(window):
                    return "long", "20-bar yüksek kırılımı"
                if allow_short and closes[i] < min(window):
                    return "short", "20-bar düşük kırılımı"
            elif t == "momentum" and i >= 10:
                if closes[i] > closes[i - 10]:
                    return "long", "10-bar pozitif momentum"
                if allow_short and closes[i] < closes[i - 10]:
                    return "short", "10-bar negatif momentum"
        return None, ""

    atr = calc_atr(highs, lows, closes, 14) if exit_types else None

    signals: list[dict] = []
    start = max(0, n - n_bars)
    in_pos = False
    entry_px = 0.0
    for i in range(start, n):
        sig = None
        reason = ""
        if not in_pos:
            fired, reason = _entry_fires(i)
            if fired:
                sig, in_pos, entry_px = fired, True, closes[i]
        else:
            # Exit: any matched exit built-in, or a simple ATR stop when atr_stop matched.
            if "atr_stop" in exit_types and atr:
                if closes[i] <= entry_px - 3.0 * atr:
                    sig, in_pos, reason = "exit", False, "3x ATR stop"
            if (
                sig is None
                and "rsi_threshold" in exit_types
                and i > 0
                and rsi[i] is not None
                and rsi[i - 1] is not None
            ):
                if rsi[i - 1] <= 70 < rsi[i]:
                    sig, in_pos, reason = "exit", False, "RSI>70 (aşırı alım)"
        signals.append({"i": i - start, "sig": sig, "reason": reason})

    # ── Indicator overlays for the matched built-ins (sliced to the view window)
    # so the chart shows WHAT the strategy watches, not just the fire points. ──
    def _slice(series: list) -> list:
        return [None if v is None else round(float(v), 2) for v in series[start:]]

    overlays: list[dict] = []
    all_types = entry_types | exit_types
    if "ma_cross" in all_types:
        overlays.append(
            {"label": "MA 10", "color": "#5EA0F6", "data": _slice(sma_fast)}
        )
        overlays.append(
            {"label": "MA 30", "color": "#E8B44C", "data": _slice(sma_slow)}
        )
    if "ema_cross" in all_types:
        overlays.append(
            {"label": "EMA 10", "color": "#5EA0F6", "data": _slice(ema_fast)}
        )
        overlays.append(
            {"label": "EMA 30", "color": "#E8B44C", "data": _slice(ema_slow)}
        )
    if "bollinger_break" in all_types:
        # Recompute upper/lower bands (mean ± 2σ over 20) across the window.
        up, lo, mid = [], [], []
        for i in range(start, n):
            if sma20[i] is None or i < 19:
                up.append(None)
                lo.append(None)
                mid.append(None)
                continue
            window = closes[i - 19 : i + 1]
            mean = sma20[i]
            sd = (sum((c - mean) ** 2 for c in window) / len(window)) ** 0.5
            up.append(round(mean + 2 * sd, 2))
            lo.append(round(mean - 2 * sd, 2))
            mid.append(round(mean, 2))
        overlays.append(
            {"label": "Bollinger üst", "color": "#8a8a8a", "data": up, "dashed": True}
        )
        overlays.append({"label": "Bollinger orta", "color": "#5EA0F6", "data": mid})
        overlays.append(
            {"label": "Bollinger alt", "color": "#8a8a8a", "data": lo, "dashed": True}
        )

    # RSI is a 0-100 oscillator → separate pane (not price-scaled).
    panes: list[dict] = []
    if "rsi_threshold" in all_types:
        panes.append(
            {
                "label": "RSI 14",
                "color": "#c084fc",
                "data": _slice(rsi),
                "guides": [30, 70],
            }
        )

    return {
        "closes": closes[start:],
        "dates": dates[start:],
        "signals": signals,
        "overlays": overlays,
        "panes": panes,
    }


@router.post("/plan", response_class=HTMLResponse)
async def plan_preview(
    request: Request,
    description: str = Form(""),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    ticker: str = Form(""),
    ext_instrument: str = Form(""),
    allow_short: str = Form(""),
):
    """Interpret the description + show the block plan + warnings (read-only preview).

    A single propose_condition_breakdown LLM call; mapping and warnings are local.
    Generates/saves/backtests nothing. Triggered with a debounce while typing.
    Results are cached by (desc, allow_short) for 5 minutes to avoid redundant
    LLM calls during iterative editing sessions.
    """
    import asyncio

    from server import templates

    desc = (description or "").strip()
    if len(desc) < 12:
        return templates.TemplateResponse(
            request, "fragments/plan_preview.html", {"stage": "prompt"}
        )

    _allow_short_bool = bool(allow_short)

    # Simple TTL cache keyed on (desc, allow_short) — avoids repeated LLM calls
    # while the user edits the same description.
    import time as _time

    _cache_key = (desc.lower(), _allow_short_bool)
    _cached = _PLAN_CACHE.get(_cache_key)
    if _cached and (_time.monotonic() - _cached["ts"]) < _PLAN_CACHE_TTL:
        bd, llm_ok = _cached["bd"], _cached["llm_ok"]
    else:
        from agent import _hint_indicators

        local_inds = _hint_indicators(desc)
        llm_ok = True
        try:
            from agent import propose_condition_breakdown

            bd = await asyncio.to_thread(propose_condition_breakdown, desc)
        except Exception:
            llm_ok = False
            bd = _local_fallback_breakdown(desc, local_inds)
        _PLAN_CACHE[_cache_key] = {"bd": bd, "llm_ok": llm_ok, "ts": _time.monotonic()}
        # Evict oldest entries beyond capacity
        if len(_PLAN_CACHE) > _PLAN_CACHE_MAX:
            oldest = min(_PLAN_CACHE, key=lambda k: _PLAN_CACHE[k]["ts"])
            _PLAN_CACHE.pop(oldest, None)

    rows: list[dict] = []
    for cond in bd["conditions"]:
        rows.append(
            {
                "role": cond["role"],
                "label": cond["label"],
                "desc": cond["desc"],
                "reuse": _match_builtin(cond["role"], cond["label"], cond["desc"]),
            }
        )

    warnings = _predict_plan_warnings(
        desc,
        bd,
        rows,
        instrument_kind,
        symbol,
        category,
        ticker,
        ext_instrument,
        allow_short=_allow_short_bool,
    )

    # Local, LLM-free signal estimate for the matched built-ins (best effort).
    # Offloaded to thread — _preview_signals does a blocking pd.read_parquet.
    try:
        import asyncio as _asyncio

        chart_data = await _asyncio.to_thread(_preview_signals, rows, _allow_short_bool)
    except Exception:
        chart_data = None

    return templates.TemplateResponse(
        request,
        "fragments/plan_preview.html",
        {
            "stage": "plan",
            "llm_ok": llm_ok,
            "label": bd["label"],
            "entry_logic": bd["entry_logic"],
            "exit_logic": bd["exit_logic"],
            "rows": rows,
            "warnings": warnings,
            "n_entry": sum(1 for r in rows if r["role"] == "entry"),
            "n_exit": sum(1 for r in rows if r["role"] == "exit"),
            "chart_data": chart_data,
        },
    )


# ── Multi-TF sweep: same strategy, multiple bar intervals ────────────────────
# For each interval, Bybit bars are loaded and run SEPARATELY via run_backtest_guarded;
# results are collected in a single comparison table. Bar-loading (load_bybit_bars) and
# the run (run_backtest_guarded) are existing primitives — the 280-line /run worker is
# not duplicated.

# Extract metrics into table columns (NAU: sharpe = per-trade). None-safe.
_SWEEP_METRIC_KEYS = (
    "pnl_pct",
    "sharpe_per_trade",
    "max_dd_pct",
    "n_trades",
    "win_rate",
    "profit_factor",
)


def _sweep_row_metrics(metrics: dict) -> dict:
    m = metrics or {}
    return {k: m.get(k) for k in _SWEEP_METRIC_KEYS}


def _sweep_state_view(sweep_id: str) -> dict | None:
    with _SWEEP_LOCK:
        raw = _SWEEP_PROGRESS.get(sweep_id)
        if raw is None:
            return None
        return {
            "spec_name": raw["spec_name"],
            "symbol": raw["symbol"],
            "category": raw["category"],
            "done": raw["done"],
            "error": raw["error"],
            "rows": [dict(r) for r in raw["rows"]],
        }


@router.post("/sweep", response_class=HTMLResponse)
async def sweep(
    request: Request,
    spec_id: str = Form(...),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    intervals: list[str] = Form(default=[]),
    intervals_csv: str = Form(""),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form(""),
    granularity_csv: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
):
    """Run the same strategy on each of the selected TFs → comparison table.

    Bybit: intervals (1/5/15/60/240/D). Index/Equity: granularity_csv
    (1m/5m/15m/60m/1d) loaded via load_index_bars over the full cache range
    (or explicit start/end dates)."""
    from server import templates

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Strategy not found.</div>", status_code=404
        )

    is_index = instrument_kind == "Index"

    if is_index:
        # Index: granularity list (csv from the describe→sweep chain).
        picked = [g.strip() for g in granularity_csv.split(",") if g.strip()]
        if not ticker:
            return HTMLResponse(
                "<div class='empty-state'>Ticker required for Index sweep.</div>",
                status_code=400,
            )
        if len(picked) < 2:
            return HTMLResponse(
                "<div class='empty-state'>Select at least 2 timeframes "
                "(for comparison).</div>",
                status_code=400,
            )
        _idx_labels = {"1m": "1m", "5m": "5m", "15m": "15m", "60m": "1h", "1d": "1d"}
        label_of = _idx_labels
        display_symbol, display_category = ticker, "index"
    else:
        # The describe→sweep chain carries intervals as csv (without relying on hx-vals
        # array encoding). A direct form POST (standalone/tests) provides the ``intervals``
        # list; if present, it takes priority.
        raw = list(intervals)
        if not raw and intervals_csv:
            raw = [x.strip() for x in intervals_csv.split(",") if x.strip()]
        picked = _normalize_intervals(raw)
        if len(picked) < 2:
            return HTMLResponse(
                "<div class='empty-state'>Select at least 2 timeframes "
                "(for comparison).</div>",
                status_code=400,
            )
        label_of = dict(BYBIT_ALL_INTERVALS)
        display_symbol, display_category = symbol, category

    sweep_id = uuid.uuid4().hex[:8]
    _SWEEP_STORE.create_evicting(
        sweep_id,
        {
            "spec_name": spec.name,
            "symbol": display_symbol,
            "category": display_category,
            "done": False,
            "error": None,
            "rows": [
                {
                    "interval": code,
                    "label": label_of.get(code, code),
                    "status": "pending",
                    "error": "",
                    "metrics": _sweep_row_metrics({}),
                    "n_bars": None,
                    "date_from": "",
                    "date_to": "",
                }
                for code in picked
            ],
        },
    )

    def _row(code: str, **upd) -> None:
        with _SWEEP_LOCK:
            st = _SWEEP_PROGRESS.get(sweep_id)
            if st is None:
                return
            for r in st["rows"]:
                if r["interval"] == code:
                    r.update(upd)
                    return

    def _worker() -> None:
        from concurrent.futures import ThreadPoolExecutor
        from datetime import timedelta

        import pandas as _pd

        from data import _base_ccy, _bybit_cache_path, load_bybit_bars
        from parallel_exec import get_worker_count, parallel_enabled
        from sandbox import run_backtest_guarded

        def _run_one_index(code: str) -> None:
            _row(code, status="running")
            try:
                from data import INDEX_CACHE_DIR, _ticker_to_filename, load_index_bars

                # Full cache range unless explicit dates given.
                if start_date and end_date:
                    s_d, e_d = (
                        date.fromisoformat(start_date),
                        date.fromisoformat(end_date),
                    )
                else:
                    cp = (
                        INDEX_CACHE_DIR
                        / f"{_ticker_to_filename(ticker)}_{code}.parquet"
                    )
                    if not cp.exists():
                        _row(
                            code,
                            status="error",
                            error="no cache — fetch from the Data screen",
                        )
                        return
                    _df = _pd.read_parquet(cp)
                    if _df.empty:
                        _row(code, status="error", error="no bars")
                        return
                    s_d = _df.index[0].date()
                    e_d = _df.index[-1].date()
                bars = load_index_bars(ticker, s_d, e_d, code)
                if bars.empty:
                    _row(code, status="error", error="no bars")
                    return
                actual_from = bars.index[0].strftime("%Y-%m-%d")
                actual_to = bars.index[-1].strftime("%Y-%m-%d")
                run_spec = spec
                if float(spec.trade_size) < 1:
                    import copy as _copy

                    run_spec = _copy.copy(spec)
                    run_spec.trade_size = 1.0
                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={"source": "index", "ticker": ticker, "granularity": code},
                    iteration_id=0,
                    rationale=f"tf-sweep · Index {ticker} {code}",
                    force_subprocess=True,
                )
                if result.error:
                    _row(code, status="error", error=result.error[:120])
                else:
                    _row(
                        code,
                        status="done",
                        metrics=_sweep_row_metrics(result.metrics),
                        n_bars=len(bars),
                        date_from=actual_from,
                        date_to=actual_to,
                    )
            except Exception as e:
                _row(code, status="error", error=f"{type(e).__name__}: {e}"[:120])

        base = _base_ccy(symbol)

        def _run_one(code: str) -> None:
            _row(code, status="running")
            try:
                cp = _bybit_cache_path(category, symbol, code)
                if not cp.exists():
                    _row(
                        code,
                        status="error",
                        error="no cache — fetch from the Data screen",
                    )
                    return
                # If dates are blank, the full range of the cache (deterministic).
                if bybit_start:
                    s_dt = datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                else:
                    s_dt = None
                if bybit_end:
                    e_dt = datetime.fromisoformat(bybit_end).replace(
                        hour=23, minute=59, second=59, tzinfo=UTC
                    )
                else:
                    e_dt = None
                if s_dt is None or e_dt is None:
                    _df = _pd.read_parquet(cp)
                    if not _df.empty:
                        s_dt = s_dt or _df.index[0].to_pydatetime().replace(tzinfo=UTC)
                        e_dt = e_dt or _df.index[-1].to_pydatetime().replace(tzinfo=UTC)
                    else:
                        s_dt = s_dt or datetime.now(UTC) - timedelta(days=7)
                        e_dt = e_dt or datetime.now(UTC)
                bars = load_bybit_bars(
                    symbol=symbol,
                    interval=code,
                    category=category,
                    start=s_dt,
                    end=e_dt,
                )
                if bars.empty:
                    _row(code, status="error", error="no bars")
                    return
                # Use actual bar range for display.
                actual_from = (
                    bars.index[0].strftime("%Y-%m-%d") if not bars.empty else ""
                )
                actual_to = (
                    bars.index[-1].strftime("%Y-%m-%d") if not bars.empty else ""
                )
                result = run_backtest_guarded(
                    spec,
                    bars,
                    recipe={
                        "symbol": symbol,
                        "base": base,
                        "category": category,
                        "interval": code,
                    },
                    iteration_id=0,
                    rationale=f"tf-sweep · {symbol} {category} {code}",
                    force_subprocess=True,
                )
                if result.error:
                    _row(code, status="error", error=result.error[:120])
                else:
                    _row(
                        code,
                        status="done",
                        metrics=_sweep_row_metrics(result.metrics),
                        n_bars=len(bars),
                        date_from=actual_from,
                        date_to=actual_to,
                    )
            except Exception as e:
                _row(code, status="error", error=f"{type(e).__name__}: {e}"[:120])

        runner = _run_one_index if is_index else _run_one
        try:
            # Intervals are INDEPENDENT full backtests on different bars, so run
            # them concurrently — each force_subprocess child runs in parallel
            # while its supervisor thread just blocks. Bounded + kill-switchable
            # (NAUTILUS_PARALLEL=0 → sequential). Per-row errors handled inside.
            workers = min(len(picked), get_worker_count()) if parallel_enabled() else 1
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                list(ex.map(runner, picked))
        except Exception as e:
            with _SWEEP_LOCK:
                if sweep_id in _SWEEP_PROGRESS:
                    _SWEEP_PROGRESS[sweep_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _SWEEP_LOCK:
                if sweep_id in _SWEEP_PROGRESS:
                    _SWEEP_PROGRESS[sweep_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    return templates.TemplateResponse(
        request,
        "fragments/sweep_progress.html",
        {"sweep_id": sweep_id, "state": _sweep_state_view(sweep_id), "done": False},
    )


@router.get("/sweep/progress/{sweep_id}", response_class=HTMLResponse)
async def sweep_progress(request: Request, sweep_id: str):
    from server import templates

    state = _sweep_state_view(sweep_id)
    if state is None:
        return HTMLResponse(
            "<div class='empty-state'>Sweep record not found (the server may have "
            "been restarted).</div>"
        )
    return templates.TemplateResponse(
        request,
        "fragments/sweep_progress.html",
        {"sweep_id": sweep_id, "state": state, "done": state["done"]},
    )


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    from server import templates

    # Snapshot state under lock to avoid torn reads from the worker thread.
    with _RUN_PROGRESS_LOCK:
        raw = _RUN_PROGRESS.get(run_id)
        if raw is None:
            return HTMLResponse("<div class='empty-state'>Unknown run ID.</div>")
        state = {
            "done": raw["done"],
            "result": raw["result"],
            "error": raw["error"],
            "spec_name": raw["spec_name"],
            "steps": list(raw["steps"]),
            "bars_info": raw.get("bars_info", {}),
            "narrative": raw.get("narrative", ""),
        }

    if state["done"] and state["result"] is not None:
        result = state["result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["spec_name"]
        last_row["steps"] = state["steps"]
        last_row["narrative"] = state["narrative"]  # generated in the worker (#9)
        bi = state.get("bars_info", {})
        last_row["bars_info"] = bi  # for the robustness panel (#1)
        if bi.get("symbol"):
            _sid = (last_row.get("params") or {}).get("spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

        with _RUN_PROGRESS_LOCK:
            _RUN_PROGRESS.pop(run_id, None)

        return templates.TemplateResponse(
            request,
            "fragments/backtest_result.html",
            {"last": last_row},
        )

    if state["done"] and state["error"]:
        with _RUN_PROGRESS_LOCK:
            _RUN_PROGRESS.pop(run_id, None)
        return HTMLResponse(
            f"<div class='panel' style='border-color:rgba(239,68,68,0.5)'>"
            f"<div class='panel-body'><span class='badge exit'>✗ ERROR</span>"
            f"<pre class='diagram mt-3'>{state['error']}</pre></div></div>"
        )

    return templates.TemplateResponse(
        request,
        "fragments/backtest_progress.html",
        {
            "run_id": run_id,
            "steps": state["steps"],
            "phases": _derive_bt_phases(state["steps"], False, None),
            "done": False,
            "error": None,
        },
    )
