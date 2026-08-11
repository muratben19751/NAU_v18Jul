"""NAU-parite bloklarında bar başına tek hesap (H4, DeepR 2026-08-11).

`block_library_nau._nau_cached` aynı mumda aynı (gösterge, parametre) penceresi
için `indicators.calc_*`'ı BİR KEZ çalıştırır: aynı blok hem entry hem exit
rolünde kullanıldığında ya da ateşleyen bir blok için `composer._build_reason`
snapshot'ı da hesapladığında ikinci tam tarama yapılmaz.

KRİTİK: bu bir artımlı (incremental) gösterge state'i DEĞİL — H1940'ın sabit
260'lık penceresi ve dolayısıyla NAU paritesi aynen duruyor. Önbellek yalnız
*aynı mum içinde*, *aynı girdiyle* yapılan tekrar çağrıyı eler. Aşağıdaki
testler hem tasarrufu hem de "yeni mumda mutlaka yeniden hesapla" garantisini
çiviliyor.

Wiki References
---------------
See: [[nau_performans_denetimi]], [[webapp_module_map]].
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import block_library_nau as nau
import indicators as ind


def _series(n: int = 400, seed: int = 9):
    rng = random.Random(seed)
    price = 100.0
    highs, lows, closes = [], [], []
    for _ in range(n):
        price *= 1.0 + rng.gauss(0.0, 0.004)
        highs.append(price * 1.002)
        lows.append(price * 0.998)
        closes.append(price)
    return highs, lows, closes


def _strategy(highs, lows, closes):
    return SimpleNamespace(
        _highs=highs, _lows=lows, _closes=closes, _prev_state={}, _indicators={}
    )


def _block(typ, **params):
    return SimpleNamespace(params=params, role="entry", type=typ)


class _Counter:
    """calc_* çağrılarını sayan sarmalayıcı."""

    def __init__(self, monkeypatch, name):
        self.n = 0
        real = getattr(ind, name)

        def counted(*a, **k):
            self.n += 1
            return real(*a, **k)

        monkeypatch.setattr(ind, name, counted)


def test_entry_and_exit_share_one_adx_computation_per_bar(monkeypatch):
    highs, lows, closes = _series()
    strat = _strategy(highs, lows, closes)
    counter = _Counter(monkeypatch, "calc_adx")

    entry = SimpleNamespace(params={"period": 14}, role="entry", type="adx_threshold")
    exit_b = SimpleNamespace(params={"period": 14}, role="exit", type="adx_threshold")

    nau._eval_adx_threshold(strat, 0, entry, closes)
    nau._eval_adx_threshold(strat, 1, exit_b, closes)
    nau._snap_adx_threshold(strat, 0, entry, closes)
    assert counter.n == 1, "aynı mumda aynı pencere birden fazla kez hesaplandı"


def test_different_params_are_cached_separately(monkeypatch):
    highs, lows, closes = _series()
    strat = _strategy(highs, lows, closes)
    counter = _Counter(monkeypatch, "calc_adx")

    nau._eval_adx_threshold(strat, 0, _block("adx_threshold", period=14), closes)
    nau._eval_adx_threshold(strat, 1, _block("adx_threshold", period=20), closes)
    nau._eval_adx_threshold(strat, 2, _block("adx_threshold", period=14), closes)
    assert counter.n == 2


def test_new_bar_always_recomputes(monkeypatch):
    """Yeni mum = yeni pencere: parite gereği MUTLAKA yeniden hesaplanmalı."""
    highs, lows, closes = _series()
    strat = _strategy(highs, lows, closes)
    counter = _Counter(monkeypatch, "calc_adx")
    block = _block("adx_threshold", period=14)

    for extra in range(5):
        nau._eval_adx_threshold(strat, 0, block, closes)
        nau._eval_adx_threshold(strat, 0, block, closes)  # aynı mum → önbellek
        highs.append(highs[-1] * 1.001)
        lows.append(lows[-1] * 1.001)
        closes.append(closes[-1] * 1.001)
    assert counter.n == 5, f"bar başına tam bir hesap bekleniyordu, {counter.n} oldu"


def test_stoch_rsi_and_wave_trend_are_cached_too(monkeypatch):
    highs, lows, closes = _series()
    strat = _strategy(highs, lows, closes)
    c_stoch = _Counter(monkeypatch, "calc_stoch_rsi")
    c_wt = _Counter(monkeypatch, "calc_wave_trend")

    sb = _block("stoch_rsi_cross", rsi_period=14, stoch_period=14)
    wb = _block("wave_trend_cross", channel_len=10, avg_len=21)
    for _ in range(3):
        nau._eval_stoch_rsi_cross(strat, 0, sb, closes)
        nau._eval_wave_trend_cross(strat, 1, wb, closes)
    nau._snap_stoch_rsi_cross(strat, 0, sb, closes)
    nau._snap_wave_trend_cross(strat, 1, wb, closes)
    assert c_stoch.n == 1
    assert c_wt.n == 1


def test_cached_value_equals_the_uncached_value():
    """Önbellek DEĞERİ değiştirmiyor — parite kontrolü."""
    highs, lows, closes = _series()
    strat = _strategy(highs, lows, closes)
    block = _block("adx_threshold", period=14, threshold=20.0)

    direct = ind.calc_adx(
        nau._nau_win(highs), nau._nau_win(lows), nau._nau_win(closes), 14
    )
    first = nau._eval_adx_threshold(strat, 0, block, closes)
    second = nau._eval_adx_threshold(strat, 0, block, closes)
    assert first == second
    expected = None
    if direct["adx"] >= 20.0:
        expected = "long" if direct["plusDI"] > direct["minusDI"] else "short"
    assert first == expected


def test_cross_state_still_advances_once_per_bar():
    """Önbellek yalnız calc_*'ı eliyor; kesişim state'i her çağrıda güncel
    kalmalı (aynı mumda iki çağrı sahte kesişim üretmemeli)."""
    highs, lows, closes = _series(n=600)
    strat = _strategy(highs, lows, closes)
    block = _block("wave_trend_cross", channel_len=10, avg_len=21)

    nau._eval_wave_trend_cross(strat, 0, block, closes)
    state_after_first = strat._prev_state["wavetrend_0"]
    nau._eval_wave_trend_cross(strat, 0, block, closes)
    assert strat._prev_state["wavetrend_0"] == state_after_first


def test_strategy_without_settable_attributes_still_works():
    """Öznitelik atanamayan bir duck-type gelirse önbelleksiz ama DOĞRU çalış."""

    class Frozen:
        __slots__ = ("_highs", "_lows", "_closes", "_prev_state")

        def __init__(self, highs, lows, closes):
            self._highs = highs
            self._lows = lows
            self._closes = closes
            self._prev_state = {}

    highs, lows, closes = _series()
    strat = Frozen(highs, lows, closes)
    block = _block("adx_threshold", period=14, threshold=20.0)
    out_a = nau._eval_adx_threshold(strat, 0, block, closes)
    out_b = nau._eval_adx_threshold(strat, 0, block, closes)
    assert out_a == out_b
