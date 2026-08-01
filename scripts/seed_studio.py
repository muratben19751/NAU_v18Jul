"""Seed the studio's demo strategies.

Two fixtures, for two different jobs:

``wt-funding-v3``
    Matches the design mockup — regime branch, funding z-score, price-vs-EMA,
    time stop. It exercises the full UI surface, but ``to_nautilus`` refuses
    it (none of those have a composer block), so it only runs on the stub.

``rsi-adx-btc``
    The engine-runnable one: every rule maps to a composer block, no regime,
    no allocation, one Bybit instrument. Use this to see real Nautilus metrics
    with ``STUDIO_BACKTEST=nautilus``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_studio.schema import (
    InstrumentConfig,
    OptimizeRange,
    Param,
    RegimeBranch,
    RiskBlock,
    Rule,
    RuleGroup,
    StrategyDefinition,
    WalkForwardConfig,
)
from strategy_studio.store import StrategyStore


def build_fixture() -> StrategyDefinition:
    return StrategyDefinition(
        id="wt-funding-v3",
        name="WT-Funding Confluence v3",
        regime=RegimeBranch(
            conditions=RuleGroup(
                match="all",
                rules=[
                    Rule(
                        indicator="ema",
                        params={"len": Param(value=200)},
                        operator="price_above",
                        timeframe="1h",
                    ),
                    Rule(
                        indicator="adx",
                        params={"len": Param(value=14)},
                        operator="gt",
                        target=Param(
                            value=20, optimize=OptimizeRange(min=15, step=5, max=30)
                        ),
                    ),
                ],
            ),
            evaluate="every_bar",
            **{"else": "flat"},
        ),
        entry=RuleGroup(
            match="all",
            rules=[
                Rule(
                    indicator="wavetrend",
                    params={
                        "n1": Param(
                            value=10, optimize=OptimizeRange(min=6, step=2, max=14)
                        ),
                        "n2": Param(
                            value=21, optimize=OptimizeRange(min=15, step=3, max=27)
                        ),
                    },
                    operator="crosses_above",
                    target=Param(value=-53),
                ),
                Rule(
                    indicator="funding_z",
                    params={"lookback": Param(value=96)},
                    operator="lt",
                    target=Param(
                        value=-1.5,
                        optimize=OptimizeRange(min=-2.5, step=0.25, max=-1.0),
                    ),
                ),
                Rule(
                    indicator="ema",
                    params={"len": Param(value=200)},
                    operator="price_above",
                    timeframe="1h",
                ),
            ],
            filters=[
                Rule(
                    indicator="relative_volume",
                    params={"window": Param(value=20)},
                    operator="lt",
                    target=Param(value=0.8),
                ),
            ],
        ),
        exit=RuleGroup(
            match="any",
            rules=[
                Rule(
                    indicator="wavetrend",
                    params={},
                    operator="crosses_below",
                    target=Param(value=53),
                ),
                Rule(
                    indicator="time_stop",
                    params={
                        "bars": Param(
                            value=36, optimize=OptimizeRange(min=24, step=12, max=72)
                        )
                    },
                    operator="true",
                ),
            ],
        ),
        risk=RiskBlock(
            stop_loss_atr_mult=Param(
                value=2.0, optimize=OptimizeRange(min=1.5, step=0.25, max=3.0)
            ),
            stop_loss_atr_len=Param(value=14),
            take_profit_r=Param(
                value=1.8, optimize=OptimizeRange(min=1.2, step=0.2, max=2.6)
            ),
            risk_per_trade_pct=Param(value=0.75),
            max_concurrent=Param(value=2),
            time_stop_bars=Param(value=36),
        ),
        instruments=[
            InstrumentConfig(symbol="XAUUSD", timeframe="15m", active=True),
            InstrumentConfig(symbol="EURUSD", timeframe="15m"),
            InstrumentConfig(symbol="NAS100", timeframe="5m"),
        ],
        walkforward=WalkForwardConfig(),
    )


def build_engine_fixture() -> StrategyDefinition:
    """A strategy `to_nautilus` accepts — the shortest path to real metrics.

    Every rule maps to a composer block (`rsi_threshold`, `adx_threshold`,
    `atr_stop`), there is no regime or allocation block, and the single
    instrument is a Bybit symbol on an interval the loader understands. Keep
    it that way: `tests/studio/test_seed_fixtures.py` asserts it stays
    engine-runnable.
    """
    return StrategyDefinition(
        id="rsi-adx-btc",
        name="RSI Dip + ADX Trend (BTC)",
        entry=RuleGroup(
            match="all",
            rules=[
                # Thresholds picked so the strategy actually trades: at
                # rsi<30 & adx>25 it fired once in 180 days of BTCUSDT 1h,
                # which tells you nothing about the engine path.
                Rule(
                    indicator="rsi",
                    params={
                        "len": Param(
                            value=14, optimize=OptimizeRange(min=10, step=2, max=18)
                        )
                    },
                    operator="crosses_below",
                    target=Param(
                        value=40, optimize=OptimizeRange(min=30, step=5, max=45)
                    ),
                ),
                Rule(
                    indicator="adx",
                    params={"len": Param(value=14)},
                    operator="gt",
                    target=Param(value=20),
                ),
            ],
        ),
        exit=RuleGroup(
            match="any",
            rules=[
                Rule(
                    indicator="atr",
                    params={"len": Param(value=14)},
                    operator="gt",
                    target=Param(
                        value=3.0, optimize=OptimizeRange(min=2.0, step=0.5, max=4.0)
                    ),
                ),
            ],
        ),
        risk=RiskBlock(
            stop_loss_atr_mult=Param(value=2.0),
            stop_loss_atr_len=Param(value=14),
            take_profit_r=Param(value=1.5),
            risk_per_trade_pct=Param(value=0.5),
            max_concurrent=Param(value=1),
        ),
        instruments=[
            InstrumentConfig(symbol="BTCUSDT", timeframe="1h", active=True),
        ],
        walkforward=WalkForwardConfig(folds=3),
    )


if __name__ == "__main__":
    store = StrategyStore()
    for build in (build_fixture, build_engine_fixture):
        defn = build()
        v = store.save(defn)
        print(f"seeded {defn.id} as version {v} -> {store.db_path}")
