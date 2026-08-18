"""Cent altı semboller artık 0.00'a yuvarlanmıyor — sessiz yuvarlama imkânsız.

DeepR 2026-08-11 [YÜKSEK]: `_BYBIT_SPECS` yalnız BTC/ETH/SOL/XRP içeriyordu;
listede olmayan HER sembol `_BYBIT_DEFAULT_SPEC = (2, 3, "0.01")` alıyordu.
`Price(0.000009, 2)` → `0.00`. Yani kullanıcı Data/Lab ekranından PEPEUSDT,
SHIBUSDT, FLOKIUSDT gibi cent altı bir sembol seçtiğinde tüm OHLC serisi
sıfırlanıyor, `_compute_qty` `price <= 0` dalına düşüp sabit lota geri dönüyor
ve koşum HATA VERMEDEN "geçerli" bir sonuç raporluyordu.

Bunun teorik olmadığının kanıtı kod tabanının kendisindeydi:
`chart_indicators.py`'nin yorumu "düşük fiyatlı enstrümanlar (SHIB/PEPE
~1e-5..1e-8) için round(v,4) çizgiyi 0.0'a düzlüyordu" diyor — grafik tarafı
düzeltilmiş, motor tarafı düzeltilmemişti.

Düzeltme, wiki'deki Equity `size_precision=0` tuzağıyla TUTARLI iki katman:
  (a) hassasiyet sembolün gerçek fiyat ölçeğinden türetilir,
  (b) buna rağmen pozitif bir fiyat sıfıra düşerse dönüşüm REDDEDİLİR
      (`PricePrecisionError`) — sessiz yuvarlama artık mümkün değil.

Wiki References: [[index_backtest_via_equity_proxy]], [[nau_deepr_dorduncu_tur_2026_08_11]]
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from nautilus_trader.model.objects import Price

from backtest import (
    PricePrecisionError,
    _bars_from_df,
    _make_bybit_bar_type,
    _make_bybit_instrument,
    price_precision_for,
    price_scale_hint,
)


def _frame(price: float, n: int = 30) -> pd.DataFrame:
    """Tek fiyat seviyesinde küçük bir OHLCV çerçevesi (open-time index)."""
    idx = pd.date_range(
        end=datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        periods=n,
        freq="60min",
        tz="UTC",
    )
    close = np.full(n, price)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1_000.0),
        },
        index=idx,
    )


class TestPricePrecisionDerivation:
    @pytest.mark.parametrize(
        "price,expected",
        [
            (60_000.0, 2),  # BTC — varsayılanla aynı, gereksiz hane yok
            (150.0, 3),  # SOL ölçeği
            (0.5, 6),
            (9e-6, 9),  # PEPE
            (1e-8, 9),  # SHIB'in en dibi — Nautilus tavanı 9
        ],
    )
    def test_precision_follows_the_order_of_magnitude(self, price, expected):
        assert price_precision_for(price) == expected

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_degenerate_input_falls_back_to_the_default(self, bad):
        """Fiyat olmayan bir değer için tahminde bulunmuyoruz — varsayılan."""
        assert price_precision_for(bad) == 2

    def test_derived_precision_actually_survives_nautilus(self):
        """Türetilen hane sayısı iddiayı taşımalı: Price(...) sıfır OLMAMALI."""
        for p in (9e-6, 1e-7, 3.4e-5):
            assert float(Price(p, price_precision_for(p))) > 0

    def test_hint_is_the_smallest_positive_price_in_the_frame(self):
        """Ortalama değil MİNİMUM: sıfıra düşecek olan barlar tam onlar."""
        df = _frame(9e-6)
        hint = price_scale_hint(df)
        assert hint == pytest.approx(9e-6 * 0.99)

    def test_hint_is_none_for_a_frame_without_prices(self):
        assert price_scale_hint(None) is None
        assert price_scale_hint(pd.DataFrame({"volume": [1.0]})) is None


class TestInstrumentUsesTheRealScale:
    def test_subcent_symbol_no_longer_gets_two_decimals(self):
        inst = _make_bybit_instrument(
            symbol="PEPEUSDT", base="PEPE", category="linear", price_hint=9e-6
        )
        assert inst.price_precision >= 6
        assert float(inst.price_increment) > 0
        # Asıl iddia: bu enstrümanla fiyat artık kaybolmuyor.
        assert float(Price(9e-6, inst.price_precision)) > 0

    def test_pinned_symbol_ignores_the_hint(self):
        """BTCUSDT NAU universe.yaml'a sabitli — veri ne derse desin sp/pp sabit.

        Aksi hâlde katalog kimliği (bar_type + instrument spec) veri
        penceresine göre kayardı.
        """
        inst = _make_bybit_instrument(
            symbol="BTCUSDT", base="BTC", category="linear", price_hint=9e-6
        )
        assert inst.price_precision == 2

    def test_without_a_hint_behaviour_is_unchanged(self):
        """Geriye dönük uyumluluk: ipucu yoksa eski varsayılan aynen geçerli."""
        inst = _make_bybit_instrument(symbol="FLOKIUSDT", base="FLOKI")
        assert inst.price_precision == 2

    def test_sandbox_recipe_path_derives_from_the_bars(self):
        """Gerçek çağrı yolu: `_build_instrument_bar_type(recipe, bars_df)`.

        Backtest zincirindeki her enstrüman buradan geçiyor; ipucunun
        gerçekten bağlandığını route seviyesinin bir altında sabitliyoruz.
        """
        from sandbox import _build_instrument_bar_type

        recipe = {"symbol": "PEPEUSDT", "category": "linear", "interval": "60"}
        inst, _bt = _build_instrument_bar_type(recipe, _frame(9e-6))
        assert inst.price_precision >= 6


class TestSilentRoundingIsImpossible:
    def test_bars_from_df_refuses_to_zero_out_prices(self):
        """(b) katmanı: ipucu hiç gelmese bile dönüşüm SESSİZCE geçmemeli."""
        inst = _make_bybit_instrument(symbol="PEPEUSDT", base="PEPE")  # pp=2
        bt = _make_bybit_bar_type(inst.id, "60")
        with pytest.raises(PricePrecisionError) as exc:
            _bars_from_df(bt, inst, _frame(9e-6))
        msg = str(exc.value)
        assert "rounds to 0.00" in msg
        assert "price_precision=2" in msg
        # Mesaj eyleme dönüşebilir olmalı: kaç hane gerektiğini söylesin.
        assert "9 decimals" in msg

    def test_derived_instrument_passes_the_guard_and_keeps_the_prices(self):
        """Uçtan uca: türetilmiş hassasiyetle aynı veri sorunsuz Bar'a döner."""
        inst = _make_bybit_instrument(
            symbol="PEPEUSDT", base="PEPE", price_hint=price_scale_hint(_frame(9e-6))
        )
        bt = _make_bybit_bar_type(inst.id, "60")
        bars = _bars_from_df(bt, inst, _frame(9e-6))
        assert len(bars) == 30
        assert all(float(b.close) > 0 for b in bars)
        assert float(bars[0].close) == pytest.approx(9e-6, rel=1e-3)

    def test_normal_btc_data_is_untouched(self):
        """Regresyon: guard normal veride ASLA tetiklenmemeli."""
        inst = _make_bybit_instrument(symbol="BTCUSDT", base="BTC")
        bt = _make_bybit_bar_type(inst.id, "60")
        bars = _bars_from_df(bt, inst, _frame(30_000.0))
        assert len(bars) == 30
        assert float(bars[0].close) == pytest.approx(30_000.0)

    def test_all_zero_frame_is_not_treated_as_a_precision_error(self):
        """Hiç pozitif fiyat yoksa kaybedilecek bir şey de yok — guard susar.

        (Bu veri başka bir sebeple bozuk; onu burada hata olarak raporlamak
        yanlış yere işaret etmek olurdu.)
        """
        inst = _make_bybit_instrument(symbol="BTCUSDT", base="BTC")
        bt = _make_bybit_bar_type(inst.id, "60")
        df = _frame(30_000.0)
        for col in ("open", "high", "low", "close"):
            df[col] = 0.0
        assert len(_bars_from_df(bt, inst, df)) == 30
