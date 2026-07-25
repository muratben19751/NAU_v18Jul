"""Single-run backtest page — with real-time step progress via polling.

Wiki References
---------------
See: [[backtesting_guide]], [[environment_contexts]], [[parquet_data_catalog]]

The Backtest leg of [[environment_contexts]].

Not: ``describe`` üretimi bitince chain-tetiği (``#bt-chain-trigger``
data-attr'ları → JS ile ``/backtest/run|/sweep``) SADECE BİR kez ``#result``'a
servis edilmeli — ``_mark_gen_chained`` atomik test-and-set + ``describe_progress``
204 fallback, gecikmeli/örtüşen bir poll'ün backtest'i yeniden tetikleyip sonucu
silmesini önler (bkz. [[webapp_module_map]], 2026-07-20 sonuç-kaybolma yarışı).

Bybit cache bounds keşfi FULL parquet read yerine pyarrow row-group metadata
istatistikleriyle yapılır (min/max timestamp; 3M+ satır taranmaz) — bkz.
[[parquet_data_catalog]] "Parquet Cache Bounds Keşfi: Row-Group Metadata".
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from composer import BLOCK_CATALOG, load_catalog
from data import (
    BYBIT_ALL_INTERVALS,
    discover_index_tickers,
    external_instrument_object,
    list_external_instruments,
    load_external_bars,
    load_index_bars,
)
from web.viewmodels import iteration_row

router = APIRouter(prefix="/backtest")

# Leaf shared module — keeps the route→shared dependency arrow one-directional.
from web.shared import (  # noqa: E402
    BACKTEST_LOG,
    ChatStore,
    ProgressStore,
    session_id,
)
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import load_result_snapshot as _load_result_snapshot  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402
from web.shared import save_result_snapshot as _save_result_snapshot  # noqa: E402


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


# Last backtest result — now SESSION-SCOPED (sid → slot) instead of a single
# process-global slot that was last-writer-wins across all users. Bounded like
# _DRAFTS: drop the oldest sid when over cap. Read via _last_result_get(sid),
# written by the run worker via _last_result_set(sid, ...).
_LAST_RESULT: dict[str, dict] = {}
_LAST_RESULT_LOCK = threading.Lock()
_MAX_RESULT_SESSIONS = 500


def _empty_result_slot() -> dict:
    return {"r": None, "spec_name": None, "narrative": "", "bars_info": {}}


def _last_result_get(sid: str) -> dict:
    """Return a copy of this session's last-result slot (empty slot if none)."""
    with _LAST_RESULT_LOCK:
        slot = _LAST_RESULT.get(sid)
        return dict(slot) if slot is not None else _empty_result_slot()


def _last_result_set(sid: str, *, r, spec_name, narrative, bars_info) -> None:
    with _LAST_RESULT_LOCK:
        if sid not in _LAST_RESULT and len(_LAST_RESULT) >= _MAX_RESULT_SESSIONS:
            _LAST_RESULT.pop(next(iter(_LAST_RESULT)), None)
        _LAST_RESULT[sid] = {
            "r": r,
            "spec_name": spec_name,
            "narrative": narrative,
            "bars_info": bars_info,
        }


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

# ── Per-session single-active-run guard (Bug: duplicate submission) ─────────
# One session may have ONE unfinished backtest-family job (run / describe-gen /
# sweep) at a time. A second submit while the first is live returns 409 +
# HX-Toast; HTMX does not swap non-2xx responses, so the live progress panel in
# #result is preserved instead of being reset by a new run. The registry
# self-clears: the worker's ``finally`` sets the store record's ``done`` flag,
# and _session_active_kind() treats done/evicted records as inactive.
_ACTIVE_RUNS: dict[str, tuple[str, str]] = {}  # sid → (kind, run/gen/sweep id)
_ACTIVE_RUNS_LOCK = threading.Lock()
# Prune threshold — entries self-clear on the owner's next submit, but sessions
# that never come back would otherwise accumulate forever. The ProgressStores
# cap at 50+20+20 records, so past ~90 entries the rest are guaranteed stale.
_ACTIVE_RUNS_MAX = 500

_BUSY_LABELS = {
    "run": "backtest",
    "gen": "strategy generation",
    "sweep": "timeframe sweep",
}


def _active_store(kind: str) -> ProgressStore:
    return {"run": _RUN_STORE, "gen": _GEN_STORE, "sweep": _SWEEP_STORE}[kind]


def _session_active_kind(sid: str) -> str | None:
    """Return 'run'|'gen'|'sweep' if this session has a live job, else None."""
    with _ACTIVE_RUNS_LOCK:
        ent = _ACTIVE_RUNS.get(sid)
    if ent is None:
        return None
    kind, rid = ent
    raw = _active_store(kind).get(rid)
    if raw is None or raw.get("done"):
        # Finished (or evicted/restarted) — drop the stale entry.
        with _ACTIVE_RUNS_LOCK:
            if _ACTIVE_RUNS.get(sid) == ent:
                _ACTIVE_RUNS.pop(sid, None)
        return None
    return kind


def _session_set_active(sid: str, kind: str, rid: str) -> None:
    with _ACTIVE_RUNS_LOCK:
        over_cap = len(_ACTIVE_RUNS) >= _ACTIVE_RUNS_MAX and sid not in _ACTIVE_RUNS
        snapshot = list(_ACTIVE_RUNS.items()) if over_cap else []
        _ACTIVE_RUNS[sid] = (kind, rid)
    if not over_cap:
        return
    # Opportunistic prune of abandoned sessions (store lookups deliberately
    # OUTSIDE the registry lock — no lock nesting).
    for other_sid, (other_kind, other_rid) in snapshot:
        raw = _active_store(other_kind).get(other_rid)
        if raw is None or raw.get("done"):
            with _ACTIVE_RUNS_LOCK:
                if _ACTIVE_RUNS.get(other_sid) == (other_kind, other_rid):
                    _ACTIVE_RUNS.pop(other_sid, None)


def _busy_response(kind: str) -> HTMLResponse:
    """409 for a concurrent submit — body is informational only (no swap)."""
    label = _BUSY_LABELS.get(kind, "run")
    resp = HTMLResponse(
        f"<div class='empty-state'>A {label} is already in progress for this "
        "session — wait for it to finish.</div>",
        status_code=409,
    )
    resp.headers["HX-Toast"] = (
        f"err|A {label} is already running - wait for it to finish."
    )
    return resp


def _invalid_date_range(start: str, end: str) -> str | None:
    """Return an error message when a start/end pair is inverted or malformed.

    Blank values are fine (blank = full cache). Only rejects when BOTH are
    given and start > end, or either fails to parse as YYYY-MM-DD.
    """
    s, e = (start or "").strip(), (end or "").strip()
    if not s and not e:
        return None
    try:
        sd = date.fromisoformat(s) if s else None
        ed = date.fromisoformat(e) if e else None
    except ValueError:
        return "Dates must be in YYYY-MM-DD format."
    if sd and ed and sd > ed:
        return "End date cannot be before the start date."
    return None


def _date_error_response(msg: str) -> HTMLResponse:
    """400 for an invalid date range — toast + non-swapping body."""
    resp = HTMLResponse(f"<div class='empty-state'>{msg}</div>", status_code=400)
    resp.headers["HX-Toast"] = f"err|{msg}"
    return resp


# Plan-preview FIFO/TTL cache keyed on (desc_lower, allow_short_bool). Avoids
# repeated propose_condition_breakdown LLM calls during iterative editing.
# Eviction is FIFO by insertion ts (not LRU — hits do not refresh ts).
_PLAN_CACHE: dict = {}
_PLAN_CACHE_TTL = 300  # seconds
_PLAN_CACHE_MAX = 32  # max entries before evicting oldest
# In-flight dedup: (desc_lower, allow_short) → asyncio.Future. Concurrent
# same-key requests await the first request's LLM call instead of firing N.
_PLAN_INFLIGHT: dict = {}

# ── Multi-turn "AI ile iyileştir" sohbeti — sunucu tarafı konuşma store'u ──
# conv_id → {"messages": [...], "context": {...}, "last_refined": str, "ts": monotonic}.
# web.shared.ChatStore: TTL + oldest-first eviction (chat'in "done"u yok). LLM
# çağrısı ASLA lock altında yapılmaz: get() (kopya) → chat_refine → commit().
# (Bu store, ChatStore'un çıkarıldığı el-yapımı orijinaldi; artık ortak sınıfı kullanır.)
_CHAT = ChatStore()


def _parse_bool_form(v: str) -> bool:
    """Parse an HTML form bool. Checkboxes send 'on'/absent, but explicit
    falsey strings ('0','false','off','no') must map to False — plain
    bool('0') would be True."""
    return str(v).strip().lower() in {"1", "true", "on", "yes"}


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


def _result_viewmodel(r, spec_name: str, narrative: str, bars_info: dict) -> dict:
    """Build the full backtest result view-model consumed by
    ``fragments/backtest_result.html``. Shared by page(), the run worker
    (snapshot write), and the history reload route so all three render the
    identical result screen. Returns a JSON-serializable dict."""
    row = iteration_row(r)
    row["rationale"] = r.rationale
    row["equity_curve"] = r.equity_curve
    row["equity_dates"] = r.equity_dates
    row["spec_name"] = spec_name
    row["narrative"] = narrative
    row["bars_info"] = dict(bars_info or {})  # required for the robustness panel (#1)
    if row["bars_info"].get("symbol"):
        _sid = (row.get("params") or {}).get("spec_id", "")
        row["chart_url"] = _chart_url(row["bars_info"], _sid)
        row["chart_symbol"] = row["bars_info"]["symbol"]
        row["chart_category"] = row["bars_info"].get("category", "linear")
        row["chart_interval"] = row["bars_info"].get("interval", "60")
    return row


def _recent_runs(limit: int = 6) -> list[dict]:
    """Read the last N backtest runs from the log (for the Run History panel)."""
    if not BACKTEST_LOG.exists():
        return []
    out: list[dict] = []
    try:
        # Read a bounded tail to avoid loading the full multi-MB log. A single
        # record can be large (equity/MTM arrays embedded in metrics ~280 KB),
        # so the window must comfortably cover `limit` records. If the first
        # line came out partial (window cut mid-record) it fails json.loads and
        # is skipped below — harmless.
        window = max(2_000_000, limit * 400_000)
        with open(BACKTEST_LOG, "rb") as fb:
            fb.seek(0, 2)
            size = fb.tell()
            fb.seek(max(0, size - window))
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
                    "run_id": rec.get("run_id") or "",
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
        from agent import _create_message, _get_client

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
        resp = _create_message(
            client,
            _purpose="narrative",
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
    # Merged into the unified Studio (Faz 4). The Backtest form now lives as the
    # "Backtest" tab of /studio; this root path redirects there, preserving the
    # ?spec_id= deep-link (the catalog picker pre-selects it). The /backtest/*
    # HTMX endpoints (run, sweep, describe, plan, chat, result) are unchanged.
    from fastapi.responses import RedirectResponse

    spec_id = request.query_params.get("spec_id", "")
    target = f"/studio?spec_id={spec_id}" if spec_id else "/studio"
    return RedirectResponse(target, status_code=307)


@router.get("/result/{run_id}", response_class=HTMLResponse)
def result_snapshot(request: Request, run_id: str):
    """Reload a previously stored backtest result screen (history tab click).

    Renders the identical ``fragments/backtest_result.html`` from the snapshot
    persisted at run time, so equity/drawdown/heatmap/price-chart/trades all
    rebuild exactly as they were. Swapped into #result, so the page's existing
    htmx:afterSettle hooks re-init the charts + robustness panel."""
    from server import templates

    last = _load_result_snapshot(run_id)
    if not last:
        resp = HTMLResponse(
            "<div class='panel'><div class='panel-body empty-state'>"
            "Bu çalışmanın kayıtlı sonucu bulunamadı (yalnızca son çalışmalar saklanır)."
            "</div></div>"
        )
        resp.headers["HX-Toast"] = "err|Kayitli sonuc bulunamadi"
        return resp
    return templates.TemplateResponse(
        request, "fragments/backtest_result.html", {"last": last}
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
    initial_capital: float = Form(0.0),
    commission_pct: float = Form(-1.0),
    trade_size: float = Form(0.0),
):
    """Return a progress panel immediately, run the backtest in a daemon thread."""
    from server import templates

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Spec not found.</div>", status_code=404
        )

    # Server-side date validation — an inverted range must never start a pipeline.
    date_err = (
        _invalid_date_range(bybit_start, bybit_end)
        or _invalid_date_range(start_date, end_date)
        or _invalid_date_range(ext_start, ext_end)
    )
    if date_err:
        return _date_error_response(date_err)

    # Session id captured HERE (request-time) — the worker thread has no request,
    # so it writes the result into THIS session's slot.
    sid = session_id(request)
    # One live run per session: a duplicate submit must not reset the progress
    # panel of the run already in flight.
    busy = _session_active_kind(sid)
    if busy:
        return _busy_response(busy)

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
    _session_set_active(sid, "run", run_id)

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

        # Broker overrides — None means "use defaults".
        _cap = initial_capital if initial_capital > 0 else None
        _comm_bps = (commission_pct * 100.0) if commission_pct >= 0 else None

        _t_start = _time.perf_counter()
        try:
            _progress(f"Starting · {spec.name} · {instrument_kind}")

            if instrument_kind == "Bybit":
                cache_path = _bybit_cache_path(category, symbol, interval)
                _progress(
                    f"Reading data · parquet cache · {symbol}/{category}/{interval}"
                )
                # Read only row-group metadata to discover cache bounds — avoids
                # loading 3M+ rows just to find start/end timestamps.
                if cache_path.exists():
                    import pyarrow.parquet as _pq

                    _pf = _pq.ParquetFile(cache_path)
                    _meta = _pf.metadata
                    if _meta.num_rows > 0:
                        # Row-group stats give min/max timestamps without full scan.
                        # col[0] is "open" (price); the index is "__index_level_0__".
                        _rg0 = _meta.row_group(0)
                        _rgN = _meta.row_group(_meta.num_row_groups - 1)
                        _idx_col = next(
                            (
                                i
                                for i in range(_rg0.num_columns)
                                if "index" in _rg0.column(i).path_in_schema.lower()
                            ),
                            None,
                        )
                        _col0 = _rg0.column(_idx_col) if _idx_col is not None else None
                        _colN = _rgN.column(_idx_col) if _idx_col is not None else None
                        _ts0 = (
                            _col0.statistics.min
                            if (
                                _col0
                                and _col0.statistics
                                and _col0.statistics.has_min_max
                            )
                            else None
                        )
                        _tsN = (
                            _colN.statistics.max
                            if (
                                _colN
                                and _colN.statistics
                                and _colN.statistics.has_min_max
                            )
                            else None
                        )
                        # Fall back to full read if stats unavailable.
                        if _ts0 is None or _tsN is None:
                            _df_idx = _pd.read_parquet(cache_path, columns=[])
                            cache_start = (
                                _df_idx.index[0].to_pydatetime().replace(tzinfo=UTC)
                                if not _df_idx.empty
                                else None
                            )
                            cache_end = (
                                _df_idx.index[-1].to_pydatetime().replace(tzinfo=UTC)
                                if not _df_idx.empty
                                else None
                            )
                        else:
                            cache_start = (
                                _pd.Timestamp(_ts0).to_pydatetime().replace(tzinfo=UTC)
                            )
                            cache_end = (
                                _pd.Timestamp(_tsN).to_pydatetime().replace(tzinfo=UTC)
                            )
                    else:
                        cache_start = cache_end = None
                else:
                    cache_start = cache_end = None

                if bybit_start:
                    start_dt = datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                else:
                    # Cap the default window to 365 days so the Bar construction
                    # loop doesn't churn through 3M+ rows when no date is entered.
                    default_start = (cache_end or datetime.now(UTC)) - timedelta(
                        days=365
                    )
                    start_dt = (
                        max(cache_start, default_start)
                        if cache_start
                        else default_start
                    )

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

                _run_spec = spec
                if trade_size > 0:
                    import copy as _copy

                    _run_spec = _copy.copy(spec)
                    _run_spec.trade_size = trade_size

                result = run_backtest_guarded(
                    _run_spec,
                    bars,
                    recipe={
                        "symbol": symbol,
                        "base": base_guess,
                        "category": category,
                        "interval": interval,
                        "initial_capital": _cap,
                        "commission_bps_override": _comm_bps,
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

                if trade_size > 0:
                    import copy as _copy

                    run_spec = _copy.copy(run_spec)
                    run_spec.trade_size = trade_size

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "external",
                        "instrument_id": ext_instrument,
                        "granularity": ext_granularity,
                        "initial_capital": _cap,
                        "commission_bps_override": _comm_bps,
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
                # Resolve dates — if blank, use the full cache range.
                try:
                    import pandas as _pd2

                    from data import INDEX_CACHE_DIR, _ticker_to_filename

                    _progress(f"Reading data · {ticker}/{granularity}…")
                    if start_date and end_date:
                        start_d = date.fromisoformat(start_date)
                        end_d = date.fromisoformat(end_date)
                    else:
                        cp = (
                            INDEX_CACHE_DIR
                            / f"{_ticker_to_filename(ticker)}_{granularity}.parquet"
                        )
                        if cp.exists():
                            _df2 = _pd2.read_parquet(cp)
                            start_d = (
                                _df2.index[0].date()
                                if not _df2.empty
                                else date(2000, 1, 1)
                            )
                            end_d = (
                                _df2.index[-1].date()
                                if not _df2.empty
                                else date.today()
                            )
                        else:
                            start_d = date(2000, 1, 1)
                            end_d = date.today()
                except (ValueError, Exception):
                    _set_error("start_date and end_date must be YYYY-MM-DD.")
                    return
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

                if trade_size > 0:
                    import copy as _copy

                    run_spec = _copy.copy(run_spec)
                    run_spec.trade_size = trade_size

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "index",
                        "ticker": ticker,
                        "granularity": granularity,
                        "initial_capital": _cap,
                        "commission_bps_override": _comm_bps,
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
            # Session-scoped last-result (prevent torn read #8; match #23) — sid
            # was captured at request time (worker has no request context).
            _last_result_set(
                sid,
                r=result,
                spec_name=spec.name,
                narrative=narrative,
                bars_info=bars_info,
            )

            # Persist the FULL result view-model so the history tab can reload
            # this exact screen later (the jsonl log keeps only scalar metrics).
            if result.error is None:
                try:
                    _save_result_snapshot(
                        run_id,
                        _result_viewmodel(result, spec.name, narrative, bars_info),
                    )
                except Exception:
                    pass  # snapshot failure must not hide the result

            try:
                _log_backtest(
                    run_spec if "run_spec" in locals() else spec,
                    result,
                    instrument_kind,
                    bars_info,
                    elapsed_sec=_time.perf_counter() - _t_start,
                    run_id=run_id,
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


def _mark_gen_chained(gen_id: str) -> bool:
    """Atomic test-and-set: chain'in #result'a servis edildiğini işaretle.

    İlk çağrıda False döner (chain servis EDİLEBİLİR); sonraki çağrılarda True
    döner (zaten servis edildi → describe_progress 204 basıp #result'ı korur).
    done sonrası gelen gecikmeli/örtüşen poll'lerin backtest'i yeniden
    tetiklemesini önler. Kayıt yoksa True döner (yeni chain başlatma).
    """
    with _GEN_LOCK:
        raw = _GEN_PROGRESS.get(gen_id)
        if raw is None:
            return True
        if raw.get("chained"):
            return True
        raw["chained"] = True
        return False


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
    initial_capital: float = Form(0.0),
    commission_pct: float = Form(-1.0),
    trade_size: float = Form(0.0),
    trade_size_mode: str = Form("fixed"),
    trade_size_percent: float = Form(5.0),
    trade_size_atr_risk: float = Form(1.0),
    atr_period: int = Form(14),
    trade_size_vol_target: float = Form(0.02),
    trade_size_vol_span: int = Form(10),
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

    # Server-side date validation — same rule as /run (the generation would
    # otherwise burn LLM calls and then chain into a doomed backtest).
    date_err = (
        _invalid_date_range(bybit_start, bybit_end)
        or _invalid_date_range(start_date, end_date)
        or _invalid_date_range(ext_start, ext_end)
    )
    if date_err:
        return _date_error_response(date_err)

    # One live run per session — a second describe while generation/backtest is
    # in flight would reset the progress panel (see _session_active_kind).
    sid = session_id(request)
    busy = _session_active_kind(sid)
    if busy:
        return _busy_response(busy)

    # Short/sell direction: the Backtest form carries an allow_short checkbox
    # (defaults ON). An unchecked HTML checkbox sends nothing → "" → False.
    _allow_short = _parse_bool_form(allow_short)

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
        "initial_capital": initial_capital,
        "commission_pct": commission_pct,
        "trade_size": trade_size,
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
            # Chain (done → /run|/sweep) yalnızca BİR kez #result'a servis edilmeli.
            # done sonrası gelen gecikmeli/örtüşen bir poll chain div'ini tekrar
            # yayınlarsa backtest yeniden tetiklenip mevcut sonucu siler. İlk servis
            # bunu True yapar; sonraki poll'ler 204 döner (bkz. describe_progress).
            "chained": False,
        },
    )
    _session_set_active(sid, "gen", gen_id)

    def _worker() -> None:
        from agent import (
            GeneratedCodeError,
            propose_condition_breakdown,
            propose_custom_block,
        )
        from composer import (
            SignalBlock,
            append_to_catalog,
            build_spec,
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

            spec = build_spec(
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
                trade_size_mode=trade_size_mode,
                trade_size_percent=trade_size_percent,
                trade_size_atr_risk=trade_size_atr_risk,
                atr_period=atr_period,
                trade_size_vol_target=trade_size_vol_target,
                trade_size_vol_span=trade_size_vol_span,
                # vol_target uses a FIXED capital notional from the form's
                # Initial Capital (falls back to 10k when blank/0).
                trade_size_capital=(
                    initial_capital if initial_capital > 0 else 10000.0
                ),
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
        # In-memory kayıt yok → neredeyse her zaman server üretim sırasında
        # yeniden başladı (dev'de `uvicorn --reload`, izlenen bir .py değişince
        # worker thread + _GEN_PROGRESS uçar). Sessizce boş div basıp float
        # paneli yok etmek yerine, AYNI float panelde net bir mesaj bırak ve
        # polling'i durdur (fragment bu durumda hx-trigger üretmez).
        return templates.TemplateResponse(
            request,
            "fragments/describe_progress.html",
            {"gen_id": gen_id, "state": None, "done": True},
        )

    # Chain'i (done → /run|/sweep) SADECE BİR KEZ servis et. done olduktan sonra
    # HTMX normalde poll'ü durdurur, ama gecikmeli/örtüşen bir in-flight poll
    # (yavaş yanıt, geçmiş geri-yükleme, çift tetik) done fragmanını #result'a
    # tekrar yazarsa chain div `hx-trigger="load"` ile backtest'i İKİNCİ kez
    # tetikler ve o an #result'ta duran sonucu/ilerlemeyi siler ("sonuç gelip
    # kayboluyor"). İlk servis chained'i işaretler; sonraki poll'ler 204 döner —
    # HTMX 204'te innerHTML swap yapmaz, yani #result olduğu gibi korunur.
    if state["chain_vals"] is not None:
        already = _mark_gen_chained(gen_id)
        if already:
            return HTMLResponse(status_code=204)

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
    bt_pnl_pct: str = Form(""),
    bt_sharpe: str = Form(""),
    bt_max_dd: str = Form(""),
    bt_n_trades: str = Form(""),
    bt_win_rate: str = Form(""),
    bt_spec_name: str = Form(""),
    bt_best_tf: str = Form(""),
    rob_overfitting: str = Form(""),
    rob_verdict: str = Form(""),
    rob_wfo_eff: str = Form(""),
    rob_oos_sharpe: str = Form(""),
    rob_stability: str = Form(""),
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

    _allow_short_bool = _parse_bool_form(allow_short)

    # Backtest metriklerini topla — mevcutsa AI'ya beslenir
    _bt_metrics: dict | None = None
    if any([bt_pnl_pct, bt_sharpe, bt_n_trades]):
        _bt_metrics = {
            "pnl_pct": bt_pnl_pct,
            "sharpe": bt_sharpe,
            "max_dd": bt_max_dd,
            "n_trades": bt_n_trades,
            "win_rate": bt_win_rate,
            "spec_name": bt_spec_name,
            "best_tf": bt_best_tf,
        }

    # Robustness özeti (overfitting-farkındalık) — analiz yapıldıysa AI'ya beslenir.
    _robustness: dict | None = None
    if any([rob_overfitting, rob_verdict, rob_wfo_eff, rob_oos_sharpe]):
        _robustness = {
            "overfitting_score": rob_overfitting,
            "verdict": rob_verdict,
            "wfo_efficiency": rob_wfo_eff,
            "oos_sharpe": rob_oos_sharpe,
            "stability": rob_stability,
        }

    # TTL cache keyed on (desc, allow_short) with in-flight dedup, so concurrent
    # identical requests (multi-tab, un-debounced allow_short toggle) share one
    # LLM call instead of stampeding it.
    # Metrikler varsa cache key'e dahil et — farklı sonuçlar için ayrı cache.
    import time as _time

    # bd (block plan) is cached by (desc, allow_short) — independent of metrics.
    # refined_result is NEVER cached: user pressing the button again always wants
    # a fresh AI suggestion (especially after a new backtest with new metrics).
    _cache_key = (desc.lower(), _allow_short_bool)
    _cached = _PLAN_CACHE.get(_cache_key)
    if _cached and (_time.monotonic() - _cached["ts"]) < _PLAN_CACHE_TTL:
        bd, llm_ok = _cached["bd"], _cached["llm_ok"]
        # Always re-run refined even on bd cache hit — fresh metrics, fresh suggestion.
        try:
            from agent import propose_refined_description

            refined_result = await asyncio.to_thread(
                propose_refined_description, desc, _bt_metrics, _robustness
            )
        except Exception:
            refined_result = {"refined": desc, "notes": "", "suggestions": []}
    else:
        # If another request for this key is already computing, await it.
        _inflight = _PLAN_INFLIGHT.get(_cache_key)
        if _inflight is not None:
            bd, llm_ok, refined_result = await _inflight
        else:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            _PLAN_INFLIGHT[_cache_key] = fut
            try:
                from agent import _hint_indicators

                local_inds = _hint_indicators(desc)
                llm_ok = True
                try:
                    from agent import (
                        propose_condition_breakdown,
                        propose_refined_description,
                    )

                    bd, refined_result = await asyncio.gather(
                        asyncio.to_thread(propose_condition_breakdown, desc),
                        asyncio.to_thread(
                            propose_refined_description,
                            desc,
                            _bt_metrics,
                            _robustness,
                        ),
                        return_exceptions=True,
                    )
                    if isinstance(bd, BaseException):
                        raise bd
                    if isinstance(refined_result, BaseException):
                        refined_result = {
                            "refined": desc,
                            "notes": "",
                            "suggestions": [],
                        }
                except Exception:
                    llm_ok = False
                    bd = _local_fallback_breakdown(desc, local_inds)
                    refined_result = {"refined": desc, "notes": "", "suggestions": []}
                # Only cache bd on successful LLM call.
                if llm_ok:
                    _PLAN_CACHE[_cache_key] = {
                        "bd": bd,
                        "llm_ok": llm_ok,
                        "ts": _time.monotonic(),
                    }
                    if len(_PLAN_CACHE) > _PLAN_CACHE_MAX:
                        oldest = min(_PLAN_CACHE, key=lambda k: _PLAN_CACHE[k]["ts"])
                        _PLAN_CACHE.pop(oldest, None)
                fut.set_result((bd, llm_ok, refined_result))
            except BaseException as _e:
                fut.set_exception(_e)
                raise
            finally:
                _PLAN_INFLIGHT.pop(_cache_key, None)

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
            "refined": refined_result.get("refined") or desc,
            "refine_notes": refined_result.get("notes") or "",
            "suggestions": refined_result.get("suggestions") or [],
        },
    )


# ── Multi-turn "AI ile iyileştir" sohbeti ───────────────────────────────────
# İki mod bir sekmede: /backtest/plan (tek-atışlık, korunur) + /backtest/chat*
# (çok turlu). İkisi de aynı #refined-description textarea'sını ve "Blokları
# Oluştur" (/backtest/describe) akışını besler.


def _collect_chat_context(
    bt_pnl_pct: str,
    bt_sharpe: str,
    bt_max_dd: str,
    bt_n_trades: str,
    bt_win_rate: str,
    bt_spec_name: str,
    bt_best_tf: str,
    rob_overfitting: str,
    rob_verdict: str,
    rob_wfo_eff: str,
    rob_oos_sharpe: str,
    rob_stability: str,
) -> dict:
    """Backtest + robustness metriklerini chat_context sözlüğüne topla (plan_preview ile aynı mantık)."""
    bt_metrics: dict | None = None
    if any([bt_pnl_pct, bt_sharpe, bt_n_trades]):
        bt_metrics = {
            "pnl_pct": bt_pnl_pct,
            "sharpe": bt_sharpe,
            "max_dd": bt_max_dd,
            "n_trades": bt_n_trades,
            "win_rate": bt_win_rate,
            "spec_name": bt_spec_name,
            "best_tf": bt_best_tf,
        }
    robustness: dict | None = None
    if any([rob_overfitting, rob_verdict, rob_wfo_eff, rob_oos_sharpe]):
        robustness = {
            "overfitting_score": rob_overfitting,
            "verdict": rob_verdict,
            "wfo_efficiency": rob_wfo_eff,
            "oos_sharpe": rob_oos_sharpe,
            "stability": rob_stability,
        }
    return {"bt_metrics": bt_metrics, "robustness": robustness}


def _render_chat_thread(request: Request, conv: dict, conv_id: str):
    """chat_thread.html fragment'ini konuşma state'inden render et. Sadece user/assistant
    turn'leri gösterilir (context metrikleri ilk user mesajına gömülü, gizli tutulur)."""
    from server import templates

    bubbles = [
        {"role": m["role"], "content": m.get("display", m["content"])}
        for m in conv["messages"]
        if m.get("role") in ("user", "assistant")
    ]
    return templates.TemplateResponse(
        request,
        "fragments/chat_thread.html",
        {
            "conv_id": conv_id,
            "bubbles": bubbles,
            "refined": conv.get("last_refined") or "",
        },
    )


@router.post("/chat/new", response_class=HTMLResponse)
async def chat_new(
    request: Request,
    description: str = Form(""),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    ticker: str = Form(""),
    ext_instrument: str = Form(""),
    allow_short: str = Form(""),
    bt_pnl_pct: str = Form(""),
    bt_sharpe: str = Form(""),
    bt_max_dd: str = Form(""),
    bt_n_trades: str = Form(""),
    bt_win_rate: str = Form(""),
    bt_spec_name: str = Form(""),
    bt_best_tf: str = Form(""),
    rob_overfitting: str = Form(""),
    rob_verdict: str = Form(""),
    rob_wfo_eff: str = Form(""),
    rob_oos_sharpe: str = Form(""),
    rob_stability: str = Form(""),
):
    """Yeni bir sohbet başlat: conv_id üret, ilk user mesajını (metrikler gömülü) gönder,
    ilk asistan turn'ünü store'a yaz, chat_thread fragment'ini döndür."""
    import asyncio

    from server import templates

    desc = (description or "").strip()
    if len(desc) < 12:
        return templates.TemplateResponse(
            request, "fragments/chat_thread.html", {"stage": "prompt"}
        )

    context = _collect_chat_context(
        bt_pnl_pct,
        bt_sharpe,
        bt_max_dd,
        bt_n_trades,
        bt_win_rate,
        bt_spec_name,
        bt_best_tf,
        rob_overfitting,
        rob_verdict,
        rob_wfo_eff,
        rob_oos_sharpe,
        rob_stability,
    )

    from agent import _format_metrics_block, chat_refine

    # İlk user mesajı: modele metrik-gömülü, ekranda ham tarif (display).
    embedded = _format_metrics_block(desc, context["bt_metrics"], context["robustness"])
    first_user = {"role": "user", "content": embedded, "display": desc}

    reply = await asyncio.to_thread(chat_refine, [first_user], context)

    conv = {
        "messages": [
            first_user,
            {"role": "assistant", "content": reply["text"]},
        ],
        "context": context,
        "last_refined": reply.get("refined") or desc,
    }
    conv_id = _CHAT.new(conv)

    return _render_chat_thread(request, conv, conv_id)


@router.post("/chat", response_class=HTMLResponse)
async def chat_turn(
    request: Request,
    conv_id: str = Form(""),
    message: str = Form(""),
):
    """Mevcut sohbete bir kullanıcı mesajı ekle, AI yanıtını al, thread'i güncelle.

    conv_id süresi dolmuş/bilinmiyorsa nazik yeniden-başlat fragment'i döner.
    LLM çağrısı lock'suz yapılır (oku-kopyala → çağır → tekrar-al-ve-yaz).
    """
    import asyncio

    from server import templates

    msg = (message or "").strip()
    if not msg:
        # Boş mesaj — mevcut thread'i olduğu gibi geri ver (varsa).
        conv = _CHAT.get(conv_id)
        if conv is None:
            return templates.TemplateResponse(
                request, "fragments/chat_thread.html", {"stage": "expired"}
            )
        return _render_chat_thread(request, conv, conv_id)

    # Oku-kopyala (lock kısa süre tutulur) — ChatStore.get() bir kopya döndürür.
    conv = _CHAT.get(conv_id)
    if conv is None:
        return templates.TemplateResponse(
            request, "fragments/chat_thread.html", {"stage": "expired"}
        )
    history = list(conv["messages"])
    context = dict(conv["context"])

    user_turn = {"role": "user", "content": msg, "display": msg}
    from agent import chat_refine

    reply = await asyncio.to_thread(chat_refine, history + [user_turn], context)

    # Tekrar-al-ve-yaz — arada evict edilmiş olabilir (commit None dönerse expired).
    def _apply(c: dict) -> None:
        c["messages"].append(user_turn)
        c["messages"].append({"role": "assistant", "content": reply["text"]})
        if reply.get("refined"):
            c["last_refined"] = reply["refined"]

    conv = _CHAT.commit(conv_id, _apply)
    if conv is None:
        return templates.TemplateResponse(
            request, "fragments/chat_thread.html", {"stage": "expired"}
        )

    return _render_chat_thread(request, conv, conv_id)


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


def _sweep_row_metrics(metrics: dict, bars=None) -> dict:
    m = metrics or {}
    row = {k: m.get(k) for k in _SWEEP_METRIC_KEYS}
    if bars is not None and not bars.empty:
        try:
            fc = float(bars.iloc[0]["close"])
            lc = float(bars.iloc[-1]["close"])
            row["buy_hold_pct"] = (lc - fc) / fc * 100.0 if fc > 0 else None
        except Exception:
            row["buy_hold_pct"] = None
    else:
        row["buy_hold_pct"] = None
    return row


def _sweep_state_view(sweep_id: str) -> dict | None:
    with _SWEEP_LOCK:
        raw = _SWEEP_PROGRESS.get(sweep_id)
        if raw is None:
            return None
        return {
            "spec_id": raw["spec_id"],
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

    # Same guards as /run: no inverted date range, one live job per session.
    date_err = _invalid_date_range(bybit_start, bybit_end) or _invalid_date_range(
        start_date, end_date
    )
    if date_err:
        return _date_error_response(date_err)
    sid = session_id(request)
    busy = _session_active_kind(sid)
    if busy:
        return _busy_response(busy)

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
            "spec_id": spec.id,
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
    _session_set_active(sid, "sweep", sweep_id)

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
                        metrics=_sweep_row_metrics(result.metrics, bars),
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
                        metrics=_sweep_row_metrics(result.metrics, bars),
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
