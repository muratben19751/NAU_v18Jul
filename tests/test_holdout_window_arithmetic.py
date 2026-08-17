"""Mühürlü holdout kapısının aritmetiği (AUTO 755b7880, 2026-08-17).

Kapı iki BAĞIMSIZ sabitle tanımlıydı: `OOS_HOLDOUT_DAYS` (takvim günü) ve
`HOLDOUT_MIN_TRADES` (sayım). Aralarındaki dönüşüm katsayısı stratejinin işlem
hızı — serbest bir değişken, üstelik önceki kapıların (maliyet-ayarlı benchmark)
AKTİF OLARAK küçülttüğü bir değişken. Sonuç: sistem aradığı profili üretip en
sonda kendi eliyle eliyordu.

Ölçüldü (QQQC, 22 yıl): 1-DAY'de mühürlü pencere 41 bar, 20 giriş isteniyordu —
barların %49'u, her 2 barda bir yeni pozisyon. Giriş + tutma + çıkış üçlüsü
bunu tanım gereği dışlıyor.

Wiki References
---------------
Bkz: [[nau_auto_kosusu_755b7880_2026_08_17]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import pandas as pd
import pytest


def _series(days: int, freq: str = "1D") -> pd.DataFrame:
    n = days if freq == "1D" else days * 24
    idx = pd.date_range("2003-09-10", periods=max(n, 10), freq=freq, tz="UTC")
    return pd.DataFrame({"close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0}, index=idx)


# ---------------------------------------------------------------------------
# Pencere artık örneklemin oranı
# ---------------------------------------------------------------------------


def test_long_sample_gets_a_window_worth_measuring():
    """22 yıllık seride 60 gün mühürlemek örneklemin %0,7'siydi."""
    from web.routes.agent_backtest import OOS_HOLDOUT_DAYS, holdout_window_days

    w = holdout_window_days(_series(22 * 365))
    assert w > 10 * OOS_HOLDOUT_DAYS, f"22 yılda pencere hâlâ dar: {w} gün"


def test_short_sample_keeps_the_old_floor():
    """Kısa örneklemde DAVRANIŞ DEĞİŞMEDİ — 60 gün taban olarak duruyor."""
    from web.routes.agent_backtest import OOS_HOLDOUT_DAYS, holdout_window_days

    assert holdout_window_days(_series(365)) == OOS_HOLDOUT_DAYS
    assert holdout_window_days(_series(120)) == OOS_HOLDOUT_DAYS


def test_window_scales_monotonically_with_the_sample():
    from web.routes.agent_backtest import holdout_window_days

    ws = [holdout_window_days(_series(y * 365)) for y in (1, 3, 10, 22)]
    assert ws == sorted(ws), f"pencere örneklemle birlikte büyümüyor: {ws}"


def test_empty_frame_does_not_explode():
    from web.routes.agent_backtest import OOS_HOLDOUT_DAYS, holdout_window_days

    assert holdout_window_days(None) == OOS_HOLDOUT_DAYS
    assert holdout_window_days(pd.DataFrame()) == OOS_HOLDOUT_DAYS


def test_the_split_leaves_the_bulk_of_the_sample_for_training():
    """Pencere büyüdü ama eğitim verisi yenmemeli."""
    from web.routes.agent_backtest import _split_holdout

    df = _series(22 * 365)
    trimmed, hold = _split_holdout(df)
    assert hold is not None and len(hold) > 0
    assert len(trimmed) / len(df) > 0.8, "eğitim verisinin %20'sinden fazlası gitti"


def test_the_sealed_slice_is_the_tail_and_does_not_overlap_training():
    """Sızıntı yok: mühür sondan alınır ve eğitimle çakışmaz (embargo dahil)."""
    from web.routes.agent_backtest import _split_holdout

    df = _series(22 * 365)
    trimmed, hold = _split_holdout(df)
    assert hold.index[-1] == df.index[-1]
    assert trimmed.index[-1] < hold.index[0], "eğitim mühürlü pencereye taşıyor"


# ---------------------------------------------------------------------------
# Ulaşılabilirlik adaydan bağımsız ve ÖNDEN bilinebilir
# ---------------------------------------------------------------------------


def test_the_measured_failure_case_is_flagged():
    """Gerçek vaka: 41 bar, 20 giriş — her 2 barda bir. Uyarı ateşlemeli."""
    from web.routes.agent_backtest import holdout_feasibility

    rate, warn = holdout_feasibility(41)
    assert rate > 0.4
    assert warn and "every 2.0 bars" in warn


def test_a_reachable_window_is_silent():
    """Uyarı gürültü olmamalı — ulaşılabilir pencerede susmalı."""
    from web.routes.agent_backtest import holdout_feasibility

    for n in (200, 862, 6013):
        rate, warn = holdout_feasibility(n)
        assert warn is None, f"{n} bar ulaşılabilir ama uyarı çıktı (%{100 * rate:.0f})"


def test_feasibility_needs_no_candidate():
    """Bilgi mühür anında var: pencere kaç bar + kaç giriş isteniyor.

    Bu yüzden uyarı LLM masrafı BAŞLAMADAN önce basılabiliyor; eski davranış
    aynı bilgiyi zincirin tamamı koştuktan sonra veriyordu (127 dk, $9,19).
    """
    import inspect

    from web.routes.agent_backtest import holdout_feasibility

    sig = inspect.signature(holdout_feasibility)
    assert list(sig.parameters) == ["n_bars"], (
        "ulaşılabilirlik adaya bağlanmış — o zaman önden söylenemez"
    )


def test_zero_bar_window_is_reported_not_crashed():
    from web.routes.agent_backtest import holdout_feasibility

    rate, warn = holdout_feasibility(0)
    assert warn and "no bars" in warn


@pytest.mark.parametrize("n_bars", [41, 82, 862])
def test_reported_cadence_matches_the_arithmetic(n_bars):
    """Mesajdaki sayı hesapla tutmalı — operatör buna bakıp karar verecek."""
    from web.routes.agent_backtest import HOLDOUT_MIN_TRADES, holdout_feasibility

    rate, _ = holdout_feasibility(n_bars)
    assert rate == pytest.approx(HOLDOUT_MIN_TRADES / n_bars)


# ---------------------------------------------------------------------------
# Kapı gevşemedi
# ---------------------------------------------------------------------------


def test_the_min_trades_threshold_is_untouched():
    """Pencere büyüdü, EŞİK aynı: bu bir gevşetme değil, birim düzeltmesi."""
    from web.routes.agent_backtest import HOLDOUT_MIN_TRADES, _holdout_promotion_verdict

    ok, why = _holdout_promotion_verdict(HOLDOUT_MIN_TRADES - 1, 0.1, 1.0, 0.1)
    assert not ok and "need" in why
