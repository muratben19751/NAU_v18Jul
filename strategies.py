"""MA-crossover and RSI mean-reversion Nautilus strategies.

Kept minimal on purpose: fixed trade size, one position at a time,
market orders. The LLM agent proposes numeric parameters only.

Vol-targeted position sizing (``size = (vol_target / ewma_vol) * capital /
price``) now lives in the composer engine as the ``vol_target`` trade-size
mode (see [[webapp_module_map]] and ``ComposedStrategy._compute_qty``), not as
a standalone strategy here.

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


STRATEGY_REGISTRY = {
    "ma_crossover": (MACrossoverStrategy, MACrossoverConfig),
    "rsi_mean_reversion": (RSIMeanReversionStrategy, RSIMeanReversionConfig),
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
}
