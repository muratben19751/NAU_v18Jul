"""WFO penceresi takvimde sabit, eşiği sayımdaydı — mühürlü kapıyla aynı hata.

AUTO'nun Walk-Forward aşaması pencereyi 6 ay eğitim / 2 ay test / 3 ay adım
diye SABİT yazıyordu; geçerlilik eşiği ise `WFO_MIN_TRADES` (5) bir SAYIM.
Aradaki dönüşüm katsayısı stratejinin işlem hızı — yani serbest bir değişken.

ÖLÇÜLDÜ (diskteki AUTO artefaktları, 178 pencere): pencere başına işlem
dağılımı {0: 70, 1: 88, 2: 20}. Hiçbir pencere 3'e bile ulaşmamış; ölçüt hiç
konuşmamış, ama GA maliyeti her koşuda ödenmiş. Kapı bunu dürüstçe "ölçülmedi"
sayıyor, yani sonuç YANLIŞ değil YOK — ödenen bedel gerçek.

Wiki References
---------------
Bkz: [[nau_holdout_dogrulama_turu_2026_08_18]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import pandas as pd

from auto.robustness import (
    WFO_BASE_STEP_MONTHS,
    WFO_BASE_TEST_MONTHS,
    WFO_BASE_TRAIN_MONTHS,
    WFO_MIN_TRADES,
    WFO_MIN_WINDOWS,
    wfo_window_months,
)
from backtest_robustness import _wfo_window_bounds

BASE = (WFO_BASE_TRAIN_MONTHS, WFO_BASE_TEST_MONTHS, WFO_BASE_STEP_MONTHS)


def _series(n: int, freq: str = "1D") -> pd.DataFrame:
    idx = pd.date_range("2003-09-10", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"close": 1.0}, index=idx)


def _windows(df, months) -> int:
    train, test, step = months[:3]
    return len(_wfo_window_bounds(df.index[0], df.index[-1], train, test, step))


def test_the_measured_case_gets_a_window_worth_measuring():
    """52 işlem / 5.159 bar: eski pencere ~0,4 giriş taşıyordu."""
    df = _series(5159)
    train, test, step, _note = wfo_window_months(df, 52)

    rate = 52 / len(df)
    bars_per_month = len(df) / ((df.index[-1] - df.index[0]).days / 30.44)
    old_expected = WFO_BASE_TEST_MONTHS * bars_per_month * rate
    new_expected = test * bars_per_month * rate

    assert old_expected < 1, f"vaka değişmiş: eski pencere {old_expected:.1f} giriş"
    assert new_expected > 3 * old_expected, (
        f"pencere anlamlı büyümedi: {old_expected:.2f} → {new_expected:.2f}"
    )
    assert (train, test, step) != BASE


def test_a_hopeless_pace_is_announced_up_front():
    """Ölçüt susacaksa bunu ÖNDEN söyle — zincirin sonunda '0/0' yazmak yerine."""
    df = _series(5159)
    train, test, step, note = wfo_window_months(df, 52)
    assert note and "probably stay silent" in note
    # Cümledeki her sayı doğrulanabilir olmalı.
    assert f"a {test}-month test window" in note
    assert f"({_windows(df, (train, test, step))} windows)" in note
    assert f"under the {WFO_MIN_TRADES}" in note


def test_a_fast_enough_candidate_gets_no_warning():
    df = _series(5159)
    _train, _test, _step, note = wfo_window_months(df, 400)
    assert note is None


def test_widening_never_drops_below_the_window_floor():
    """Pencereyi büyütmenin bedeli pencere SAYISI; oran az örneklemden konuşmasın."""
    df = _series(5159)
    months = wfo_window_months(df, 52)
    assert _windows(df, months) >= WFO_MIN_WINDOWS


def test_short_timeframes_keep_the_old_behaviour():
    """1 saatlik seride pencere zaten yeterli bar taşıyor — taban korunur."""
    df = _series(26280, "1h")  # ~3 yıl
    train, test, step, note = wfo_window_months(df, 150)
    assert (train, test, step) == BASE and note is None


def test_the_window_shape_is_preserved_when_it_scales():
    """6/2/3'ün oranı korunmalı: eğitim 3×test, adım 1,5×test."""
    df = _series(5159)
    train, test, step, _ = wfo_window_months(df, 52)
    assert train == 3 * test
    assert step == round(1.5 * test)


def test_an_unmeasurable_pace_falls_back_to_the_base():
    """Hız yoksa uydurma: bilinen sabite dön."""
    df = _series(5159)
    for n in (None, 0):
        assert wfo_window_months(df, n) == (*BASE, None)


def test_a_broken_frame_does_not_take_the_suite_down():
    assert wfo_window_months(None, 52) == (*BASE, None)
    assert wfo_window_months(pd.DataFrame(), 52) == (*BASE, None)


def test_the_suite_uses_the_derived_window_not_a_literal():
    """Sabit 6/2/3 geri gelirse test kırılsın — düzeltme bir literal değil."""
    import inspect

    import auto.robustness as ar

    src = inspect.getsource(ar.run_full_robustness)
    assert "wfo_window_months(" in src
    assert "train_months=_wf_train" in src
    assert "train_months=6" not in src, "sabit pencere geri gelmiş"
