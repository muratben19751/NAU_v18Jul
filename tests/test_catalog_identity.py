"""Faz 1b — spot/linear/inverse have distinct catalog identities.

Before the fix every category collapsed to one InstrumentId/BarType, so the
per-bar_type delete-then-write in ``_auto_write_bybit_catalog`` wiped one
category's bars when another was written. These tests pin the per-category
venue identity and the no-clobber guarantee.
"""

import pandas as pd
import pytest

from backtest import _make_bybit_bar_type, _make_bybit_instrument


def _ohlcv(n=6, start="2024-01-01"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "volume": [1.0] * n,
        },
        index=idx,
    )
    return df


class TestPerCategoryIdentity:
    def test_venues_are_distinct(self):
        venues = {
            str(_make_bybit_instrument(symbol="BTCUSDT", category=c).id.venue)
            for c in ("spot", "linear", "inverse")
        }
        assert venues == {"BYBIT_SPOT", "BYBIT_LINEAR", "BYBIT_INVERSE"}

    def test_bar_type_dsls_differ_by_category(self):
        dsls = {
            str(
                _make_bybit_bar_type(
                    _make_bybit_instrument(symbol="BTCUSDT", category=c).id, "1"
                )
            )
            for c in ("spot", "linear", "inverse")
        }
        assert len(dsls) == 3

    @pytest.mark.parametrize(
        "category,quote,symbol",
        [
            ("spot", "USDT", "BTCUSDT"),
            ("linear", "USDT", "BTCUSDT"),
            ("inverse", "USD", "BTCUSD"),
        ],
    )
    def test_inverse_quote_and_symbol(self, category, quote, symbol):
        inst = _make_bybit_instrument(symbol="BTCUSDT", category=category)
        assert inst.quote_currency.code == quote
        assert str(inst.raw_symbol) == symbol


class TestNoClobber:
    """Writing linear after spot (same symbol+interval) must NOT delete spot's
    bars — the regression that motivated per-category venues."""

    def test_spot_and_linear_coexist(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "catalog")

        df = _ohlcv(6)
        data._auto_write_bybit_catalog("BTCUSDT", "1", df, "spot")
        data._auto_write_bybit_catalog("BTCUSDT", "1", df, "linear")

        for category in ("spot", "linear"):
            inst = _make_bybit_instrument(symbol="BTCUSDT", category=category)
            bt = _make_bybit_bar_type(inst.id, "1")
            state = data.nautilus_catalog_bar_state(str(bt))
            assert state is not None, f"{category} bars missing (clobbered)"
            assert state["rows"] == 6

    def test_rebuild_is_idempotent(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "catalog")
        df = _ohlcv(6)
        # Writing the same (category, symbol, interval) twice must not stack rows.
        data._auto_write_bybit_catalog("BTCUSDT", "1", df, "linear")
        data._auto_write_bybit_catalog("BTCUSDT", "1", df, "linear")
        inst = _make_bybit_instrument(symbol="BTCUSDT", category="linear")
        bt = _make_bybit_bar_type(inst.id, "1")
        state = data.nautilus_catalog_bar_state(str(bt))
        assert state is not None and state["rows"] == 6
