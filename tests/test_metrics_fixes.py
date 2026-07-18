"""B kümesi regresyonları: H5 frekans-duyarlı yıllıklandırma, M5 komisyon,
H11 max_dd_pct köprüsü, L5 _to_unix bantları, L30 NAU spec pinleri, L33 PF cap.
"""

from __future__ import annotations

import math

import pytest


class TestPeriodsPerYear:
    """H5: yıllıklandırma tabanı kaynak + bar aralığından türetilmeli."""

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
        # M387: equity/index WEEK→52, MONTH→12 (252 değil; haftalık Sharpe ~2.2×,
        # aylık ~4.6× şişiktı). instrument non-CurrencyPair → equity dalı; spec
        # str'i '<step>-<UNIT>-...' yüzeyinden türetilir (_periods_per_year doc).
        from backtest import _periods_per_year

        class _Spec:
            def __init__(self, unit):
                self._unit = unit

            def __str__(self):
                return f"1-{self._unit}-LAST"

        class _BarType:
            def __init__(self, unit):
                self.spec = _Spec(unit)

        equity_inst = object()  # CurrencyPair değil → is_crypto False
        assert _periods_per_year(_BarType("WEEK"), equity_inst) == 52
        assert _periods_per_year(_BarType("MONTH"), equity_inst) == 12
        assert _periods_per_year(_BarType("DAY"), equity_inst) == 252


class TestToUnixBands:
    """L5: sn/ms/µs/ns dört bandı da doğru sınıflanmalı."""

    def test_all_resolutions(self):
        import pandas as pd

        from backtest import _extract_trades  # closure'a erişim yok — dolaylı test

        ts_s = 1_752_000_000
        rows = []
        for v in (
            ts_s,  # saniye
            ts_s * 1_000,  # ms
            ts_s * 1_000_000,  # µs (eski kod ~55.000 yıl ileri atardı)
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
    """H11: negatif-kesir iç konvansiyonu ↔ NAU pozitif-yüzde köprüsü."""

    def test_bridge(self):
        from backtest import nau_max_drawdown

        assert nau_max_drawdown(-0.125) == 12.5
        assert nau_max_drawdown(0.0) == 0.0
        assert nau_max_drawdown(None) is None
        assert nau_max_drawdown(float("nan")) is None


class TestNauSpecPins:
    """L30: enstrüman spec'leri NAU universe.yaml değerlerine pinli."""

    def test_bybit_specs_match_universe_yaml(self):
        from backtest import _BYBIT_SPECS

        # universe.yaml'dan elle pinlendi (runtime yaml okuma yok).
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
        """M5: instrument komisyon oranları bps sabitleriyle tutarlı."""
        from backtest import (
            BYBIT_MAKER_FEE_BPS,
            BYBIT_TAKER_FEE_BPS,
            _make_bybit_instrument,
        )

        inst = _make_bybit_instrument()
        assert float(inst.maker_fee) == pytest.approx(BYBIT_MAKER_FEE_BPS / 10_000)
        assert float(inst.taker_fee) == pytest.approx(BYBIT_TAKER_FEE_BPS / 10_000)


class TestProfitFactorCap:
    """L33: kayıpsız koşuda inf yerine NAU cap'i (99.0)."""

    def test_metrics_empty_pf_zero(self):
        from backtest import _metrics

        m = _metrics(None, None)
        assert m["profit_factor"] == 0.0  # NaN değil (JSON güvenli)
        # M620: dejenere/boş → sharpe_per_trade NaN (alan var, değer NaN).
        # (eski `!= self or True` totolojisi her zaman geçiyordu — ölü kontrol.)
        assert math.isnan(m["sharpe_per_trade"])
        assert "max_dd_pct" in m
