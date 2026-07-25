"""Registry INTEGRATION POINT: indicator `impl` wiring to indicators.py.

Guards the calling convention documented in strategy_studio/registry.py —
`impl(bars, **schema_params)` — so a rename in indicators.py (or a schema
param rename) fails here instead of silently at backtest time.
"""

from __future__ import annotations

import math

import pytest

from strategy_studio.registry import INDICATOR_REGISTRY

# Indicators the host app has no feature function for yet.
UNWIRED = {
    "macd",
    "funding_z",
    "oi_z",
    "cvd_divergence",
    "volume_profile",
    "time_stop",
    "session_filter",
}
WIRED = sorted(set(INDICATOR_REGISTRY) - UNWIRED)


@pytest.fixture(scope="module")
def bars():
    n = 300
    close = [100 + 10 * math.sin(i / 12) + i * 0.03 for i in range(n)]
    return {
        "high": [c * 1.004 for c in close],
        "low": [c * 0.996 for c in close],
        "close": close,
        "volume": [1000 + (i % 37) * 25 for i in range(n)],
    }


def test_wired_and_unwired_split_is_exhaustive():
    """Every registry entry is deliberately either wired or listed as unwired."""
    assert set(WIRED) | UNWIRED == set(INDICATOR_REGISTRY)
    assert not (set(WIRED) & UNWIRED)


@pytest.mark.parametrize("key", WIRED)
def test_wired_impl_runs_with_schema_param_names(key, bars):
    """impl accepts the registry's own param names and returns a value."""
    spec = INDICATOR_REGISTRY[key]
    assert spec.impl is not None, f"{key} lost its impl"

    params = {p.name: p.default for p in spec.params if p.default is not None}
    value = spec.impl(bars, **params)

    assert value is not None, f"{key} returned None on a 300-bar window"
    if isinstance(value, dict):
        # Multi-output indicators mix numeric readings with label fields
        # ('signal': 'neutral', 'trend': 'up') — at least one number must be there.
        numeric = [
            v
            for v in value.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        assert numeric, f"{key} returned no numeric reading: {value}"
        assert not any(math.isnan(float(v)) for v in numeric)
    else:
        assert isinstance(value, (int, float))
        assert not math.isnan(float(value))


@pytest.mark.parametrize("key", sorted(UNWIRED))
def test_unwired_entries_stay_declarative(key):
    """No implementation claimed where none exists — compiler still accepts them."""
    assert INDICATOR_REGISTRY[key].impl is None


def test_impl_params_are_a_subset_of_the_declared_schema_params():
    """impl must not need arguments the UI cannot supply."""
    import inspect

    for key in WIRED:
        spec = INDICATOR_REGISTRY[key]
        sig = inspect.signature(spec.impl)
        accepted = set(sig.parameters) - {"bars"}
        declared = {p.name for p in spec.params}
        assert declared <= accepted, (
            f"{key}: schema params {declared - accepted} not accepted by impl"
        )
