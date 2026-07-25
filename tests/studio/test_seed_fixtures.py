"""The seeded demo strategies, and which engine each one can run on.

`rsi-adx-btc` exists to be engine-runnable — that is its whole job, so a rule
added to it later must not quietly cost it that property.
"""

from __future__ import annotations

import pytest

from scripts.seed_studio import build_engine_fixture, build_fixture
from strategy_studio.backtest import (
    StubBacktestAdapter,
    UnsupportedStrategy,
    to_nautilus,
)
from strategy_studio.compiler import compile_strategy


def test_engine_fixture_is_accepted_by_to_nautilus():
    spec = to_nautilus(compile_strategy(build_engine_fixture()))

    assert spec.validate() is None, spec.validate()
    assert [(b.type, b.role) for b in spec.blocks] == [
        ("rsi_threshold", "entry"),
        ("adx_threshold", "entry"),
        ("atr_stop", "exit"),
    ]


def test_engine_fixture_instrument_is_loadable():
    """Symbol/timeframe the default bars loader can actually resolve."""
    from data import BYBIT_ALL_INTERVALS

    inst = build_engine_fixture().instruments
    assert len(inst) == 1 and inst[0].active
    assert inst[0].timeframe in {label for _, label in BYBIT_ALL_INTERVALS}


def test_engine_fixture_stays_free_of_untranslatable_blocks():
    defn = build_engine_fixture()

    assert defn.regime is None
    assert defn.allocation is None


def test_engine_fixture_also_runs_on_the_stub():
    """Default install (no env flag) must still produce metrics for it."""
    metrics = StubBacktestAdapter().run(compile_strategy(build_engine_fixture()))

    assert metrics.trades > 0
    assert metrics.equity_curve


def test_design_fixture_is_refused_and_says_why():
    """The mockup fixture is stub-only by design — the reasons are the docs."""
    with pytest.raises(UnsupportedStrategy) as e:
        to_nautilus(compile_strategy(build_fixture()))

    blob = " ".join(e.value.reasons)
    for expected in (
        "regime",  # conditional branch
        "funding_z",  # no composer block
        "timeframe",  # the 1h EMA rule on a 15m/5m strategy
        "time_stop",  # no time-based exit
        "max_concurrent",  # engine holds one position
    ):
        assert expected in blob, f"{expected!r} missing from: {blob}"


def test_the_two_fixtures_are_distinct_strategies():
    assert build_fixture().id != build_engine_fixture().id
