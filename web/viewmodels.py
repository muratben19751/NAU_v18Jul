"""Formatters that turn backend dataclasses into template-friendly dicts."""

from __future__ import annotations

import math

from state import IterationResult


def fmt_money(v: float | None, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if signed:
        sign = "-" if v < 0 else "+"
        return f"{sign}{abs(v):,.2f} USDT"
    return f"{v:,.2f} USDT"


def fmt_pct(v: float | None, decimals: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.{decimals}f}%"


def fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, float) and math.isinf(v):
        # profit_factor kayıpsız stratejide inf olur; "inf" yerine sembol
        return "∞" if v > 0 else "-∞"
    return f"{v:,.{digits}f}"


def fmt_dur(mins: float | None) -> str:
    """Format average duration in minutes → human readable."""
    if mins is None or (isinstance(mins, float) and math.isnan(mins)):
        return "—"
    if mins < 60:
        return f"{mins:.0f}m"
    if mins < 1440:
        return f"{mins / 60:.1f}h"
    return f"{mins / 1440:.1f}d"


def pnl_direction(v: float | None) -> str:
    if v is None or v == 0:
        return "flat"
    return "up" if v > 0 else "down"


def iteration_row(r: IterationResult) -> dict:
    m = r.metrics if r.error is None else {}
    pnl = m.get("pnl")
    sc = m.get("starting_cash", 10_000.0)
    return {
        "id": r.id,
        "strategy": r.strategy,
        "params": r.params,
        "error": r.error,
        "timestamp": r.timestamp.strftime("%H:%M:%S"),
        # PnL
        "pnl": pnl,
        "pnl_fmt": fmt_money(pnl, signed=True),
        "pnl_dir": pnl_direction(pnl),
        "pnl_pct_fmt": fmt_pct(m.get("pnl_pct"), 3),
        # Starting capital
        "starting_cash": sc,
        "starting_cash_fmt": fmt_money(sc),
        # Risk-adjusted
        "sharpe": m.get("sharpe"),
        "sharpe_fmt": fmt_num(m.get("sharpe"), 2),
        "sortino": m.get("sortino"),
        "sortino_fmt": fmt_num(m.get("sortino"), 2),
        "volatility_fmt": fmt_pct(m.get("volatility"), 2),
        "profit_factor_fmt": fmt_num(m.get("profit_factor"), 2),
        # Drawdown
        "max_dd": m.get("max_dd"),
        "max_dd_fmt": fmt_pct(m.get("max_dd"), 2),
        # Trade counts (None gelirse 0'a düş — #24)
        "n_trades": m.get("n_trades") or 0,
        "n_wins": m.get("n_wins") or 0,
        "n_losses": m.get("n_losses") or 0,
        "win_rate": m.get("win_rate"),
        "win_rate_fmt": fmt_pct(m.get("win_rate"), 2),
        # Per-trade stats
        "avg_win_fmt": fmt_money(m.get("avg_win"), signed=True),
        "avg_loss_fmt": fmt_money(m.get("avg_loss"), signed=True),
        "max_winner_fmt": fmt_money(m.get("max_winner"), signed=True),
        "max_loser_fmt": fmt_money(m.get("max_loser"), signed=True),
        "long_ratio_fmt": fmt_pct(m.get("long_ratio"), 0),
        "avg_dur_fmt": fmt_dur(m.get("avg_duration_mins")),
        # Costs
        "commission_fmt": fmt_money(m.get("commission_total")),
        "slippage_fmt": fmt_money(m.get("slippage_total")),
        # Trade markers for price chart
        "trades": r.trades,
        "bars_info": r.bars_info,
        # Which engine produced this (#17)
        "runner": m.get("runner", "BacktestEngine"),
        # L19: bar-çözünürlüklü MTM eğrisi [(iso_ts, eq)] — raporlanan
        # max_dd bu seriden gelir; UI varsa bunu çizer (realized fallback).
        "equity_curve_mtm": m.get("equity_curve_mtm") or [],
    }


def best_card(r: IterationResult | None) -> dict | None:
    if r is None:
        return None
    row = iteration_row(r)
    row["rationale"] = r.rationale
    row["equity_curve"] = r.equity_curve
    row["equity_dates"] = r.equity_dates
    return row


# ── Zaman çizelgesi (Gantt) render modeli ────────────────────────────────────
# Agent pipeline span'larını (bkz. agent_backtest._tl_begin) SVG bar'larına
# çevirir. Saf fonksiyonlar — template ve sessions replay ikisi de kullanır.

_TL_LANE_ORDER = [
    ("data", "VERİ"),
    ("llm", "LLM"),
    ("backtest", "BACKTEST"),
    ("robustness", "ROBUSTNESS"),
]
_TL_MIN_W_PCT = 0.35  # anlık işlemler tıklanabilir kalsın
# "Nice" tick aralığı merdiveni (saniye) — 4-8 tick hedefi.
_TL_TICK_LADDER = [10, 30, 60, 300, 900, 1800, 3600, 10800]
_TL_STATUS_GLYPH = {"ok": "✓", "fail": "✗", "warn": "⚠", "running": "●"}


def _fmt_dur_s(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def timeline_view(
    spans: list[dict],
    *,
    now: float | None = None,
    round_num: int | None = None,
) -> dict | None:
    """Span listesi → SVG render modeli. Eşleşen span yoksa None.

    ``round_num`` verilirse yalnız o turun span'ları (continuous mod).
    Açık span'lar (t1 None) ``now``'a kadar uzatılır; ``now`` None ise
    (tamamlanmış run) kapalı span'ların maksimumu kullanılır ve now-imleci
    çizilmez.
    """
    from datetime import UTC, datetime

    sel = [sp for sp in spans if round_num is None or sp.get("round", 1) == round_num]
    if not sel:
        return None

    t0 = min(sp["t0"] for sp in sel)
    closed_max = max((sp["t1"] for sp in sel if sp.get("t1")), default=None)
    if now is None:
        t1 = closed_max if closed_max is not None else t0 + 1.0
        now_clamped = None
    else:
        t1 = max(now, closed_max or t0)
        now_clamped = now
    dur = max(t1 - t0, 1e-6)

    def x_pct(t: float) -> float:
        return round(max(0.0, min(1.0, (t - t0) / dur)) * 100, 3)

    # Tick'ler
    tick_step = next((s for s in _TL_TICK_LADDER if dur / s <= 8), _TL_TICK_LADDER[-1])
    ticks = []
    tick_t = t0 - (t0 % tick_step) + tick_step
    while tick_t < t1 and len(ticks) < 12:
        ticks.append(
            {
                "x_pct": x_pct(tick_t),
                "label": datetime.fromtimestamp(tick_t, UTC).strftime("%H:%M:%S"),
            }
        )
        tick_t += tick_step

    lanes = []
    for lane_key, lane_label in _TL_LANE_ORDER:
        lane_spans = [sp for sp in sel if sp.get("lane") == lane_key]
        if not lane_spans:
            continue
        # Robustness: ana span'lar satır 0, sub'lar satır 1.
        rows: list[list[dict]] = [[], []] if lane_key == "robustness" else [[]]
        for sp in sorted(lane_spans, key=lambda s: s["t0"]):
            end = sp["t1"] if sp.get("t1") is not None else (now_clamped or t1)
            w = max(x_pct(end) - x_pct(sp["t0"]), _TL_MIN_W_PCT)
            status = sp.get("status", "ok")
            glyph = _TL_STATUS_GLYPH.get(status, "")
            meta = sp.get("meta") or {}
            title_bits = [f"{glyph} {sp.get('label', sp['key'])}"]
            title_bits.append(f"{_fmt_dur_s(end - sp['t0'])}")
            if meta.get("pnl") is not None:
                title_bits.append(f"PnL {meta['pnl']:+.2f}")
            if meta.get("n_bars"):
                title_bits.append(f"{meta['n_bars']:,} bar")
            bar = {
                "key": sp["key"],
                "x_pct": x_pct(sp["t0"]),
                "w_pct": round(min(w, 100.0 - x_pct(sp["t0"])), 3),
                "status": status,
                "label": sp.get("label", sp["key"]),
                "title": " · ".join(title_bits),
            }
            rows[1 if sp.get("sub") else 0].append(bar)
        rows = [r for r in rows if r]
        lanes.append({"key": lane_key, "label": lane_label, "rows": rows})

    if not lanes:
        return None
    return {
        "t0": t0,
        "t1": t1,
        "dur_label": _fmt_dur_s(dur),
        "ticks": ticks,
        "now_pct": None if now_clamped is None else x_pct(now_clamped),
        "lanes": lanes,
    }


def associate_steps(
    spans: list[dict],
    steps: list[dict],
    *,
    round_num: int | None = None,
) -> dict[str, list[dict]]:
    """span.key → span'ın [t0,t1] penceresine düşen adımlar.

    Adım ts'leri "HH:MM:SS" — span epoch'larının gününe bağlanır; gece-yarısı
    sarması için 12 saatten büyük negatif fark +86400 ile düzeltilir. Bir adım
    birden çok span penceresine düşerse en içteki (sub) / en geç başlayan kazanır.
    """
    from datetime import UTC, datetime

    sel = [sp for sp in spans if (round_num is None or sp.get("round", 1) == round_num)]
    if not sel or not steps:
        return {}

    day0 = datetime.fromtimestamp(min(sp["t0"] for sp in sel), UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day0_ts = day0.timestamp()

    def step_epoch(ts_str: str) -> float | None:
        try:
            h, m, s = (int(x) for x in ts_str.split(":"))
        except (ValueError, AttributeError):
            return None
        t = day0_ts + h * 3600 + m * 60 + s
        if t < min(sp["t0"] for sp in sel) - 43200:
            t += 86400  # gece-yarısı sarması
        return t

    # En içteki span kazansın: sub'lar önce, sonra geç başlayanlar.
    ordered = sorted(sel, key=lambda sp: (not sp.get("sub", False), -sp["t0"]))
    out: dict[str, list[dict]] = {}
    for st in steps:
        t = step_epoch(st.get("ts", ""))
        if t is None:
            continue
        for sp in ordered:
            end = sp["t1"] if sp.get("t1") is not None else float("inf")
            # 1s tolerans: step ts saniye çözünürlüklü.
            if sp["t0"] - 1.0 <= t <= end + 1.0:
                out.setdefault(sp["key"], []).append(st)
                break
    return out
