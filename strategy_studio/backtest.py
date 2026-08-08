"""Backtest adapters (STUDIO_SPEC Phase 3).

Two adapters share the ``BacktestAdapter`` protocol:

``StubBacktestAdapter``
    Deterministic fake engine: metrics derive from a hash of the compiled
    config, so changing any parameter changes the results — good enough to
    exercise the full UI loop (run → poll → metrics → equity curve) and for
    tests, useless for real decisions. Still the default; kept as the offline
    path (no market data, no engine) that the test suite runs on.

``NautilusBacktestAdapter``
    The real engine. ``to_nautilus(compiled)`` lowers a ``CompiledStrategy``
    onto a composer ``ComposedStrategySpec``, which ``run_composed_backtest``
    executes through NautilusTrader.

Swap them in ``web/routes/strategy_studio.py`` (``ADAPTER = ...``), or set
``STUDIO_BACKTEST=nautilus`` to select the real engine at import time.

Windows
-------
``run(compiled, window=...)`` restricts a run to a fractional slice of the
sample. That is what the walk-forward optimizer folds on: it asks for one
in-sample window and ``walkforward.folds`` out-of-sample windows instead of
letting each adapter invent its own split. A windowed run reports no fold
table of its own — the caller *is* the folding. The same rule gates
``per_instrument``: only a full (window-less) run keeps the per-sleeve
breakdown (``InstrumentMetrics`` rows incl. capped trade lists) that the
studio's per-symbol table renders.

What ``to_nautilus`` refuses to translate
-----------------------------------------
The composer spec cannot express a regime branch, a ranked allocation block,
or an indicator with no engine equivalent. Rather than silently dropping such
rules — which would return metrics for a *different* strategy than the one on
screen — ``to_nautilus`` raises ``UnsupportedStrategy`` naming the offending
rule ids. The studio surfaces that as a failed run.

Wiki References
---------------
Bkz: [[strategy_studio]], [[nau_guvenlik_dayaniklilik_duzeltmeleri]], [[backtesting_guide]], [[portfolio]]

``StubBacktestAdapter`` being the default everywhere is a known, load-bearing
tradeoff (see module docstring above) — but until 2026-08-08 nothing in the UI
told an operator which engine produced the numbers on screen. The route layer
(``web/routes/strategy_studio.py``, ``_ctx()``) now derives an
``engine_is_stub`` flag from ``isinstance(ADAPTER, StubBacktestAdapter)`` and
every template that renders a metric shows a "SİMÜLE" badge when it is set —
see [[strategy_studio]] for the full disclosure design.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .compiler import CompiledStrategy


@dataclass
class FoldMetrics:
    fold: int
    dsr: float
    sharpe: float
    max_dd_pct: float


@dataclass
class InstrumentMetrics:
    """One sleeve of a multi-instrument run — the engine already computes all
    of this per instrument; this dataclass is what keeps it from being blended
    away. ``sharpe`` follows the same per-trade-preferred convention as the
    aggregate, so a single-instrument run's row matches the headline strip;
    with several instruments the strip's Sharpe comes from the blended curve
    and the rows will not visually average to it."""

    symbol: str
    timeframe: str
    net_pnl_pct: float
    sharpe: float
    max_dd_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    equity_curve: list[float] = field(default_factory=list)  # normalized, ≤80 pts
    trades_detail: list[dict] = field(default_factory=list)  # last ≤100 trades
    date_from: str = ""  # ISO date of the sleeve's first bar ("" = unknown)
    date_to: str = ""  # ISO date of the sleeve's last bar


@dataclass
class BacktestMetrics:
    net_pnl_pct: float
    sharpe: float
    dsr: float
    max_dd_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    equity_curve: list[float] = field(default_factory=list)  # normalized, 1.0 start
    folds: list[FoldMetrics] = field(default_factory=list)
    per_instrument: list[InstrumentMetrics] = field(default_factory=list)
    # Backtest data window (min/max across sleeves, ISO dates; "" = unknown —
    # old stored runs deserialize to "" and the UI hides the range chip).
    date_from: str = ""
    date_to: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> BacktestMetrics:
        d = json.loads(raw)
        d["folds"] = [FoldMetrics(**f) for f in d.get("folds", [])]
        d["per_instrument"] = [
            InstrumentMetrics(**p) for p in d.get("per_instrument", [])
        ]
        return cls(**d)


@dataclass(frozen=True)
class Window:
    """A fractional slice of the sample, plus a leading purge.

    Fractions rather than dates: the adapter owns the sample (how many days it
    loads, at what timeframe), so only it can turn a fraction into rows. The
    optimizer decides the *geometry* — where in-sample ends, where each
    out-of-sample fold sits — without needing to know the bar count.

    ``embargo_bars`` rows are dropped from the *front* of the slice, so a
    position or an indicator state carried over the boundary cannot leak the
    previous window's information into this one.
    """

    start: float
    end: float
    embargo_bars: int = 0
    label: str = ""


class BacktestAdapter(Protocol):
    def run(
        self, compiled: CompiledStrategy, *, window: Window | None = None
    ) -> BacktestMetrics: ...


def _slice_dates(bars, date_range: tuple[str, str]):
    """Inclusive [from, to] day slice of a bars frame; either side may be "".

    Works with tz-aware and naive DatetimeIndex; synthetic frames without a
    DatetimeIndex (RangeIndex test fixtures) pass through unchanged.
    """
    import pandas as pd

    idx = getattr(bars, "index", None)
    if not isinstance(idx, pd.DatetimeIndex):
        return bars
    f, t = date_range
    if f:
        bars = bars[bars.index >= pd.Timestamp(f, tz=idx.tz)]
    if t:
        bars = bars[bars.index < pd.Timestamp(t, tz=idx.tz) + pd.Timedelta(days=1)]
    return bars


def _bars_span(bars) -> tuple[str, str]:
    """ISO date range of a bars frame; ("", "") when it has no DatetimeIndex.

    Tolerant on purpose: tests inject synthetic frames with RangeIndex, and a
    missing range must degrade to the hidden-chip state, not crash a run.
    """
    try:
        import pandas as pd

        idx = bars.index
        if not isinstance(idx, pd.DatetimeIndex) or len(idx) == 0:
            return "", ""
        return str(idx.min())[:10], str(idx.max())[:10]
    except Exception:
        return "", ""


class StubBacktestAdapter:
    """Deterministic fake engine. See module docstring."""

    N_BARS = 220  # equity curve points (fits the 260px sparkline budget)
    LOOKBACK_DAYS = 180  # mirrors NautilusBacktestAdapter's default window

    def _span(self) -> tuple[str, str]:
        """The notional data window a real run would load (now − lookback).

        Dates are the one non-deterministic stub output: they move with the
        clock, deliberately — they describe the window, not the fake metrics,
        and none of the rng draws depend on them.
        """
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC)
        start = end - timedelta(days=self.LOOKBACK_DAYS)
        return start.date().isoformat(), end.date().isoformat()

    def _seed(self, compiled: CompiledStrategy, window: Window | None = None) -> int:
        payload = json.dumps(
            {
                "instruments": compiled.instruments,
                "entry": [
                    (c.indicator, c.params, c.operator, c.target)
                    for c in compiled.entry.conditions
                ],
                "filters": [
                    (c.indicator, c.params, c.operator, c.target)
                    for c in compiled.entry.filters
                ],
                "exit": [
                    (c.indicator, c.params, c.operator, c.target)
                    for c in compiled.exit.conditions
                ],
                "risk": compiled.risk,
                "regime": bool(compiled.regime),
                "regime_else": (compiled.regime or {}).get("else"),
                "sub_entry": [
                    (c.indicator, c.params, c.operator, c.target)
                    for c in compiled.regime["else_strategy"]["entry"].conditions
                ]
                if compiled.regime and compiled.regime.get("else_strategy")
                else [],
                "allocation": compiled.allocation,
                # A window is part of the identity of a run: two folds of the
                # same config must not come back with identical metrics, or
                # every candidate would look perfectly stable across folds.
                "window": (window.start, window.end) if window else None,
            },
            sort_keys=True,
            default=str,
        )
        return int(hashlib.sha256(payload.encode()).hexdigest()[:12], 16)

    def run(
        self,
        compiled: CompiledStrategy,
        *,
        window: Window | None = None,
        date_range: tuple[str, str] | None = None,
    ) -> BacktestMetrics:
        rng = random.Random(self._seed(compiled, window))

        # plausible-looking drifted random walk
        drift = rng.uniform(-0.0004, 0.0016)
        vol = rng.uniform(0.004, 0.011)
        equity, peak, max_dd = [1.0], 1.0, 0.0
        for _ in range(self.N_BARS - 1):
            equity.append(equity[-1] * (1 + rng.gauss(drift, vol)))
            peak = max(peak, equity[-1])
            max_dd = max(max_dd, (peak - equity[-1]) / peak)

        net = (equity[-1] - 1) * 100
        rets = [equity[i + 1] / equity[i] - 1 for i in range(len(equity) - 1)]
        mean = sum(rets) / len(rets)
        std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) or 1e-9
        sharpe = mean / std * math.sqrt(252)
        dsr = max(0.0, min(0.99, sharpe / 2 + rng.uniform(-0.12, 0.08)))

        # A windowed run is one fold of somebody else's split; sub-folding it
        # again would report a fold table for a slice, not for the strategy.
        folds = 0 if window else compiled.walkforward.get("folds", 6)
        fold_metrics = [
            FoldMetrics(
                fold=i + 1,
                dsr=round(max(0.0, dsr + rng.uniform(-0.15, 0.1)), 2),
                sharpe=round(sharpe + rng.uniform(-0.4, 0.3), 2),
                max_dd_pct=round(-(max_dd * 100) + rng.uniform(-2, 2), 1),
            )
            for i in range(folds)
        ]

        # Per-instrument rows use a FRESH Random per instrument, derived from
        # the run seed — drawing from the shared `rng` here would shift every
        # draw above and break the stub's documented determinism contract.
        # An explicit date_range relabels the notional window (fake metrics
        # stay seed-deterministic — the range is display metadata here).
        span = (
            (date_range[0], date_range[1]) if date_range is not None else self._span()
        )
        per_inst: list[InstrumentMetrics] = []
        if window is None:
            for inst in compiled.instruments:
                key = f"{inst['symbol']}|{inst['timeframe']}"
                irng = random.Random(
                    self._seed(compiled, window)
                    ^ int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)
                )
                idrift = irng.uniform(-0.0004, 0.0016)
                ivol = irng.uniform(0.004, 0.011)
                icurve = [1.0]
                for _ in range(79):
                    icurve.append(icurve[-1] * (1 + irng.gauss(idrift, ivol)))
                ist = _curve_stats(icurve)
                per_inst.append(
                    InstrumentMetrics(
                        symbol=inst["symbol"],
                        timeframe=inst["timeframe"],
                        net_pnl_pct=round((icurve[-1] - 1) * 100, 2),
                        sharpe=round(ist.sharpe_ann, 2),
                        max_dd_pct=round(-ist.max_dd * 100, 2),
                        trades=irng.randint(20, 200),
                        win_rate_pct=round(irng.uniform(38, 58), 1),
                        profit_factor=round(irng.uniform(0.9, 1.9), 2),
                        equity_curve=[round(x, 5) for x in icurve],
                        trades_detail=[],  # stub has no trades; UI hides expander
                        date_from=span[0],
                        date_to=span[1],
                    )
                )

        return BacktestMetrics(
            net_pnl_pct=round(net, 1),
            sharpe=round(sharpe, 2),
            dsr=round(dsr, 2),
            max_dd_pct=round(-max_dd * 100, 1),
            trades=rng.randint(120, 600),
            win_rate_pct=round(rng.uniform(38, 58), 1),
            profit_factor=round(rng.uniform(0.9, 1.9), 2),
            equity_curve=[round(x, 5) for x in equity],
            folds=fold_metrics,
            per_instrument=per_inst,
            date_from=span[0],
            date_to=span[1],
        )


# ── Real engine: CompiledStrategy → composer spec → NautilusTrader ──────────


class UnsupportedStrategy(Exception):
    """The compiled strategy uses something the composer spec cannot express.

    Carries every reason at once so the UI can list them all instead of
    surfacing them one failed run at a time.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# Studio operator → engine direction. Only faithful mappings appear here; an
# operator missing for a given indicator raises rather than guessing.
_UP = ("crosses_above", "gt", "gte", "price_above")
_DOWN = ("crosses_below", "lt", "lte", "price_below")


def _target(cond, what: str) -> float:
    if cond.target is None or isinstance(cond.target, str):
        raise UnsupportedStrategy(
            [
                f"rule {cond.rule_id}: '{cond.indicator}' needs a numeric {what}, "
                f"got {cond.target!r}"
            ]
        )
    return float(cond.target)


def _direction(cond, up: str, down: str) -> str:
    if cond.operator in _UP:
        return up
    if cond.operator in _DOWN:
        return down
    raise UnsupportedStrategy(
        [
            f"rule {cond.rule_id}: operator '{cond.operator}' has no engine "
            f"equivalent for '{cond.indicator}'"
        ]
    )


def _b_rsi(c) -> tuple[str, dict]:
    return "rsi_threshold", {
        "period": int(c.params["len"]),
        "threshold": _target(c, "threshold"),
        "cross": _direction(c, "above", "below"),
    }


def _b_adx(c) -> tuple[str, dict]:
    # adx_threshold fires on ADX *above* its threshold; "below" has no encoding.
    if c.operator not in ("gt", "gte"):
        raise UnsupportedStrategy(
            [
                f"rule {c.rule_id}: adx_threshold only encodes 'ADX > threshold' "
                f"(got '{c.operator}')"
            ]
        )
    return "adx_threshold", {
        "period": int(c.params["len"]),
        "threshold": _target(c, "threshold"),
    }


def _b_macd(c) -> tuple[str, dict]:
    return "macd_cross", {
        "fast": int(c.params["fast"]),
        "slow": int(c.params["slow"]),
        "direction": _direction(c, "up", "down"),
    }


def _b_relative_volume(c) -> tuple[str, dict]:
    return "volume_spike", {
        "period": int(c.params["window"]),
        "mult": _target(c, "multiple"),
        "direction": _direction(c, "above", "below"),
    }


def _b_stochrsi(c) -> tuple[str, dict]:
    level = _target(c, "level")
    params = {"rsi_period": int(c.params["len"]), "stoch_period": int(c.params["len"])}
    # A cross up is read against the oversold band, a cross down against
    # overbought — the other band keeps the engine default.
    if _direction(c, "up", "down") == "up":
        params["oversold"] = level
    else:
        params["overbought"] = level
    return "stoch_rsi_cross", params


def _b_wavetrend(c) -> tuple[str, dict]:
    level = _target(c, "level")
    params = {"channel_len": int(c.params["n1"]), "avg_len": int(c.params["n2"])}
    if _direction(c, "up", "down") == "up":
        params["os_level"] = level
    else:
        params["ob_level"] = level
    return "wave_trend_cross", params


def _b_atr(c) -> tuple[str, dict]:
    return "atr_stop", {
        "period": int(c.params["len"]),
        "mult": _target(c, "multiple"),
    }


_BLOCK_FOR = {
    "rsi": _b_rsi,
    "adx": _b_adx,
    "macd": _b_macd,
    "relative_volume": _b_relative_volume,
    "stochrsi": _b_stochrsi,
    "wavetrend": _b_wavetrend,
    "atr": _b_atr,
}

# Declarative-only registry entries: no composer block, no silent substitute.
_NO_ENGINE_BLOCK = {
    "ema": "price-vs-EMA has no composer block (ma_cross/ema_cross need two lengths)",
    "nadaraya_watson": "no composer block",
    "funding_z": "no composer block (perp funding feed not wired)",
    "oi_z": "no composer block (open-interest feed not wired)",
    "cvd_divergence": "no composer block (trade-flow feed not wired)",
    "volume_profile": "no composer block",
    "time_stop": "the composer spec has no time-based exit (risk.time_stop_bars is refused too)",
    "session_filter": "no composer block",
}


def _lower_conditions(
    conds, role: str, reasons: list[str], timeframes: set[str] | None = None
) -> list:
    from composer import SignalBlock

    blocks = []
    for c in conds:
        # A rule pinned to another timeframe reads a different bar feed. The
        # composer spec has one feed per run (plus a trend filter that is not
        # a general per-rule mechanism), so lowering it onto the strategy
        # timeframe would silently evaluate a different condition.
        if c.timeframe and timeframes and {c.timeframe} != timeframes:
            reasons.append(
                f"rule {c.rule_id}: timeframe '{c.timeframe}' differs from the "
                f"instrument timeframe(s) {sorted(timeframes)} — the composer "
                "spec runs on a single bar feed"
            )
            continue
        why = _NO_ENGINE_BLOCK.get(c.indicator)
        if why:
            reasons.append(f"rule {c.rule_id}: '{c.indicator}' — {why}")
            continue
        builder = _BLOCK_FOR.get(c.indicator)
        if builder is None:
            reasons.append(f"rule {c.rule_id}: unknown indicator '{c.indicator}'")
            continue
        try:
            btype, params = builder(c)
        except UnsupportedStrategy as e:
            reasons.extend(e.reasons)
            continue
        except KeyError as e:  # a param the schema guarantees went missing
            reasons.append(f"rule {c.rule_id}: missing param {e}")
            continue
        blocks.append(SignalBlock(type=btype, role=role, params=params))
    return blocks


def to_nautilus(compiled: CompiledStrategy, *, initial_capital: float = 10_000.0):
    """Lower a CompiledStrategy onto a composer ``ComposedStrategySpec``.

    The spec carries no instrument — ``run_composed_backtest`` takes that
    separately, so one spec serves every instrument on the strategy.

    Risk translation:
      * stop — ``sl_type='atr'``, ``sl_value=stop_loss_atr_mult`` over
        ``atr_period=stop_loss_atr_len`` (bracket order).
      * take-profit — ``take_profit_r`` is an R multiple and one R *is* the ATR
        stop distance, so the ATR-denominated TP is
        ``take_profit_r * stop_loss_atr_mult``.
      * sizing — ``atr_target`` risks ``trade_size_atr_risk`` percent of equity
        per 1xATR of stop distance, while ``risk_per_trade_pct`` is the risk
        over the *whole* stop, hence the division by the stop multiple.

    Raises:
        UnsupportedStrategy: with every untranslatable element listed.
    """
    from composer import ComposedStrategySpec

    reasons: list[str] = []
    if compiled.regime:
        reasons.append(
            "regime block: the composer spec has no conditional branch "
            "(and no ELSE substrategy)"
        )
    if compiled.allocation:
        reasons.append(
            "allocation block: ranked/top-N allocation is not part of the composer spec"
        )

    # entry.filters are AND-ed against the entry conditions; the spec has a
    # single entry_logic for all entry blocks, so OR + filters is not faithful.
    if compiled.entry.filters and compiled.entry.match == "any":
        reasons.append(
            "entry match='any' combined with filters: the spec has one "
            "entry_logic for every entry block"
        )

    risk = compiled.risk
    # ComposedStrategy holds at most one position at a time (entries are gated
    # on portfolio.is_flat), so max_concurrent=1 is honoured and anything above
    # it would quietly run a different risk profile than the one on screen.
    if int(risk.get("max_concurrent", 1)) > 1:
        reasons.append(
            f"risk.max_concurrent={risk['max_concurrent']}: the composer "
            "strategy holds one position at a time"
        )
    # The `time_stop` *rule* is refused via _NO_ENGINE_BLOCK; the risk-block
    # field has no composer equivalent either, so it must not pass silently.
    if risk.get("time_stop_bars") is not None:
        reasons.append(
            f"risk.time_stop_bars={risk['time_stop_bars']}: the composer spec "
            "has no time-based exit"
        )

    timeframes = {i["timeframe"] for i in compiled.instruments}
    blocks = _lower_conditions(compiled.entry.conditions, "entry", reasons, timeframes)
    blocks += _lower_conditions(compiled.entry.filters, "entry", reasons, timeframes)
    blocks += _lower_conditions(compiled.exit.conditions, "exit", reasons, timeframes)

    if reasons:
        raise UnsupportedStrategy(reasons)

    stop_mult = float(risk["stop_loss_atr_mult"])
    entry_logic = (
        "AND" if (compiled.entry.match == "all" or compiled.entry.filters) else "OR"
    )

    return ComposedStrategySpec(
        id=f"studio-{compiled.strategy_id}-v{compiled.version}",
        name=f"{compiled.strategy_id} v{compiled.version}",
        description="Compiled by Strategy Studio",
        blocks=blocks,
        entry_logic=entry_logic,
        exit_logic="AND" if compiled.exit.match == "all" else "OR",
        use_bracket=True,
        sl_type="atr",
        sl_value=stop_mult,
        tp_type="atr",
        tp_value=float(risk["take_profit_r"]) * stop_mult,
        atr_period=int(risk["stop_loss_atr_len"]),
        trade_size_mode="atr_target",
        trade_size_atr_risk=float(risk["risk_per_trade_pct"]) / (stop_mult or 1.0),
        trade_size_capital=initial_capital,
    )


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probabilistic_sharpe(
    returns: list[float], sharpe: float, benchmark: float = 0.0
) -> float:
    """Probability that the true Sharpe exceeds ``benchmark``.

    ``benchmark=0`` is the Probabilistic Sharpe Ratio — what a *single* run can
    say, since deflating needs the number of trials the Sharpe was selected
    from and one run does not know it. Feed the optimizer's expected-maximum
    Sharpe as the benchmark and the same formula is the Deflated Sharpe Ratio
    (see ``optimizer.expected_max_sharpe``).

    ``sharpe`` and ``benchmark`` must be on the *per-observation* scale of
    ``returns`` — an annualized Sharpe against per-bar moments drives every
    value to 0.99.
    """
    n = len(returns)
    if n < 3 or not math.isfinite(sharpe) or not math.isfinite(benchmark):
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    skew = sum(((r - mean) / sd) ** 3 for r in returns) / n
    kurt = sum(((r - mean) / sd) ** 4 for r in returns) / n
    denom = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2
    if denom <= 0:
        return 0.0
    z = (sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return round(max(0.0, min(0.99, _norm_cdf(z))), 2)


# The single-run path reports the undeflated statistic; keeping the old name
# next to the call sites documents which of the two they mean.
_psr = probabilistic_sharpe


def _normalize(curve: list[float]) -> list[float]:
    """Absolute cash curve → 1.0-based curve (what the sparkline expects)."""
    if not curve or curve[0] == 0:
        return [1.0]
    base = float(curve[0])
    return [round(float(v) / base, 5) for v in curve]


@dataclass
class _CurveStats:
    """Statistics of a normalized equity curve.

    ``sharpe_ann`` is annualized for display; ``sharpe_step`` is the raw
    per-step ratio, which is what PSR needs — feeding it the annualized value
    would multiply z by √252 and drive every PSR to 0.99.
    """

    sharpe_ann: float = 0.0
    sharpe_step: float = 0.0
    max_dd: float = 0.0
    returns: list[float] = field(default_factory=list)


def _curve_stats(curve: list[float], annualization: float = 252.0) -> _CurveStats:
    """`annualization` is bars per year — the host engine's convention.

    Pass the engine's own ``annualization`` metric so a recomputed Sharpe sits
    on the same scale as the one the /backtest page shows for the same run.
    """
    if len(curve) < 2:
        return _CurveStats()
    rets = [curve[i + 1] / curve[i] - 1 for i in range(len(curve) - 1) if curve[i] != 0]
    if not rets:
        return _CurveStats()
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    step = (mean / sd) if sd > 0 else 0.0
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    return _CurveStats(
        sharpe_ann=step * math.sqrt(max(annualization, 1.0)),
        sharpe_step=step,
        max_dd=max_dd,
        returns=rets,
    )


def _downsample(curve: list[float], limit: int = 260) -> list[float]:
    """Thin a curve to what the sparkline can draw.

    A 180-day 1h run produces 4319 points — ~39 KB of JSON per run in the
    store, re-parsed on every page load, to fill a 260px wide `<path>`. Stats
    are computed on the full curve before this runs; only the stored/rendered
    copy is thinned. Endpoints are kept so the net P&L reads true.
    """
    if len(curve) <= limit:
        return curve
    step = (len(curve) - 1) / (limit - 1)
    thinned = [curve[round(i * step)] for i in range(limit - 1)]
    thinned.append(curve[-1])
    return thinned


def _finite(value, default: float = 0.0) -> float:
    """Engine metrics use NaN for 'undefined' (e.g. avg_win with no winners)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _trade_row(t: dict) -> dict:
    """Persisted subset of an engine trade dict (times are unix seconds).

    The full trade carries chart-marker and reason-join fields; only what the
    per-symbol trade table renders goes into the store blob.
    """
    return {
        "entry_time": int(t.get("entry_time") or 0),
        "exit_time": int(t.get("exit_time") or 0),
        "entry_price": _finite(t.get("entry_price")),
        "exit_price": _finite(t.get("exit_price")),
        "side": str(t.get("side") or ""),
        "pnl": _finite(t.get("pnl")),
        "dur_min": int(t.get("dur_min") or 0),
        "exit_reason": t.get("exit_reason"),
    }


class NautilusBacktestAdapter:
    """Runs a CompiledStrategy through the app's existing NautilusTrader runner.

    One backtest per active instrument, plus ``walkforward.folds`` slice runs
    for the fold table. Multi-instrument strategies are reported as an
    equal-weight blend of the per-instrument equity curves: each sleeve gets
    its own capital and there is no rebalancing, so this is a portfolio
    approximation, not a joint-capital run.

    ``bars_loader(symbol, timeframe) -> DataFrame`` is injectable; the default
    resolves the studio timeframe to a Bybit interval code and calls
    ``data.load_bybit_bars``. Loader results are memoized for ``BARS_TTL_S``
    so one sweep does not reload the same parquet hundreds of times.
    """

    BARS_TTL_S = 300.0

    def __init__(
        self,
        bars_loader=None,
        *,
        initial_capital: float = 10_000.0,
        lookback_days: int = 180,
    ):
        self._loader = bars_loader or self._default_loader
        self.initial_capital = initial_capital
        self.lookback_days = lookback_days
        self._bars_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._bars_lock = threading.Lock()

    @staticmethod
    def _interval_code(timeframe: str) -> str:
        from data import BYBIT_ALL_INTERVALS

        code = {label: raw for raw, label in BYBIT_ALL_INTERVALS}.get(timeframe)
        if code is None:
            raise UnsupportedStrategy(
                [
                    f"timeframe '{timeframe}' is not a Bybit interval "
                    f"({', '.join(label for _, label in BYBIT_ALL_INTERVALS)})"
                ]
            )
        return code

    @staticmethod
    def _is_external(symbol: str) -> bool:
        """Dotted ids (QQQ.NASDAQ) are external-catalog instruments.

        Same discriminator as `mutations.add_instrument` — the dot cannot
        occur in a Bybit symbol (validated alnum there).
        """
        return "." in symbol

    @classmethod
    def _external_granularity(cls, timeframe: str) -> str:
        from data import BYBIT_ALL_INTERVALS, EXTERNAL_GRAN_BY_BYBIT_CODE

        gran = EXTERNAL_GRAN_BY_BYBIT_CODE.get(cls._interval_code(timeframe))
        if gran is None:  # 30m/12h — add_instrument rejects these, belt+braces
            supported = [
                label
                for code, label in BYBIT_ALL_INTERVALS
                if code in EXTERNAL_GRAN_BY_BYBIT_CODE
            ]
            raise UnsupportedStrategy(
                [
                    f"external instruments support {', '.join(supported)} — "
                    f"not '{timeframe}'"
                ]
            )
        return gran

    def _recipe(self, symbol: str, timeframe: str) -> dict:
        """Ingredients the sandbox child needs to rebuild the instrument.

        Two shapes, discriminated exactly as the /backtest route does it:
        Bybit symbols get the linear kline recipe, dotted ids the external
        one (`sandbox._build_instrument_bar_type` handles both).
        """
        if self._is_external(symbol):
            return {
                "source": "external",
                "instrument_id": symbol,
                "granularity": self._external_granularity(timeframe),
                "initial_capital": self.initial_capital,
            }
        return {
            "symbol": symbol,
            "interval": self._interval_code(timeframe),
            "category": "linear",
            "initial_capital": self.initial_capital,
        }

    def _default_loader(self, symbol: str, timeframe: str):
        from datetime import UTC, datetime, timedelta

        from data import load_bybit_bars, load_external_bars

        end = datetime.now(UTC)
        start = end - timedelta(days=self.lookback_days)
        if self._is_external(symbol):
            # Same lookback window as the Bybit path: full-history minute
            # series run to 20+ years and a studio sweep re-runs the loader
            # for every candidate — the memo cache holds the sliced frame.
            return load_external_bars(
                symbol, self._external_granularity(timeframe), start=start, end=end
            )
        return load_bybit_bars(
            symbol,
            interval=self._interval_code(timeframe),
            start=start,
            end=end,
        )

    def load_bars(self, symbol: str, timeframe: str):
        """Loader result, memoized per (symbol, timeframe) for BARS_TTL_S.

        An optimizer sweep runs the same (symbol, timeframe) for every candidate
        and every fold; without this, each of those runs re-decoded the whole
        parquet cache and (on the default loader) re-hit Bybit and rewrote the
        cache file — hundreds of times for one sweep.

        The adapter is a module-level singleton in the studio routes, so the
        entry has to expire: a TTL well above one sweep and well below a trading
        session keeps a sweep internally consistent without pinning stale bars
        for the life of the server.
        """
        key = (symbol, timeframe)
        now = time.monotonic()
        with self._bars_lock:
            hit = self._bars_cache.get(key)
            if hit is not None and now - hit[0] < self.BARS_TTL_S:
                return hit[1]
        bars = self._loader(symbol, timeframe)
        with self._bars_lock:
            self._bars_cache[key] = (time.monotonic(), bars)
        return bars

    def _run_one(
        self, spec, bars, label: str, recipe: dict
    ) -> tuple[list[float], dict, list[dict]]:
        # Engine runs go through the sandbox child like every other engine call
        # in the app (agent, /backtest, robustness, loop). A Nautilus backtest
        # holds the GIL for its entire run, so calling run_composed_backtest
        # directly from this background task froze the whole web process —
        # an "Optimize" click could stall every HTMX poll for minutes, with no
        # timeout and no way to kill it.
        from sandbox import run_backtest_guarded

        result = run_backtest_guarded(
            spec,
            bars,
            recipe,
            iteration_id=0,
            rationale=f"studio:{label}",
            force_subprocess=True,
        )
        if result.error:
            raise RuntimeError(f"{label}: {result.error}")
        metrics = result.metrics or {}
        # Bar-level mark-to-market curve: the realized one only moves when a
        # trade closes (two points for a one-trade run), which makes both the
        # sparkline and every curve statistic meaningless.
        mtm = metrics.get("equity_curve_mtm")
        curve = [eq for _ts, eq in mtm] if mtm else result.equity_curve
        return _normalize(curve), metrics, list(getattr(result, "trades", None) or [])

    def _folds(
        self,
        spec,
        per_instrument_bars: list,
        n_folds: int,
        embargo_bars: int = 0,
        recipes: list[dict] | None = None,
    ) -> list[FoldMetrics]:
        """Sequential OOS slices, one row per fold.

        Each fold drops its first ``embargo_bars`` rows so a position (or an
        indicator state) carried across the boundary cannot leak the previous
        fold's information into this one. Every instrument is sliced the same
        way and the resulting curves are blended exactly as the headline
        metrics are, so the fold table and the headline describe the same
        portfolio — previously the table only ever reflected instrument #1.
        """
        folds: list[FoldMetrics] = []
        if not per_instrument_bars or n_folds < 1:
            return folds
        for i in range(n_folds):
            curves, sleeve = [], {}
            for n, bars in enumerate(per_instrument_bars):
                size = len(bars) // n_folds
                if size <= embargo_bars + 1:
                    continue
                chunk = bars.iloc[i * size + embargo_bars : (i + 1) * size]
                recipe = (recipes or [])[n] if recipes and n < len(recipes) else {}
                try:
                    # Trades are dropped on purpose: a per-fold trade list would
                    # multiply the store blob by fold count for no UI surface.
                    curve, m, _ = self._run_one(spec, chunk, f"fold{i + 1}", recipe)
                except UnsupportedStrategy:
                    raise  # a config problem is not a per-fold hiccup
                except Exception:  # noqa: BLE001 — a dead fold tells us nothing
                    continue
                curves.append(curve)
                if n == 0:
                    sleeve = m
            if not curves:
                continue
            width = min(len(c) for c in curves)
            blended = [sum(c[j] for c in curves) / len(curves) for j in range(width)]
            st = _curve_stats(blended, _finite(sleeve.get("annualization"), 252.0))
            single = sleeve if len(curves) == 1 else {}
            folds.append(
                FoldMetrics(
                    fold=i + 1,
                    dsr=_psr(st.returns, st.sharpe_step),
                    sharpe=round(
                        _finite(single.get("sharpe_per_trade"), st.sharpe_ann), 2
                    ),
                    max_dd_pct=round(
                        _finite(single.get("max_dd"), -st.max_dd) * 100, 1
                    ),
                )
            )
        return folds

    MIN_WINDOW_BARS = 30  # below this an engine run reports nothing usable

    def _slice(self, bars, window: Window):
        """Fractional slice of a bar frame, with the leading purge applied."""
        n = len(bars)
        lo = min(max(int(n * window.start) + window.embargo_bars, 0), n)
        hi = min(max(int(round(n * window.end)), lo), n)
        chunk = bars.iloc[lo:hi]
        if len(chunk) < self.MIN_WINDOW_BARS:
            raise RuntimeError(
                f"window {window.label or f'{window.start:.2f}-{window.end:.2f}'}: "
                f"{len(chunk)} bars after a {window.embargo_bars}-bar embargo, "
                f"under the {self.MIN_WINDOW_BARS}-bar minimum"
            )
        return chunk

    def run(
        self,
        compiled: CompiledStrategy,
        *,
        window: Window | None = None,
        date_range: tuple[str, str] | None = None,
    ) -> BacktestMetrics:
        spec = to_nautilus(compiled, initial_capital=self.initial_capital)

        curves: list[list[float]] = []
        sleeves: list[dict] = []
        trades = wins = 0
        gross_win = gross_loss = 0.0
        all_bars: list = []
        recipes: list[dict] = []
        per_inst: list[InstrumentMetrics] = []

        spans: list[tuple[str, str]] = []
        for inst in compiled.instruments:
            bars = self.load_bars(inst["symbol"], inst["timeframe"])
            if date_range is not None:
                bars = _slice_dates(bars, date_range)
                if len(bars) < self.MIN_WINDOW_BARS:
                    raise UnsupportedStrategy(
                        [
                            f"date range {date_range[0] or '…'} → "
                            f"{date_range[1] or '…'} leaves {len(bars)} bars for "
                            f"{inst['symbol']} {inst['timeframe']} — under the "
                            f"{self.MIN_WINDOW_BARS}-bar minimum"
                        ]
                    )
            if window is not None:
                bars = self._slice(bars, window)
            span = _bars_span(bars)
            spans.append(span)
            recipe = self._recipe(inst["symbol"], inst["timeframe"])
            recipes.append(recipe)
            curve, m, inst_trades = self._run_one(spec, bars, inst["symbol"], recipe)
            curves.append(curve)
            sleeves.append(m)
            all_bars.append(bars)
            n = int(m.get("n_trades") or 0)
            trades += n
            wins += int(round(_finite(m.get("win_rate")) * n))
            # profit_factor is a ratio, so it cannot be summed across sleeves —
            # rebuild it from gross P&L. avg_win/avg_loss are NaN when the side
            # never traded, hence _finite rather than `or 0.0` (NaN is truthy).
            sleeve_gw = _finite(m.get("avg_win")) * int(m.get("n_wins") or 0)
            sleeve_gl = abs(_finite(m.get("avg_loss"))) * int(m.get("n_losses") or 0)
            gross_win += sleeve_gw
            gross_loss += sleeve_gl
            if window is None:
                # Same skip rule as `folds`: a windowed run is one optimizer
                # trial slice, and per-sleeve detail there would bloat every
                # trial's store blob for a table nobody renders.
                sst = _curve_stats(curve, _finite(m.get("annualization"), 252.0))
                per_inst.append(
                    InstrumentMetrics(
                        symbol=inst["symbol"],
                        timeframe=inst["timeframe"],
                        net_pnl_pct=round((curve[-1] - 1) * 100, 2),
                        sharpe=round(
                            _finite(m.get("sharpe_per_trade"), sst.sharpe_ann), 2
                        ),
                        max_dd_pct=round(
                            _finite(m.get("max_dd"), -sst.max_dd) * 100, 2
                        ),
                        trades=n,
                        win_rate_pct=round(_finite(m.get("win_rate")) * 100, 1),
                        profit_factor=round(sleeve_gw / sleeve_gl, 2)
                        if sleeve_gl
                        else 0.0,
                        equity_curve=[
                            round(v, 5) for v in _downsample(curve, limit=80)
                        ],
                        trades_detail=[_trade_row(t) for t in inst_trades[-100:]],
                        date_from=span[0],
                        date_to=span[1],
                    )
                )

        if not curves:
            raise UnsupportedStrategy(["strategy has no active instruments"])

        # Equal-weight blend: shortest sleeve bounds the common length.
        width = min(len(c) for c in curves)
        blended = [sum(c[i] for c in curves) / len(curves) for i in range(width)]
        st = _curve_stats(blended, _finite(sleeves[0].get("annualization"), 252.0))

        # Drawdown: the engine's MTM figure (bar-resolution, so it catches dips
        # the realized curve never sees). Blended sleeves have no engine-side
        # equivalent, hence the recomputation fallback.
        #
        # Sharpe: `sharpe_per_trade` rather than the engine's bar-frequency
        # `sharpe`, deliberately. Bar-frequency Sharpe counts every flat bar as
        # a zero return, so a strategy that sits out of the market most of the
        # time gets a deflated denominator — the same run reads 6.02
        # bar-frequency against 0.51 per-trade. The studio *ranks* strategies
        # (optimizer objective, deploy gate), and that bias would systematically
        # favour rarely-trading ones. Swap this line for `single.get("sharpe")`
        # to mirror what the /backtest page shows instead.
        single = sleeves[0] if len(sleeves) == 1 else {}
        sharpe = _finite(single.get("sharpe_per_trade"), st.sharpe_ann)
        max_dd = _finite(single.get("max_dd"), -st.max_dd)  # negative fraction

        # A windowed run is already one fold of the optimizer's split; its own
        # fold table would describe sub-slices of a slice, and it would cost
        # `folds` extra engine runs per candidate per fold.
        wf = compiled.walkforward
        folds = (
            []
            if window is not None
            else self._folds(
                spec,
                all_bars,
                int(wf.get("folds", 6)),
                embargo_bars=int(wf.get("embargo_bars", 0) or 0),
                recipes=recipes,
            )
        )

        froms = sorted(s[0] for s in spans if s[0])
        tos = sorted(s[1] for s in spans if s[1])
        return BacktestMetrics(
            net_pnl_pct=round((blended[-1] - 1) * 100, 2),
            sharpe=round(sharpe, 2),
            dsr=_psr(st.returns, st.sharpe_step),
            max_dd_pct=round(max_dd * 100, 2),
            trades=trades,
            win_rate_pct=round(wins / trades * 100, 1) if trades else 0.0,
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss else 0.0,
            equity_curve=_downsample(blended),
            folds=folds,
            per_instrument=per_inst,
            date_from=froms[0] if froms else "",
            date_to=tos[-1] if tos else "",
        )
