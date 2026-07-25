"""Seed the WT-Funding Confluence v3 fixture (matches the design mockup)."""
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
            conditions=RuleGroup(match="all", rules=[
                Rule(indicator="ema", params={"len": Param(value=200)},
                     operator="price_above", timeframe="1h"),
                Rule(indicator="adx", params={"len": Param(value=14)},
                     operator="gt",
                     target=Param(value=20,
                                  optimize=OptimizeRange(min=15, step=5, max=30))),
            ]),
            evaluate="every_bar",
            **{"else": "flat"},
        ),
        entry=RuleGroup(
            match="all",
            rules=[
                Rule(indicator="wavetrend",
                     params={
                         "n1": Param(value=10, optimize=OptimizeRange(min=6, step=2, max=14)),
                         "n2": Param(value=21, optimize=OptimizeRange(min=15, step=3, max=27)),
                     },
                     operator="crosses_above", target=Param(value=-53)),
                Rule(indicator="funding_z",
                     params={"lookback": Param(value=96)},
                     operator="lt",
                     target=Param(value=-1.5,
                                  optimize=OptimizeRange(min=-2.5, step=0.25, max=-1.0))),
                Rule(indicator="ema", params={"len": Param(value=200)},
                     operator="price_above", timeframe="1h"),
            ],
            filters=[
                Rule(indicator="relative_volume",
                     params={"window": Param(value=20)},
                     operator="lt", target=Param(value=0.8)),
            ],
        ),
        exit=RuleGroup(
            match="any",
            rules=[
                Rule(indicator="wavetrend", params={}, operator="crosses_below",
                     target=Param(value=53)),
                Rule(indicator="time_stop",
                     params={"bars": Param(
                         value=36, optimize=OptimizeRange(min=24, step=12, max=72))},
                     operator="true"),
            ],
        ),
        risk=RiskBlock(
            stop_loss_atr_mult=Param(value=2.0,
                                     optimize=OptimizeRange(min=1.5, step=0.25, max=3.0)),
            stop_loss_atr_len=Param(value=14),
            take_profit_r=Param(value=1.8,
                                optimize=OptimizeRange(min=1.2, step=0.2, max=2.6)),
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


if __name__ == "__main__":
    store = StrategyStore()
    v = store.save(build_fixture())
    print(f"seeded wt-funding-v3 as version {v} -> {store.db_path}")
