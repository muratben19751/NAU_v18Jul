"""Yıllıklaştırma tabanı, enstrüman yoksa VERİDEN okunur.

`run_composed_backtest`/`run_backtest` çağıranı bir instrument vermediğinde
SENTETİK bir Bybit 1-DAKİKA enstrümanı kuruyor. Bu sentetik nesne bir ara
yıllıklaştırmaya da giriyordu: günlük hisse barları 1-dakikalık kripto gibi
yıllıklaştırılıyor ve Sharpe sqrt(525.600/252) = 45,7 kat şişiyordu.

Ölçüldü 2026-08-21 (QQQC.NASDAQ 1-DAY, mühürlü eğitim çerçevesi, aynı spec):

    yol            önce      sonra
    doğrudan      25,4209    0,5566
    havuz          0,5622    0,5622      (değişmedi — zaten instrument veriyordu)

Öngörülen oran sqrt(525.600/252) = 45,67; gözlenen 45,22. Kalan ~%1 fark
pnl/komisyon/precision farkından geliyor (ayrı ve önceden var olan mesele) —
bu yüzden aşağıdaki testler METRİK EŞİTLİĞİ değil, YILLIKLAŞTIRMA TABANINI
çiviliyor. İkisini karıştırmak, düzeltilmemiş başka bir farkı bu testin
sırtına yıkardı.

Şiddet notu: `_score` işlem başına Sharpe'ı (`sharpe_per_trade`, sqrt(n)
ölçekli) okuyor ve onu `_periods_per_year` hiç etkilemiyor; üretimdeki her
sıralama/robustluk çağıranı da instrument geçiriyor. Yani bu aktif bir kapı
bozucu değildi, instrument geçirmeyen çağıranlar (capture_baseline, legacy,
testler) için GİZLİ bir tuzaktı.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import backtest as B


def _frame(periods: int, freq: str, *, weekdays_only: bool) -> pd.DataFrame:
    idx = pd.date_range("2015-01-05", periods=periods * 3, freq=freq, tz="UTC")
    if weekdays_only:
        idx = idx[idx.dayofweek < 5]
    idx = idx[:periods]
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(idx)))
    return pd.DataFrame({"close": close}, index=idx)


# ---------------------------------------------------------------------------
# Taban veriden okunuyor
# ---------------------------------------------------------------------------


def test_weekday_only_daily_bars_are_annualized_as_equity():
    """Hafta sonu barı yoksa seri 24/7 değildir → 252 işlem günü.

    Eski davranış 525.600 idi (1-dakikalık kripto varsayımı).
    """
    assert B._periods_per_year(None, None, _frame(400, "1D", weekdays_only=True)) == 252


def test_calendar_daily_bars_are_annualized_as_24_7():
    """Hafta sonunda da bar varsa seri 24/7'dir → 365.

    Bu bir kusur değil, doğru davranış: takvim-günlük veri 365 gün işlem görür.
    Sentetik test çerçeveleri (`pd.date_range(freq="1D")`) bu daldan geçer.
    """
    assert (
        B._periods_per_year(None, None, _frame(400, "1D", weekdays_only=False)) == 365
    )


def test_minute_bars_with_weekends_stay_crypto():
    """Kripto yolu DEĞİŞMEMELİ — bu düzeltme yalnız yanlış varsayımı kaldırır."""
    got = B._periods_per_year(None, None, _frame(3000, "1min", weekdays_only=False))
    assert got == 365 * 24 * 60 == 525_600


def test_weekday_only_hourly_bars_use_the_observed_session_shape():
    """Seans şeklinin otoritesi seridir; sabit 6,5 saat varsayımı değil."""
    got = B._periods_per_year(None, None, _frame(2000, "1h", weekdays_only=True))
    # Saatlik hisse: gözlenen bar/gün × 252. Kripto-saatlik (8.760) OLMAMALI.
    assert 252 <= got <= 252 * 24
    assert got != 8_760


# ---------------------------------------------------------------------------
# Gerçek enstrüman verildiğinde hiçbir şey değişmez
# ---------------------------------------------------------------------------


def test_a_real_instrument_still_wins():
    """Enstrüman biliniyorsa veri tahmini devreye GİRMEZ.

    Aksi hâlde düzeltme, doğru çalışan üretim yolunu da değiştirirdi.
    """
    pytest.importorskip("nautilus_trader")
    from sandbox import _build_instrument_bar_type

    df = _frame(400, "1D", weekdays_only=True)
    inst, bar_type = _build_instrument_bar_type(
        {"symbol": "BTCUSDT", "interval": "1", "category": "linear"}, df
    )
    # Kripto enstrümanı + 1-DAKİKA bar tipi → veri günlük olsa bile kripto-dakika.
    assert B._periods_per_year(bar_type, inst, df) == 525_600


# ---------------------------------------------------------------------------
# Çıkarım güvenilmezse eski varsayım korunur
# ---------------------------------------------------------------------------


def test_a_short_series_does_not_get_guessed():
    """İki haftadan kısa seride hafta sonu çıkarımı yapılamaz.

    Yanlış bir "hisse" tahmini tabanı sessizce 252'ye düşürürdü — ve sessiz
    yanlış, gürültülü yanlıştan beterdir.
    """
    short = _frame(30, "1h", weekdays_only=True)  # ~4 gün kapsam
    assert B._periods_per_year(None, None, short) == 365 * 24


@pytest.mark.parametrize("bad", [None, pd.DataFrame()])
def test_no_data_falls_back_to_the_old_default(bad):
    assert B._periods_per_year(None, None, bad) == 365


def test_helpers_refuse_to_guess_from_too_few_bars():
    tiny = _frame(5, "1D", weekdays_only=True)
    assert B._observed_bar_seconds(tiny) == 0.0
    assert B._looks_like_24_7(tiny) is True


# ---------------------------------------------------------------------------
# Sentetik enstrüman yıllıklaştırmaya bir daha SOKULMAMALI
# ---------------------------------------------------------------------------


def test_the_synthetic_instrument_never_drives_annualization():
    """Kusurun kendisi buradaydı: çağrı yeri `active_*`'ı geçiriyordu.

    `active_instrument`/`active_bar_type`, çağıran bir şey vermediğinde
    sentetik Bybit 1-DAKİKA olur. Onları `_periods_per_year`'a vermek, veriye
    bakılmaksızın kripto-dakika varsaymak demektir.
    """
    import inspect
    import re

    for fn in (B.run_backtest, B.run_composed_backtest):
        src = inspect.getsource(fn)
        for call in re.findall(r"_periods_per_year\(([^)]*)\)", src):
            args = [a.strip() for a in call.split(",")]
            assert not any(a.startswith("active_") for a in args), (
                f"{fn.__name__}: sentetik `active_*` yıllıklaştırmaya sokulmuş: {call}"
            )
