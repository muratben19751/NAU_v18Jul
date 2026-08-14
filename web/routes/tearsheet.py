"""GET /tearsheet — one backtest's performance overlay, from any store.

Every backtest listing in the app links here; ``src`` selects the resolver that
knows where that listing's rows live (see ``web/tearsheet.py`` for the table).
The route only *fetches and identifies*; normalising and formatting is the
render model's job, and drawing is the fragment's.

No re-run is involved: each store already persists the metrics and the equity
curve, so the overlay opens instantly. That is the whole reason this is a
separate read-only endpoint rather than a variant of ``/reports/detail``,
which deliberately replays the strategy in the sandbox.

Wiki References
---------------
See: [[tear_sheet_overlay]], [[webapp_module_map]]
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.shared import BACKTEST_LOG
from web.tearsheet import tearsheet_error, tearsheet_view
from web.templating import templates

router = APIRouter(tags=["tearsheet"])


def _render(request: Request, view: dict) -> HTMLResponse:

    return templates.TemplateResponse(request, "fragments/tearsheet.html", {"ts": view})


# ── source: backtest_log.jsonl (Reports, Studio run history, AUTO) ──────────


def _find_log_record(ts: str) -> dict | None:
    """The log line whose raw UTC ``ts`` matches, active file then archive.

    ``rotate_if_large`` moves the old file to ``.jsonl.1``; a report row older
    than the last rotation would otherwise be unopenable even though the data
    is still on disk.
    """
    for path in (BACKTEST_LOG, BACKTEST_LOG.with_suffix(".jsonl.1")):
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Cheap substring reject before the JSON parse — the file holds
                # embedded equity arrays, so parsing every line is expensive.
                if not line or f'"{ts}"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ts") == ts:
                    return rec
    return None


def _log_view(ts: str) -> dict:
    rec = _find_log_record(ts)
    if rec is None:
        return tearsheet_error(
            "This run is no longer in the backtest log (it may have been "
            "rotated out of the archive)."
        )
    if rec.get("error"):
        return tearsheet_error(f"This run ended with an error: {rec['error'][:300]}")

    spec = rec.get("spec") or {}
    bars = rec.get("bars") or {}
    sym = bars.get("symbol") or bars.get("ticker") or "—"
    tf = bars.get("interval") or bars.get("granularity") or "—"
    rng = " → ".join(x for x in (bars.get("start"), bars.get("end")) if x) or "—"
    ident = [
        ("Symbol", str(sym)),
        ("Timeframe", str(tf)),
        ("Range", rng),
        ("Bars", f"{bars['n_bars']:,}" if bars.get("n_bars") else "—"),
        ("Run at", (rec.get("ts") or "")[:19].replace("T", " ") + " UTC"),
    ]
    if rec.get("elapsed_sec") is not None:
        ident.append(("Took", f"{rec['elapsed_sec']:.1f} s"))
    if rec.get("rationale"):
        ident.append(("Origin", str(rec["rationale"])[:80]))

    return tearsheet_view(
        title=spec.get("name") or "Backtest",
        subtitle=f"{sym} · {tf}",
        metrics=rec.get("metrics") or {},
        ident=ident,
    )


# ── source: agent session log (AUTO iterations, Session Logs) ───────────────


def _session_events(run_id: str) -> list[dict]:
    from web.shared import SESSION_LOG_DIR

    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or '"backtest_result"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "backtest_result":
                out.append(ev)
    return out


def _session_view(run_id: str, index: int) -> dict:
    # Same guard as the session detail page: run_id reaches the filesystem.
    if len(run_id) != 8 or not all(c in "0123456789abcdef" for c in run_id):
        return tearsheet_error("Invalid session id.")
    events = _session_events(run_id)
    if not 0 <= index < len(events):
        return tearsheet_error("This iteration is not in the session log.")
    ev = events[index]
    if ev.get("error"):
        return tearsheet_error(f"This iteration failed: {str(ev['error'])[:300]}")

    bars = ev.get("bars_info") or {}
    sym = bars.get("symbol") or bars.get("ticker") or "—"
    tf = ev.get("interval") or bars.get("interval") or "—"
    ident = [
        ("Symbol", str(sym)),
        ("Timeframe", str(tf)),
        ("Iteration", f"#{ev.get('iteration', index) + 1}"),
        ("Round", str(ev.get("round", 1))),
        ("Session", run_id),
        ("Run at", (ev.get("ts") or "")[:19].replace("T", " ") + " UTC"),
    ]
    score = ev.get("score")
    if isinstance(score, (int, float)) and score > -1e9:
        ident.append(("Score", f"{score:.4f}"))

    return tearsheet_view(
        title=ev.get("spec_name") or "Iteration",
        subtitle=f"{sym} · {tf}",
        metrics=ev.get("metrics") or {},
        equity=ev.get("equity_curve") or [],
        equity_dates=ev.get("equity_dates") or [],
        ident=ident,
    )


# ── source: strategy_studio runs / AI suggestion trials ─────────────────────

# Strategy Builder's BacktestMetrics is a smaller shape than the composer's
# (percentages, a normalised curve, no cost breakdown). Mapped onto the shared
# KPI keys so one grid renders both; absent keys are simply not shown.
_STUDIO_KPI_MAP = {
    "net_pnl_pct": "pnl_pct",
    "sharpe": "sharpe",
    "max_dd_pct": "max_dd",
    "trades": "n_trades",
    "win_rate_pct": "win_rate",
    "profit_factor": "profit_factor",
}


def _studio_metrics(raw: str) -> tuple[dict, list[float]]:
    """BacktestMetrics JSON → (shared-key metrics, equity curve)."""
    d = json.loads(raw)
    out: dict = {}
    for src, dst in _STUDIO_KPI_MAP.items():
        if d.get(src) is None:
            continue
        v = float(d[src])
        # This store keeps percentages as whole numbers (12.5 = 12.5%); the
        # shared formatter expects fractions.
        out[dst] = v / 100 if dst in ("pnl_pct", "max_dd", "win_rate") else v
    return out, [float(x) for x in (d.get("equity_curve") or [])]


def _studio_view(*, run_id: str = "", suggestion_id: str = "") -> dict:
    from strategy_studio.store import StrategyStore

    store = StrategyStore()
    if run_id:
        row = store.get_run(run_id)
        if row is None:
            return tearsheet_error("Run not found.")
        if row.get("error"):
            return tearsheet_error(f"This run failed: {str(row['error'])[:300]}")
        raw, label, sid = row.get("metrics"), f"Run {run_id[:8]}", row["strategy_id"]
        when = row.get("finished_at") or row.get("created_at") or ""
        extra = [("Version", f"v{row.get('version', '?')}")]
    else:
        row = store.get_suggestion(suggestion_id)
        if row is None:
            return tearsheet_error("Suggestion not found.")
        raw = row.get("trial_metrics")
        label = f"Trial {suggestion_id[:8]}"
        sid = row.get("strategy_id", "")
        when = row.get("created_at") or ""
        extra = [("Decision", str(row.get("status", "—")))]
    if not raw:
        return tearsheet_error(
            "This run stored no metrics — it never completed a backtest."
        )

    try:
        metrics, curve = _studio_metrics(raw)
        window = json.loads(raw)
    except ValueError:
        return tearsheet_error("Stored metrics could not be read.")

    rng = (
        " → ".join(x for x in (window.get("date_from"), window.get("date_to")) if x)
        or "—"
    )
    ident = [
        ("Strategy", str(sid)),
        ("Range", rng),
        ("Run at", str(when)[:19].replace("T", " ") + " UTC"),
        *extra,
    ]
    return tearsheet_view(
        title=label,
        subtitle="Strategy Builder",
        metrics=metrics,
        equity=curve,
        ident=ident,
        notes=[
            "Strategy Builder stores a normalised equity curve and percentage "
            "metrics — absolute PnL, costs and trade extremes are not recorded "
            "for these runs."
        ],
    )


# ── route ───────────────────────────────────────────────────────────────────


@router.get("/tearsheet", response_class=HTMLResponse)
async def tearsheet(
    request: Request,
    src: str = "log",
    ts: str = "",
    run_id: str = "",
    i: int = 0,
    id: str = "",
):
    """Tear sheet fragment for one backtest. Read-only; never re-runs anything.

    Disk reads run in a thread: the log holds embedded equity arrays and the
    overlay can be opened while a 1 s AUTO poll is in flight.
    """
    if src == "log":
        if not ts:
            return _render(request, tearsheet_error("Missing run timestamp."))
        view = await asyncio.to_thread(_log_view, ts)
    elif src == "session":
        view = await asyncio.to_thread(_session_view, run_id, i)
    elif src == "strategy":
        view = await asyncio.to_thread(_studio_view, run_id=run_id)
    elif src == "suggestion":
        view = await asyncio.to_thread(_studio_view, suggestion_id=id)
    else:
        view = tearsheet_error(f"Unknown tear sheet source: {src}")
    return _render(request, view)
