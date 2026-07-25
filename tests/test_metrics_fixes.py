"""B set regressions: H5 frequency-aware annualization, M5 commission,
H11 max_dd_pct bridge, L5 _to_unix bands, L30 NAU spec pins, L33 PF cap.
"""

from __future__ import annotations

import math

import pytest


class TestPeriodsPerYear:
    """H5: annualization base should be derived from source + bar interval."""

    def _bt(self, interval: str):
        from backtest import _make_bybit_bar_type, _make_bybit_instrument

        inst = _make_bybit_instrument()
        return _make_bybit_bar_type(inst.id, interval), inst

    def test_crypto_1m(self):
        from backtest import _periods_per_year

        bt, inst = self._bt("1")
        assert _periods_per_year(bt, inst) == 365 * 24 * 60  # 525_600

    def test_crypto_1h_and_daily(self):
        from backtest import _periods_per_year

        bt, inst = self._bt("60")
        assert _periods_per_year(bt, inst) == 365 * 24
        bt, inst = self._bt("D")
        assert _periods_per_year(bt, inst) == 365

    def test_unknown_falls_back_to_365(self):
        from backtest import _periods_per_year

        assert _periods_per_year(None, None) == 365

    def test_equity_week_month(self):
        # M387: equity/index WEEK→52, MONTH→12 (not 252; weekly Sharpe was ~2.2×,
        # monthly ~4.6× inflated). instrument non-CurrencyPair → equity branch; spec
        # str is derived from the '<step>-<UNIT>-...' surface (_periods_per_year doc).
        from backtest import _periods_per_year

        class _Spec:
            def __init__(self, unit):
                self._unit = unit

            def __str__(self):
                return f"1-{self._unit}-LAST"

        class _BarType:
            def __init__(self, unit):
                self.spec = _Spec(unit)

        equity_inst = object()  # not CurrencyPair → is_crypto False
        assert _periods_per_year(_BarType("WEEK"), equity_inst) == 52
        assert _periods_per_year(_BarType("MONTH"), equity_inst) == 12
        assert _periods_per_year(_BarType("DAY"), equity_inst) == 252


class TestToUnixBands:
    """L5: all four bands sec/ms/µs/ns should be classified correctly."""

    def test_all_resolutions(self):
        import pandas as pd

        from backtest import _extract_trades  # no access to closure — indirect test

        ts_s = 1_752_000_000
        rows = []
        for v in (
            ts_s,  # seconds
            ts_s * 1_000,  # ms
            ts_s * 1_000_000,  # µs (old code would jump ~55,000 years forward)
            ts_s * 1_000_000_000,  # ns
        ):
            rows.append(
                {
                    "ts_opened": v,
                    "ts_closed": v,
                    "avg_px_open": 100.0,
                    "avg_px_close": 101.0,
                    "realized_pnl": "1.0 USDT",
                    "entry": "BUY",
                }
            )
        df = pd.DataFrame(rows)
        trades = _extract_trades(df, 0)
        assert len(trades) == 4
        for t in trades:
            assert t["entry_time"] == ts_s, t


class TestNauDrawdownBridge:
    """H11: internal negative-fraction convention ↔ NAU positive-percent bridge."""

    def test_bridge(self):
        from backtest import nau_max_drawdown

        assert nau_max_drawdown(-0.125) == 12.5
        assert nau_max_drawdown(0.0) == 0.0
        assert nau_max_drawdown(None) is None
        assert nau_max_drawdown(float("nan")) is None


class TestNauSpecPins:
    """L30: instrument specs pinned to NAU universe.yaml values."""

    def test_bybit_specs_match_universe_yaml(self):
        from backtest import _BYBIT_SPECS

        # pinned by hand from universe.yaml (no runtime yaml reading).
        assert _BYBIT_SPECS["BTCUSDT"] == (2, 3, "0.01")
        assert _BYBIT_SPECS["ETHUSDT"] == (2, 3, "0.01")
        assert _BYBIT_SPECS["SOLUSDT"] == (3, 2, "0.001")
        assert _BYBIT_SPECS["XRPUSDT"] == (4, 1, "0.0001")

    def test_index_equity_spec_matches_qlab(self):
        from backtest import _make_index_instrument

        eq = _make_index_instrument("I:SPX")
        assert str(eq.quote_currency) == "USD"
        assert eq.price_precision == 2
        assert str(eq.price_increment) == "0.01"
        assert int(eq.lot_size) == 1

    def test_bybit_fees_pinned(self):
        """M5: instrument commission rates consistent with bps constants."""
        from backtest import (
            BYBIT_MAKER_FEE_BPS,
            BYBIT_TAKER_FEE_BPS,
            _make_bybit_instrument,
        )

        inst = _make_bybit_instrument()
        assert float(inst.maker_fee) == pytest.approx(BYBIT_MAKER_FEE_BPS / 10_000)
        assert float(inst.taker_fee) == pytest.approx(BYBIT_TAKER_FEE_BPS / 10_000)


class TestIBEquityCommission:
    """US stock/ETF (QQQ) Interactive Brokers Pro Fixed commission."""

    def test_fee_model_selection_by_instrument_type(self):
        """Crypto→MakerTaker (bps), equity→IB Fixed (per-share)."""
        from nautilus_trader.backtest.models import MakerTakerFeeModel

        from backtest import (
            IBFixedFeeModel,
            _fee_model_for,
            _make_bybit_instrument,
            _make_index_instrument,
        )

        assert isinstance(_fee_model_for(_make_bybit_instrument()), MakerTakerFeeModel)
        assert isinstance(_fee_model_for(_make_index_instrument("QQQ")), IBFixedFeeModel)

    def test_ib_fixed_commission_math(self):
        """IB Fixed: max(per-share, min) then clamp with 1% cap."""
        from nautilus_trader.model.objects import Price, Quantity

        from backtest import IBFixedFeeModel, _make_index_instrument

        eq = _make_index_instrument("QQQ")
        m = IBFixedFeeModel()

        def comm(qty, px):
            return float(
                m.get_commission(
                    None, Quantity.from_int(qty), Price.from_str(str(px)), eq
                )
            )

        # 100 shares @ $500: per-share $0.50 < min → $1.00 floor
        assert comm(100, 500) == pytest.approx(1.0)
        # 1000 shares @ $500: per-share $5.00 (above min, below 1% cap=$5000)
        assert comm(1000, 500) == pytest.approx(5.0)
        # 1000 shares @ $0.10: per-share $5.00 but 1% cap=$1.00 wins
        assert comm(1000, 0.10) == pytest.approx(1.0)

    def test_ib_fixed_constants(self):
        """IBKR Pro Fixed tariff: $0.005/share, min $1, 1% cap."""
        from backtest import (
            IB_FIXED_MAX_PCT_OF_TRADE,
            IB_FIXED_MIN_PER_ORDER_USD,
            IB_FIXED_PER_SHARE_USD,
        )

        assert IB_FIXED_PER_SHARE_USD == 0.005
        assert IB_FIXED_MIN_PER_ORDER_USD == 1.0
        assert IB_FIXED_MAX_PCT_OF_TRADE == 0.01


class TestProfitFactorCap:
    """L33: NAU cap (99.0) instead of inf on a lossless run."""

    def test_metrics_empty_pf_zero(self):
        from backtest import _metrics

        m = _metrics(None, None)
        assert m["profit_factor"] == 0.0  # not NaN (JSON safe)
        # M620: degenerate/empty → sharpe_per_trade NaN (field present, value NaN).
        # (old `!= self or True` tautology always passed — dead check.)
        assert math.isnan(m["sharpe_per_trade"])
        assert "max_dd_pct" in m
