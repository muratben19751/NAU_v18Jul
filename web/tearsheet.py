"""Tear sheet — render model for the single-backtest performance overlay.

Every backtest listing in the app can open a tear sheet; the listings are backed
by three *different* stores, so this module takes the union of what they can
supply and normalises it:

===============  =======================================  ====================
source           store                                    identity
===============  =======================================  ====================
``log``          ``backtest_log.jsonl`` (web.shared)      ISO ``ts``
``session``      ``agent_sessions/<run_id>.jsonl``        run_id + event index
``strategy``     ``studio_runs`` table (strategy_studio)  studio run_id
===============  =======================================  ====================

The first two carry the full metrics dict *and* an equity curve, so their tear
sheets are complete. The third stores a deliberately smaller
``BacktestMetrics`` (normalised curve, no timestamps, no cost breakdown) —
sections that cannot be computed from it are dropped and the reason is stated
in ``notes`` rather than being filled with zeros.

Presentation lives in ``fragments/tearsheet.html``; this module stays pure so
the mapping is unit-testable (same split as [[auto_mission_control]]'s
``web/mission.py``).

Wiki References
---------------
See: [[tear_sheet_overlay]], [[webapp_module_map]]
"""

from __future__ import annotations

import math
from typing import Any

from web.viewmodels import fmt_dur, fmt_money, fmt_num, fmt_pct

# KPI grid layout. (key, label, formatter, tone) — tone "pnl" colours by sign,
# "bad" is always the loss colour (drawdown), "" is neutral.
_KPI_ROWS: list[tuple[str, str, str, str]] = [
    ("pnl", "Net PnL", "money_signed", "pnl"),
    ("pnl_pct", "Return", "pct", "pnl"),
    # Buy & hold sits next to Return because that is the only place the two
    # numbers mean anything: same instrument, same window. Neutral tone — the
    # benchmark is the yardstick, not a result to be graded.
    ("benchmark_return_fraction", "Buy & Hold", "pct", ""),
    ("excess_return_fraction", "vs Buy & Hold", "pct_signed", "pnl"),
    ("annualized_alpha", "Alpha (annual)", "pct_signed", "pnl"),
    ("sharpe", "Sharpe", "num3", "pnl"),
    ("sortino", "Sortino", "num3", "pnl"),
    ("profit_factor", "Profit Factor", "num2", "pf"),
    ("win_rate", "Win Rate", "pct1", ""),
    ("max_dd", "Max Drawdown", "pct", "bad"),
    ("volatility", "Volatility", "pct", ""),
    ("n_trades", "Trades", "int", ""),
    ("avg_duration_mins", "Avg Duration", "dur", ""),
    ("avg_win", "Avg Win", "money", "up"),
    ("avg_loss", "Avg Loss", "money", "down"),
    ("max_winner", "Best Trade", "money", "up"),
    ("max_loser", "Worst Trade", "money", "down"),
    ("commission_total", "Commission", "money", "bad"),
    ("slippage_total", "Slippage", "money", "bad"),
]

# The benchmark's own value for a KPI, rendered as a sub-line on that tile:
# {kpi key: (metrics key, formatter)}. Drawdown is the risk half of the same
# comparison — a strategy that beats buy & hold by suffering twice the drawdown
# has not beaten it, and putting the two numbers on one tile is what makes that
# visible without arithmetic.
_KPI_SUBS: dict[str, tuple[str, str]] = {
    "max_dd": ("benchmark_max_dd", "pct"),
}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))


def _fmt(kind: str, v: Any) -> str:
    if v is None or not _is_num(v):
        return "—"
    if kind == "money":
        return fmt_money(float(v))
    if kind == "money_signed":
        return fmt_money(float(v), signed=True)
    if kind == "pct":
        return fmt_pct(float(v))
    if kind == "pct_signed":
        # A difference is unreadable without its sign: "0.40%" beside a
        # benchmark could be either side of it, "+0.40%" cannot.
        return ("+" if v > 0 else "") + fmt_pct(float(v))
    if kind == "pct1":
        return fmt_pct(float(v), 1)
    if kind == "num2":
        return fmt_num(float(v), 2)
    if kind == "num3":
        return fmt_num(float(v), 3)
    if kind == "dur":
        return fmt_dur(float(v))
    if kind == "int":
        return f"{int(v):,}"
    return str(v)


def _tone(spec: str, v: Any) -> str:
    """CSS tone class for a KPI value."""
    if not _is_num(v):
        return ""
    if spec == "pnl":
        return "up" if v > 0 else ("down" if v < 0 else "")
    if spec == "pf":
        # 1.0 is break-even: below it the strategy loses money.
        return "up" if v > 1 else ("down" if v < 1 else "")
    if spec == "bad":
        # Drawdown / costs: any non-zero magnitude is a cost, never "good".
        return "down" if v else ""
    if spec in ("up", "down"):
        return spec
    return ""


def _kpis(metrics: dict) -> list[dict]:
    """KPI tiles, skipping metrics this source did not record.

    Dropping an absent key beats rendering "—" sixteen times: a thin source
    (Strategy Builder) then shows a short honest grid instead of a mostly
    empty one.
    """
    out = []
    for key, label, kind, tone in _KPI_ROWS:
        if key not in metrics:
            continue
        v = metrics.get(key)
        sub = ""
        bench_key, bench_kind = _KPI_SUBS.get(key, ("", ""))
        if bench_key and _is_num(metrics.get(bench_key)):
            sub = f"buy & hold {_fmt(bench_kind, metrics[bench_key])}"
        out.append(
            {
                "key": key,
                "label": label,
                "value": _fmt(kind, v),
                "tone": _tone(tone, v),
                "sub": sub,
            }
        )
    return out


def _benchmark_note(metrics: dict) -> str:
    """State what the buy & hold number is, or "" when there is none to state.

    The two legs are NOT the same measurement: ``Buy & Hold`` is a gross
    close-to-close return while ``Return`` is net of simulated costs, so the
    cumulative difference flatters the benchmark. ``Alpha (annual)`` is the leg
    that repairs both flaws (annualised, and the benchmark charged a round
    trip) — a reader who does not know which is which will read the wrong one.
    """
    if not _is_num(metrics.get("benchmark_return_fraction")):
        return ""
    note = (
        "Buy & hold is the gross close-to-close return of the same window; the "
        "strategy return is net of simulated costs, so the two are not on one "
        "cost basis. Alpha (annual) is: annualised, with a round trip charged "
        "to the benchmark"
    )
    div = metrics.get("benchmark_dividend_yield_annual")
    if _is_num(div) and div:
        return note + f" and a {fmt_pct(float(div))} annual dividend credited to it."
    # Default 0: an unadjusted price series undercounts buy & hold. Saying so
    # beats inventing a yield — the reader can price the gap themselves.
    return (
        note + " and no dividend credit — if the price series is not "
        "dividend-adjusted, buy & hold really returned more than shown."
    )


def _wins_losses(metrics: dict) -> str:
    w, losses, be = (
        metrics.get("n_wins"),
        metrics.get("n_losses"),
        metrics.get("n_breakeven"),
    )
    if not _is_num(w) or not _is_num(losses):
        return ""
    tail = f" · {int(be)} flat" if _is_num(be) and be else ""
    return f"{int(w)} win / {int(losses)} loss{tail}"


def tearsheet_view(
    *,
    title: str,
    metrics: dict | None,
    ident: list[tuple[str, str]] | None = None,
    equity: list[float] | None = None,
    equity_dates: list[str] | None = None,
    equity_mtm: list[list] | None = None,
    subtitle: str = "",
    notes: list[str] | None = None,
    back_label: str = "",
) -> dict:
    """Normalise one backtest into the tear sheet render model.

    ``equity_mtm`` is ``[[iso_ts, equity], …]`` — the bar-resolution
    mark-to-market series. When present it is the curve to draw: the reported
    max drawdown is computed from it, so drawing the realized step-curve
    instead would contradict the KPI right next to it.
    """
    metrics = dict(metrics or {})
    # The log embeds the curves inside metrics; a caller may also pass them
    # explicitly (session events keep them as siblings). Explicit wins.
    if equity_mtm is None:
        equity_mtm = metrics.get("equity_curve_mtm") or []
    if equity is None:
        equity = metrics.get("equity_curve_realized") or []
    # Curves are drawing data, not KPIs — keep them out of the tile grid.
    for k in ("equity_curve_realized", "equity_curve_mtm", "equity_curve"):
        metrics.pop(k, None)

    notes = list(notes or [])
    bench_note = _benchmark_note(metrics)
    if bench_note:
        notes.append(bench_note)
    dated = bool(equity_mtm) or bool(equity_dates)
    if not dated:
        notes.append(
            "Monthly returns need per-point timestamps; this record stores the "
            "equity curve without them."
        )

    pnl = metrics.get("pnl")
    if not _is_num(pnl):
        # A source without an absolute PnL (Strategy Builder keeps percentages)
        # still needs a direction for the chart colour.
        pnl = metrics.get("pnl_pct") or metrics.get("net_pnl_pct") or 0

    return {
        "title": title or "Backtest",
        "subtitle": subtitle,
        "ident": list(ident or []),
        "kpis": _kpis(metrics),
        "wins_losses": _wins_losses(metrics),
        "runner": metrics.get("runner") or "",
        "starting_cash": _fmt("money", metrics.get("starting_cash")),
        "has_starting_cash": _is_num(metrics.get("starting_cash")),
        "equity": list(equity or []),
        "equity_dates": list(equity_dates or []),
        "equity_mtm": list(equity_mtm or []),
        "has_equity": len(equity_mtm or []) > 1 or len(equity or []) > 1,
        "has_monthly": dated,
        "pnl": float(pnl) if _is_num(pnl) else 0.0,
        "notes": notes,
        "back_label": back_label,
        "error": "",
    }


def tearsheet_error(msg: str) -> dict:
    """Render model for "this backtest cannot be shown" — same shell, no data."""
    return {
        "title": "Tear sheet",
        "subtitle": "",
        "ident": [],
        "kpis": [],
        "wins_losses": "",
        "runner": "",
        "starting_cash": "—",
        "has_starting_cash": False,
        "equity": [],
        "equity_dates": [],
        "equity_mtm": [],
        "has_equity": False,
        "has_monthly": False,
        "pnl": 0.0,
        "notes": [],
        "back_label": "",
        "error": msg,
    }
