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

What ``to_nautilus`` refuses to translate
-----------------------------------------
The composer spec cannot express a regime branch, a ranked allocation block,
or an indicator with no engine equivalent. Rather than silently dropping such
rules — which would return metrics for a *different* strategy than the one on
screen — ``to_nautilus`` raises ``UnsupportedStrategy`` naming the offending
rule ids. The studio surfaces that as a failed run.

Wiki References
---------------
Bkz: [[strategy_studio]], [[backtesting_guide]], [[portfolio]]
"""

from __future__ import annotations

import hashlib
import json
import math
import random
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

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> BacktestMetrics:
        d = json.loads(raw)
        d["folds"] = [FoldMetrics(**f) for f in d.get("folds", [])]
        return cls(**d)


class BacktestAdapter(Protocol):
    def run(self, compiled: CompiledStrategy) -> BacktestMetrics: ...


class StubBacktestAdapter:
    """Deterministic fake engine. See module docstring."""

    N_BARS = 220  # equity curve points (fits the 260px sparkline budget)

    def _seed(self, compiled: CompiledStrategy) -> int:
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
            },
            sort_keys=True,
            default=str,
        )
        return int(hashlib.sha256(payload.encode()).hexdigest()[:12], 16)

    def run(self, compiled: CompiledStrategy) -> BacktestMetrics:
        rng = random.Random(self._seed(compiled))

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

        folds = compiled.walkforward.get("folds", 6)
        fold_metrics = [
            FoldMetrics(
                fold=i + 1,
                dsr=round(max(0.0, dsr + rng.uniform(-0.15, 0.1)), 2),
                sharpe=round(sharpe + rng.uniform(-0.4, 0.3), 2),
                max_dd_pct=round(-(max_dd * 100) + rng.uniform(-2, 2), 1),
            )
            for i in range(folds)
        ]

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


def _lower_conditions(conds, role: str, reasons: list[str],
                      timeframes: set[str] | None = None) -> list:
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
                "spec runs on a single bar feed")
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


def _psr(returns: list[float], sharpe: float) -> float:
    """Probabilistic Sharpe Ratio against a zero benchmark.

    This is the *undeflated* statistic: the studio calls the field ``dsr``, but
    deflating needs the number of trials the Sharpe was selected from, which a
    single run does not know. PSR is DSR's one-trial case, so it is an upper
    bound on the deflated value — the deploy gate therefore errs optimistic,
    not conservative. Wire the trial count through the optimizer to tighten it.
    """
    n = len(returns)
    if n < 3 or not math.isfinite(sharpe):
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
    z = sharpe * math.sqrt(n - 1) / math.sqrt(denom)
    return round(max(0.0, min(0.99, _norm_cdf(z))), 2)


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
    return _CurveStats(sharpe_ann=step * math.sqrt(max(annualization, 1.0)),
                       sharpe_step=step, max_dd=max_dd, returns=rets)


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


class NautilusBacktestAdapter:
    """Runs a CompiledStrategy through the app's existing NautilusTrader runner.

    One backtest per active instrument, plus ``walkforward.folds`` slice runs
    for the fold table. Multi-instrument strategies are reported as an
    equal-weight blend of the per-instrument equity curves: each sleeve gets
    its own capital and there is no rebalancing, so this is a portfolio
    approximation, not a joint-capital run.

    ``bars_loader(symbol, timeframe) -> DataFrame`` is injectable; the default
    resolves the studio timeframe to a Bybit interval code and calls
    ``data.load_bybit_bars``.
    """

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

    def _default_loader(self, symbol: str, timeframe: str):
        from datetime import UTC, datetime, timedelta

        from data import BYBIT_ALL_INTERVALS, load_bybit_bars

        code = {label: raw for raw, label in BYBIT_ALL_INTERVALS}.get(timeframe)
        if code is None:
            raise UnsupportedStrategy(
                [
                    f"timeframe '{timeframe}' is not a Bybit interval "
                    f"({', '.join(label for _, label in BYBIT_ALL_INTERVALS)})"
                ]
            )
        end = datetime.now(UTC)
        return load_bybit_bars(
            symbol,
            interval=code,
            start=end - timedelta(days=self.lookback_days),
            end=end,
        )

    def _run_one(self, spec, bars, label: str) -> tuple[list[float], dict]:
        from backtest import run_composed_backtest

        result = run_composed_backtest(
            spec,
            bars,
            iteration_id=0,
            rationale=f"studio:{label}",
            initial_capital=self.initial_capital,
        )
        if result.error:
            raise RuntimeError(f"{label}: {result.error}")
        metrics = result.metrics or {}
        # Bar-level mark-to-market curve: the realized one only moves when a
        # trade closes (two points for a one-trade run), which makes both the
        # sparkline and every curve statistic meaningless.
        mtm = metrics.get("equity_curve_mtm")
        curve = [eq for _ts, eq in mtm] if mtm else result.equity_curve
        return _normalize(curve), metrics

    def _folds(self, spec, per_instrument_bars: list, n_folds: int,
               embargo_bars: int = 0) -> list[FoldMetrics]:
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
                try:
                    curve, m = self._run_one(spec, chunk, f"fold{i + 1}")
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
                    max_dd_pct=round(_finite(single.get("max_dd"), -st.max_dd) * 100, 1),
                )
            )
        return folds

    def run(self, compiled: CompiledStrategy) -> BacktestMetrics:
        spec = to_nautilus(compiled, initial_capital=self.initial_capital)

        curves: list[list[float]] = []
        sleeves: list[dict] = []
        trades = wins = 0
        gross_win = gross_loss = 0.0
        all_bars: list = []

        for inst in compiled.instruments:
            bars = self._loader(inst["symbol"], inst["timeframe"])
            curve, m = self._run_one(spec, bars, inst["symbol"])
            curves.append(curve)
            sleeves.append(m)
            all_bars.append(bars)
            n = int(m.get("n_trades") or 0)
            trades += n
            wins += int(round(_finite(m.get("win_rate")) * n))
            # profit_factor is a ratio, so it cannot be summed across sleeves —
            # rebuild it from gross P&L. avg_win/avg_loss are NaN when the side
            # never traded, hence _finite rather than `or 0.0` (NaN is truthy).
            gross_win += _finite(m.get("avg_win")) * int(m.get("n_wins") or 0)
            gross_loss += abs(_finite(m.get("avg_loss"))) * int(m.get("n_losses") or 0)

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

        wf = compiled.walkforward
        folds = self._folds(
            spec,
            all_bars,
            int(wf.get("folds", 6)),
            embargo_bars=int(wf.get("embargo_bars", 0) or 0),
        )

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
        )
