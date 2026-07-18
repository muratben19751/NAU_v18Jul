"""MA-crossover and RSI mean-reversion Nautilus strategies.

Kept minimal on purpose: fixed trade size, one position at a time,
market orders. The LLM agent proposes numeric parameters only.

Wiki References
---------------
See: [[strategy_and_actor]], [[v1_to_v2_migration_lessons]]

StrategyConfig is v2 plain-class (see [[v1_to_v2_migration_lessons]]); Strategy services are the list on the [[strategy_and_actor]] page.
"""

from __future__ import annotations

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

    def _iid(self):
        """Return InstrumentId — resolves str if config was loaded via ImportableStrategyConfig."""
        iid = self.config.instrument_id
        return InstrumentId.from_str(iid) if isinstance(iid, str) else iid

    def _bt(self):
        """Return BarType — resolves str if config was loaded via ImportableStrategyConfig."""
        bt = self.config.bar_type
        return BarType.from_str(bt) if isinstance(bt, str) else bt

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self.subscribe_bars(self._bt())

    def on_bar(self, bar: Bar) -> None:
        self._closes.append(float(bar.close))
        if len(self._closes) < self.config.slow:
            return

        closes = list(self._closes)
        fast_ma = sum(closes[-self.config.fast :]) / self.config.fast
        slow_ma = sum(closes[-self.config.slow :]) / self.config.slow
        diff = fast_ma - slow_ma

        if self._prev_diff is None:
            self._prev_diff = diff
            return

        crossed_up = self._prev_diff <= 0 < diff
        crossed_down = self._prev_diff >= 0 > diff
        self._prev_diff = diff

        pos = self.cache.positions_open(instrument_id=self._iid())
        has_long = any(p.side.name == "LONG" for p in pos)
        has_short = any(p.side.name == "SHORT" for p in pos)

        qty = self.instrument.make_qty(float(self.config.trade_size))

        if crossed_up:
            if has_short:
                self.close_all_positions(self._iid())
            if not has_long:
                order = self.order_factory.market(
                    instrument_id=self._iid(),
                    order_side=OrderSide.BUY,
                    quantity=qty,
                )
                self.submit_order(order)
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

    def _iid(self):
        iid = self.config.instrument_id
        return InstrumentId.from_str(iid) if isinstance(iid, str) else iid

    def _bt(self):
        bt = self.config.bar_type
        return BarType.from_str(bt) if isinstance(bt, str) else bt

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self.subscribe_bars(self._bt())
        self.register_indicator_for_bars(self._bt(), self._rsi)

    def on_bar(self, bar: Bar) -> None:
        if not self._rsi.initialized:
            return

        pos = self.cache.positions_open(instrument_id=self._iid())
        has_long = any(p.side.name == "LONG" for p in pos)

        qty = self.instrument.make_qty(float(self.config.trade_size))

        # H7: Nautilus RSI.value ∈ [0,1); oversold/overbought on 0-100 scale
        # (30/70). The scale mismatch turned the strategy into a degenerate buy-hold
        # (value<30 always true, value>70 never). Scale to 0-100.
        rsi_100 = self._rsi.value * 100.0
        if rsi_100 < self.config.oversold and not has_long:
            order = self.order_factory.market(
                instrument_id=self._iid(),
                order_side=OrderSide.BUY,
                quantity=qty,
            )
            self.submit_order(order)
        elif rsi_100 > self.config.overbought and has_long:
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
