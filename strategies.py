"""MA-crossover and RSI mean-reversion Nautilus strategies.

Kept minimal on purpose: fixed trade size, one position at a time,
market orders. The LLM agent proposes numeric parameters only.

``VolTargetedTrendStrategy`` extends the pattern with dynamic position
sizing: ``size = (vol_target / ewma_vol) * capital / price``. ewma_vol is
maintained incrementally (O(1) per bar) but seeded to match
``calc_ewma_vol`` from [[indicators.py]] (first return at full weight),
so it agrees with the reference estimator after warmup.

Wiki References
---------------
See: [[strategy_and_actor]], [[v1_to_v2_migration_lessons]], [[webapp_module_map]]

StrategyConfig is v2 plain-class (see [[v1_to_v2_migration_lessons]]); Strategy services are the list on the [[strategy_and_actor]] page.
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from nautilus_trader.indicators import RelativeStrengthIndex
from nautilus_trader.model import Bar, BarType, InstrumentId
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.trading.strategy import Strategy, StrategyConfig


class MACrossoverConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast: int = 10
    slow: int = 30
    trade_size: Decimal = Decimal("1")


class MACrossoverStrategy(Strategy):
    def __init__(self, config: MACrossoverConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self._closes: deque[float] = deque(maxlen=max(config.fast, config.slow) + 5)
        self._prev_diff: float | None = None
        # Cached on on_start — avoids per-bar isinstance check
        self._iid_cache: InstrumentId | None = None
        self._bt_cache: BarType | None = None
        self._fixed_qty = None
        # Incremental running sums for O(1) MA per bar
        self._fast_sum: float = 0.0
        self._slow_sum: float = 0.0
        self._bar_count: int = 0

    def _iid(self):
        if self._iid_cache is None:
            iid = self.config.instrument_id
            self._iid_cache = (
                InstrumentId.from_str(iid) if isinstance(iid, str) else iid
            )
        return self._iid_cache

    def _bt(self):
        if self._bt_cache is None:
            bt = self.config.bar_type
            self._bt_cache = BarType.from_str(bt) if isinstance(bt, str) else bt
        return self._bt_cache

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self._fixed_qty = self.instrument.make_qty(float(self.config.trade_size))
        self.subscribe_bars(self._bt())

    def on_bar(self, bar: Bar) -> None:
        price = float(bar.close)
        buf = self._closes

        # Maintain incremental running sums: subtract evicted value, add new
        if len(buf) >= self.config.fast:
            self._fast_sum -= buf[-self.config.fast]
        if len(buf) >= self.config.slow:
            self._slow_sum -= buf[-self.config.slow]

        buf.append(price)
        self._fast_sum += price
        self._slow_sum += price

        # Bound float rounding drift: periodically recompute sums from the
        # deque (subtract-then-add never fully cancels over long runs).
        self._bar_count += 1
        if self._bar_count % 4096 == 0:
            snap = list(buf)
            self._fast_sum = math.fsum(snap[-self.config.fast :])
            self._slow_sum = math.fsum(snap[-self.config.slow :])

        if len(buf) < self.config.slow:
            return

        diff = self._fast_sum / self.config.fast - self._slow_sum / self.config.slow

        if self._prev_diff is None:
            self._prev_diff = diff
            return

        crossed_up = self._prev_diff <= 0 < diff
        crossed_down = self._prev_diff >= 0 > diff
        self._prev_diff = diff

        if not crossed_up and not crossed_down:
            return

        pos = self.cache.positions_open(instrument_id=self._iid())
        has_long = any(p.side.name == "LONG" for p in pos)
        has_short = any(p.side.name == "SHORT" for p in pos)

        if crossed_up:
            if has_short:
                self.close_all_positions(self._iid())
            if not has_long:
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=OrderSide.BUY,
                        quantity=self._fixed_qty,
                    )
                )
        elif crossed_down:
            if has_long:
                self.close_all_positions(self._iid())

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid())


class RSIMeanReversionConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    trade_size: Decimal = Decimal("1")


class RSIMeanReversionStrategy(Strategy):
    def __init__(self, config: RSIMeanReversionConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self._rsi = RelativeStrengthIndex(config.rsi_period)
        self._iid_cache: InstrumentId | None = None
        self._bt_cache: BarType | None = None
        self._fixed_qty = None

    def _iid(self):
        if self._iid_cache is None:
            iid = self.config.instrument_id
            self._iid_cache = (
                InstrumentId.from_str(iid) if isinstance(iid, str) else iid
            )
        return self._iid_cache

    def _bt(self):
        if self._bt_cache is None:
            bt = self.config.bar_type
            self._bt_cache = BarType.from_str(bt) if isinstance(bt, str) else bt
        return self._bt_cache

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self._fixed_qty = self.instrument.make_qty(float(self.config.trade_size))
        self.subscribe_bars(self._bt())
        self.register_indicator_for_bars(self._bt(), self._rsi)

    def on_bar(self, bar: Bar) -> None:
        if not self._rsi.initialized:
            return

        # H7: Nautilus RSI.value ∈ [0,1); oversold/overbought on 0-100 scale
        # (30/70). The scale mismatch turned the strategy into a degenerate buy-hold
        # (value<30 always true, value>70 never). Scale to 0-100.
        rsi_100 = self._rsi.value * 100.0

        if rsi_100 < self.config.oversold:
            pos = self.cache.positions_open(instrument_id=self._iid())
            if not any(p.side.name == "LONG" for p in pos):
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=OrderSide.BUY,
                        quantity=self._fixed_qty,
                    )
                )
        elif rsi_100 > self.config.overbought:
            pos = self.cache.positions_open(instrument_id=self._iid())
            if any(p.side.name == "LONG" for p in pos):
                self.close_all_positions(self._iid())

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid())


class VolTargetedTrendConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast: int = 10
    slow: int = 30
    vol_span: int = 10
    vol_target: float = 0.01
    capital: float = 10_000.0
    allow_short: bool = False
    trade_size: Decimal = Decimal("1")  # backtest.py setdefault uyumluluğu için


class VolTargetedTrendStrategy(Strategy):
    """MA crossover direction + EWMA vol-targeted position sizing.

    Position size = (vol_target / ewma_vol) * capital / price.
    ewma_vol is maintained incrementally (O(1)/bar) via self._ewma_var.
    When allow_short=True (requires MARGIN account in run_backtest),
    a down-cross opens a SHORT instead of just closing the long.

    MTM (mark-to-market) equity is snapshotted every ``_MTM_SAMPLE`` bars
    post-warmup — bar-resolution enough for backtest.py's max_dd / Sharpe /
    equity-curve, while cutting the per-bar ``portfolio.equity()`` cost.
    """

    # Snapshot MTM equity every N bars (post-warmup). N=1 → every bar (old
    # behavior); larger N trades intra-position drawdown resolution for speed.
    _MTM_SAMPLE: int = 5

    def __init__(self, config: VolTargetedTrendConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self._iid_cache: InstrumentId | None = None
        self._bt_cache: BarType | None = None
        self._prev_diff: float | None = None
        self._prev_close: float | None = None
        # Incremental EWMA variance state — O(1) per bar
        self._ewma_var: float = 0.0
        self._ewma_alpha: float = 2.0 / (config.vol_span + 1)
        self._vol_warmup: int = 0  # returns seen so far for warmup counter
        # Incremental running sums for MA
        _buf = max(config.fast, config.slow) + 5
        self._closes: deque[float] = deque(maxlen=_buf)
        self._fast_sum: float = 0.0
        self._slow_sum: float = 0.0
        # Throttled MTM equity snapshots (read by backtest.py via getattr)
        self._mtm_equity: list[float] = []
        self._mtm_ts: list[int] = []
        self._bar_count: int = 0

    def _iid(self) -> InstrumentId:
        if self._iid_cache is None:
            iid = self.config.instrument_id
            self._iid_cache = (
                InstrumentId.from_str(iid) if isinstance(iid, str) else iid
            )
        return self._iid_cache

    def _bt(self) -> BarType:
        if self._bt_cache is None:
            bt = self.config.bar_type
            self._bt_cache = BarType.from_str(bt) if isinstance(bt, str) else bt
        return self._bt_cache

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self.subscribe_bars(self._bt())

    def _vol_sized_qty(self, price: float) -> object:
        vol_est = math.sqrt(self._ewma_var) if self._ewma_var > 0 else None
        if (
            vol_est is not None
            and vol_est > 0
            and price > 0
            and self._vol_warmup >= self.config.vol_span
        ):
            size = (self.config.vol_target / vol_est) * self.config.capital / price
        else:
            size = float(self.config.trade_size)
        max_size = (
            0.95 * self.config.capital / price
            if price > 0
            else float(self.config.trade_size)
        )
        size = min(size, max_size)
        size = max(size, float(self.instrument.size_increment))
        return self.instrument.make_qty(size)

    def _snapshot_mtm(self, ts_ns: int) -> None:
        """Append a mark-to-market equity sample (throttled by _MTM_SAMPLE)."""
        try:
            eq = self.portfolio.equity(self._iid().venue)
            if eq is not None:
                self._mtm_equity.append(
                    float(eq.as_double() if hasattr(eq, "as_double") else eq)
                )
                self._mtm_ts.append(ts_ns)
        except Exception:
            pass

    def on_bar(self, bar: Bar) -> None:
        price = float(bar.close)
        buf = self._closes

        # Update incremental EWMA variance (O(1)). Seed the first observed
        # return at full weight (ewma = lr²) to match calc_ewma_vol; apply
        # alpha only from the second return onward.
        if self._prev_close is not None and self._prev_close > 0 and price > 0:
            lr = math.log(price / self._prev_close)
            if self._vol_warmup == 0:
                self._ewma_var = lr * lr
            else:
                self._ewma_var = (
                    self._ewma_alpha * lr * lr
                    + (1 - self._ewma_alpha) * self._ewma_var
                )
            self._vol_warmup += 1
        self._prev_close = price

        # Update incremental MA running sums
        if len(buf) >= self.config.fast:
            self._fast_sum -= buf[-self.config.fast]
        if len(buf) >= self.config.slow:
            self._slow_sum -= buf[-self.config.slow]
        buf.append(price)
        self._fast_sum += price
        self._slow_sum += price

        # Periodically recompute sums from the deque to bound float rounding
        # drift (subtract-then-add never fully cancels; over millions of bars a
        # near-zero crossover could otherwise flip sign).
        self._bar_count += 1
        if self._bar_count % 4096 == 0:
            snap = list(buf)
            self._fast_sum = math.fsum(snap[-self.config.fast :])
            self._slow_sum = math.fsum(snap[-self.config.slow :])

        if len(buf) < self.config.slow:
            return

        # Bar-resolution MTM snapshot (throttled) — post-warmup, before the
        # signal-only early return so the equity curve covers every sampled bar.
        if self._bar_count % self._MTM_SAMPLE == 0:
            self._snapshot_mtm(bar.ts_event)

        diff = self._fast_sum / self.config.fast - self._slow_sum / self.config.slow

        if self._prev_diff is None:
            self._prev_diff = diff
            return

        crossed_up = self._prev_diff <= 0 < diff
        crossed_down = self._prev_diff >= 0 > diff
        self._prev_diff = diff

        if not crossed_up and not crossed_down:
            return

        # positions_open only called when a signal fires
        pos = self.cache.positions_open(instrument_id=self._iid())
        has_long = any(p.side.name == "LONG" for p in pos)
        has_short = any(p.side.name == "SHORT" for p in pos)

        qty = self._vol_sized_qty(price)

        if crossed_up:
            if has_short:
                self.close_all_positions(self._iid())
            if not has_long:
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=OrderSide.BUY,
                        quantity=qty,
                    )
                )
        elif crossed_down:
            if has_long:
                self.close_all_positions(self._iid())
            if self.config.allow_short and not has_short:
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=OrderSide.SELL,
                        quantity=qty,
                    )
                )

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid())


STRATEGY_REGISTRY = {
    "ma_crossover": (MACrossoverStrategy, MACrossoverConfig),
    "rsi_mean_reversion": (RSIMeanReversionStrategy, RSIMeanReversionConfig),
    "vol_targeted_trend": (VolTargetedTrendStrategy, VolTargetedTrendConfig),
}

STRATEGY_PARAM_SPEC = {
    "ma_crossover": {
        "fast": {"type": "int", "range": [2, 50], "desc": "Fast MA period"},
        "slow": {"type": "int", "range": [10, 200], "desc": "Slow MA period (> fast)"},
        "_note": "Long-only: crossed_up → BUY, crossed_down → closes long; does not open short.",
    },
    "rsi_mean_reversion": {
        "rsi_period": {"type": "int", "range": [5, 30], "desc": "RSI lookback"},
        "oversold": {"type": "float", "range": [10.0, 40.0], "desc": "Buy threshold"},
        "overbought": {
            "type": "float",
            "range": [60.0, 90.0],
            "desc": "Sell threshold",
        },
        "_note": "Long-only: oversold → BUY, overbought → closes long; does not open short.",
    },
    "vol_targeted_trend": {
        "fast": {"type": "int", "range": [2, 50], "desc": "Fast MA period"},
        "slow": {"type": "int", "range": [10, 200], "desc": "Slow MA period (> fast)"},
        "vol_span": {
            "type": "int",
            "range": [5, 30],
            "desc": "EWMA span for vol estimate",
        },
        "vol_target": {
            "type": "float",
            "range": [0.001, 0.05],
            "desc": "Target daily vol fraction (0.01 = 1%)",
        },
        "capital": {
            "type": "float",
            "range": [1000.0, 100000.0],
            "desc": "Notional capital for position sizing",
        },
        "allow_short": {
            "type": "bool",
            "values": [True, False],
            "desc": "Open short when trend is down (requires MARGIN account)",
        },
        "_note": "MA crossover direction + EWMA vol-targeted position sizing. allow_short=True enables long+short.",
    },
}
