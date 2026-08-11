"""H4 hot-path optimizasyonunun BİREBİR parite kanıtı (DeepR 2026-08-11).

`indicators.py`'deki `sma`/`ema`/`calc_rsi_series`/`calc_stoch_rsi`/`calc_adx`/
`calc_wave_trend` NAU-parite bloklarının bar başına maliyetini düşürmek için
yeniden yazıldı. `NAU_WINDOW=260` sabit pencere bir PARİTE gerekliliği (H1940):
artımlı state sinyalleri kaydırır. Bu yüzden optimizasyon *yalnız* yorumlayıcı
yükünü azalttı — aritmetik ve işlem SIRASI korundu.

Bu dosya bunu kanıtlar: aşağıdaki `_ref_*` fonksiyonları optimizasyon ÖNCESİ
sürümün (git 43f3d83:indicators.py) BİREBİR kopyasıdır. Testler rastgele
üretilmiş uzun serilerde, kayan 260'lık pencerelerin HER BİRİ için eski ve yeni
sürümün aynı float'ı (``==``, yuvarlama/tolerans YOK) döndürdüğünü doğrular.

Neden ``==`` ve neden tolerans yok: parite testinin amacı "yakın" değil "aynı"
olduğunu göstermek. Tek ULP'lik bir sapma bile eşik/kesişim bloklarında farklı
bir sinyal üretebilir ve backtest sonucunu değiştirebilir.

Python 3.12 notu: ``sum()`` float'larda Neumaier toplaması yapar; bu yüzden yeni
sürüm Wilder tohumlarını hâlâ ``sum()`` ile hesaplıyor (elle ``+=`` biriktirmek
son ULP'de sapardı). Aşağıdaki testler bu kısıtın tutulduğunu da doğrular.

Wiki References
---------------
See: [[nau_performans_denetimi]], [[webapp_module_map]].
"""

from __future__ import annotations

import math
import random

import pytest

import indicators as ind

# ---------------------------------------------------------------------------
# Optimizasyon ÖNCESİ sürümlerin birebir kopyası (referans / oracle).
# BU FONKSİYONLARI OPTİMİZE ETMEYİN — parite karşılaştırmasının çıpasıdır.
# ---------------------------------------------------------------------------


def _ref_sma(values, period):
    if len(values) < period:
        return []
    result = []
    window_sum = sum(values[:period])
    result.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def _ref_ema(values, period):
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = []
    prev = sum(values[:period]) / period
    result.append(prev)
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        result.append(prev)
    return result


def _ref_calc_rsi_series(closes, period=14):
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return []
    result = []
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            avg_gain += diff
        else:
            avg_loss -= diff
    avg_gain /= period
    avg_loss /= period
    result.append(
        100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    )
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (diff if diff > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-diff if diff < 0 else 0.0)) / period
        result.append(
            100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        )
    return result


def _ref_calc_stoch_rsi(closes, rsi_period=14, stoch_period=14, k_period=3, d_period=3):
    if len(closes) < rsi_period + stoch_period + 1:
        return {"k": 50.0, "d": 50.0}
    rsi_values = _ref_calc_rsi_series(closes, rsi_period)
    if len(rsi_values) < stoch_period:
        return {"k": 50.0, "d": 50.0}
    stoch_k = []
    for i in range(stoch_period - 1, len(rsi_values)):
        window = rsi_values[i - stoch_period + 1 : i + 1]
        mn = min(window)
        mx = max(window)
        rng = mx - mn
        stoch_k.append(50.0 if rng == 0 else ((rsi_values[i] - mn) / rng) * 100.0)
    k_smoothed = _ref_sma(stoch_k, k_period)
    d_smoothed = _ref_sma(k_smoothed, d_period)
    return {
        "k": k_smoothed[-1] if k_smoothed else 50.0,
        "d": d_smoothed[-1] if d_smoothed else 50.0,
    }


def _ref_tail3(highs, lows, closes):
    n = min(len(highs), len(lows), len(closes))
    return highs[-n:], lows[-n:], closes[-n:], n


def _ref_calc_atr(highs, lows, closes, period=14):
    if period <= 0:
        raise ValueError("period must be positive")
    h, l, c, n = _ref_tail3(highs, lows, closes)  # noqa: E741
    if n < period + 1:
        return None
    h = h[-(period + 1) :]
    l = l[-(period + 1) :]  # noqa: E741
    c = c[-(period + 1) :]
    atr = 0.0
    for i in range(1, len(h)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr += tr
    return atr / period


def _ref_calc_adx(highs, lows, closes, period=14):
    if period <= 0:
        raise ValueError("period must be positive")
    h, l, c, n = _ref_tail3(highs, lows, closes)  # noqa: E741
    if n < period * 2 + 1:
        return None
    tr = []
    plus_dm = []
    minus_dm = []
    for i in range(1, n):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        up_move = h[i] - h[i - 1]
        down_move = l[i - 1] - l[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    sm_tr = sum(tr[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])

    dx_values = []

    def push_dx():
        p_di = (sm_plus / sm_tr) * 100 if sm_tr > 0 else 0.0
        m_di = (sm_minus / sm_tr) * 100 if sm_tr > 0 else 0.0
        total = p_di + m_di
        dx_values.append((abs(p_di - m_di) / total) * 100 if total > 0 else 0.0)
        return {"pDI": p_di, "mDI": m_di}

    last = push_dx()
    for i in range(period, len(tr)):
        sm_tr = sm_tr - sm_tr / period + tr[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        last = push_dx()

    if len(dx_values) < period:
        return {
            "adx": dx_values[-1] if dx_values else 0.0,
            "plusDI": last["pDI"],
            "minusDI": last["mDI"],
        }

    adx = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx = (adx * (period - 1) + dx_values[i]) / period
    return {"adx": adx, "plusDI": last["pDI"], "minusDI": last["mDI"]}


def _ref_calc_wave_trend(highs, lows, closes, channel_len=10, avg_len=21, signal_len=4):
    h, l, c, n = _ref_tail3(highs, lows, closes)  # noqa: E741
    if n < channel_len + avg_len + signal_len + 10:
        return None
    hlc3 = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    esa = _ref_ema(hlc3, channel_len)
    if not esa:
        return None
    esa_offset = len(hlc3) - len(esa)
    diff = [abs(hlc3[i + esa_offset] - esa[i]) for i in range(len(esa))]
    d = _ref_ema(diff, channel_len)
    if not d:
        return None
    d_offset = len(diff) - len(d)
    ci = []
    for i in range(len(d)):
        esa_idx = i + d_offset
        hlc_idx = esa_idx + esa_offset
        d_val = d[i]
        ci.append(
            (hlc3[hlc_idx] - esa[esa_idx]) / (0.015 * d_val) if d_val > 1e-10 else 0.0
        )
    wt1_arr = _ref_ema(ci, avg_len)
    if len(wt1_arr) < signal_len:
        return None
    wt2_arr = _ref_sma(wt1_arr, signal_len)
    if len(wt2_arr) < 2:
        return None
    wt1 = wt1_arr[-1]
    wt2 = wt2_arr[-1]
    wt1_prev = wt1_arr[-2]
    wt2_prev = wt2_arr[-2] if len(wt2_arr) >= 2 else wt2
    signal = "neutral"
    if wt1_prev <= wt2_prev and wt1 > wt2:
        signal = "buy"
    elif wt1_prev >= wt2_prev and wt1 < wt2:
        signal = "sell"
    else:
        hist = wt1 - wt2
        hist_prev = wt1_prev - wt2_prev
        if hist < 0 and hist > hist_prev and wt1 < -53:
            signal = "approaching_buy"
        elif hist > 0 and hist < hist_prev and wt1 > 53:
            signal = "approaching_sell"
    return {
        "wt1": wt1,
        "wt2": wt2,
        "signal": signal,
        "overbought": wt1 > 60,
        "oversold": wt1 < -60,
    }


# ---------------------------------------------------------------------------
# Seri üreteçleri
# ---------------------------------------------------------------------------


def _random_ohlc(seed: int, n: int, *, vol: float = 0.004, flat_run: int = 0):
    """Rastgele yürüyüş OHLC. ``flat_run`` bir düzlük (rng==0 / diff==0) bölümü
    ekler: min==max ve avg_loss==0 dallarını da parite kapsamına sokar."""
    rng = random.Random(seed)
    price = 100.0
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for i in range(n):
        if flat_run and (i % (flat_run * 3)) < flat_run:
            pass  # fiyat sabit — düz pencere
        else:
            price *= 1.0 + rng.gauss(0.0, vol)
        hi = price * (1.0 + abs(rng.gauss(0.0, vol / 2)) if not flat_run else 1.0)
        lo = price * (1.0 - abs(rng.gauss(0.0, vol / 2)) if not flat_run else 1.0)
        highs.append(hi)
        lows.append(lo)
        closes.append(price)
    return highs, lows, closes


def _monotonic_ohlc(n: int):
    """Kesintisiz yükseliş: RSI'da ``avg_loss == 0`` (100.0 kısa devresi) ve
    StochRSI'da ``rng == 0`` (50.0 kısa devresi) dallarını tetikler."""
    closes = [100.0 + i for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


NAU_WINDOW = 260  # block_library_nau.NAU_WINDOW ile aynı sabit pencere


# ---------------------------------------------------------------------------
# Kayan pencere parite testleri — asıl kanıt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 7, 1234, 99991])
def test_sliding_window_bit_parity_all_hot_indicators(seed):
    """Rastgele uzun seride, HER 260'lık kayan pencere için eski==yeni."""
    n = 900
    highs, lows, closes = _random_ohlc(seed, n)
    checked = 0
    for end in range(NAU_WINDOW, n + 1):
        h = highs[end - NAU_WINDOW : end]
        lo = lows[end - NAU_WINDOW : end]
        c = closes[end - NAU_WINDOW : end]

        assert ind.calc_adx(h, lo, c, 14) == _ref_calc_adx(h, lo, c, 14)
        assert ind.calc_stoch_rsi(c, 14, 14) == _ref_calc_stoch_rsi(c, 14, 14)
        assert ind.calc_wave_trend(h, lo, c, 10, 21) == _ref_calc_wave_trend(
            h, lo, c, 10, 21
        )
        assert ind.calc_rsi_series(c, 14) == _ref_calc_rsi_series(c, 14)
        assert ind.calc_atr(h, lo, c, 14) == _ref_calc_atr(h, lo, c, 14)
        checked += 1
    assert checked == n - NAU_WINDOW + 1 == 641


def test_bit_parity_on_flat_and_monotonic_series():
    """Kısa devre dalları (min==max, avg_loss==0) da birebir aynı olmalı."""
    for highs, lows, closes in (
        _monotonic_ohlc(400),
        ([100.0] * 400, [100.0] * 400, [100.0] * 400),
        _random_ohlc(5, 400, flat_run=9),
    ):
        for end in range(NAU_WINDOW, 401, 7):
            h = highs[end - NAU_WINDOW : end]
            lo = lows[end - NAU_WINDOW : end]
            c = closes[end - NAU_WINDOW : end]
            assert ind.calc_adx(h, lo, c, 14) == _ref_calc_adx(h, lo, c, 14)
            assert ind.calc_stoch_rsi(c, 14, 14) == _ref_calc_stoch_rsi(c, 14, 14)
            assert ind.calc_wave_trend(h, lo, c, 10, 21) == _ref_calc_wave_trend(
                h, lo, c, 10, 21
            )
            assert ind.calc_rsi_series(c, 14) == _ref_calc_rsi_series(c, 14)


@pytest.mark.parametrize("period", [1, 2, 3, 7, 14, 20, 30, 60])
def test_calc_adx_bit_parity_across_periods(period):
    highs, lows, closes = _random_ohlc(42, 400)
    for end in (261, 320, 400):
        h, lo, c = highs[:end], lows[:end], closes[:end]
        assert ind.calc_adx(h, lo, c, period) == _ref_calc_adx(h, lo, c, period)


@pytest.mark.parametrize(
    ("rsi_p", "stoch_p", "k_p", "d_p"),
    [(14, 14, 3, 3), (2, 2, 1, 1), (7, 21, 3, 5), (21, 7, 2, 2), (14, 50, 3, 3)],
)
def test_calc_stoch_rsi_bit_parity_across_params(rsi_p, stoch_p, k_p, d_p):
    _, _, closes = _random_ohlc(2026, 500)
    for end in (260, 380, 500):
        c = closes[:end]
        assert ind.calc_stoch_rsi(c, rsi_p, stoch_p, k_p, d_p) == _ref_calc_stoch_rsi(
            c, rsi_p, stoch_p, k_p, d_p
        )


@pytest.mark.parametrize(
    ("ch", "av", "sig"), [(10, 21, 4), (5, 9, 3), (21, 34, 4), (3, 3, 2), (14, 28, 8)]
)
def test_calc_wave_trend_bit_parity_across_params(ch, av, sig):
    highs, lows, closes = _random_ohlc(777, 500)
    for end in (260, 380, 500):
        h, lo, c = highs[:end], lows[:end], closes[:end]
        assert ind.calc_wave_trend(h, lo, c, ch, av, sig) == _ref_calc_wave_trend(
            h, lo, c, ch, av, sig
        )


@pytest.mark.parametrize("period", [1, 2, 3, 4, 10, 21, 50])
def test_sma_and_ema_bit_parity(period):
    _, _, closes = _random_ohlc(31337, 300)
    assert ind.sma(closes, period) == _ref_sma(closes, period)
    assert ind.ema(closes, period) == _ref_ema(closes, period)
    assert ind.sma(closes[: period - 1], period) == _ref_sma(
        closes[: period - 1], period
    )
    assert ind.ema(closes[: period - 1], period) == _ref_ema(
        closes[: period - 1], period
    )


def test_unaligned_series_lengths_still_bit_parity():
    """`_tail3` artık aynı boyda kopya üretmiyor; FARKLI boylarda kırpma
    davranışının değişmediğini de doğrula."""
    highs, lows, closes = _random_ohlc(4242, 400)
    for cut_h, cut_l, cut_c in ((0, 5, 11), (17, 0, 3), (2, 9, 0)):
        h = highs[cut_h:]
        lo = lows[cut_l:]
        c = closes[cut_c:]
        assert ind.calc_adx(h, lo, c, 14) == _ref_calc_adx(h, lo, c, 14)
        assert ind.calc_atr(h, lo, c, 14) == _ref_calc_atr(h, lo, c, 14)
        assert ind.calc_wave_trend(h, lo, c, 10, 21) == _ref_calc_wave_trend(
            h, lo, c, 10, 21
        )


def test_tail3_does_not_mutate_or_alias_incorrectly():
    """Aynı boyda `_tail3` orijinal listeyi döndürüyor (kopya yok). Çağıranlar
    yalnız okuduğu için bu güvenli — girdi listeleri değişmemiş olmalı."""
    highs, lows, closes = _random_ohlc(11, 300)
    snap = (list(highs), list(lows), list(closes))
    h, lo, c, n = ind._tail3(highs, lows, closes)
    assert n == 300
    assert h is highs and lo is lows and c is closes
    ind.calc_adx(highs, lows, closes, 14)
    ind.calc_wave_trend(highs, lows, closes, 10, 21)
    ind.calc_atr(highs, lows, closes, 14)
    assert (highs, lows, closes) == snap


def test_guards_and_degenerate_inputs_unchanged():
    """Kısa seri / hatalı period davranışı da aynı kalmalı."""
    highs, lows, closes = _random_ohlc(3, 20)
    assert ind.calc_adx(highs, lows, closes, 14) is None  # n < 2*period+1 (29)
    assert _ref_calc_adx(highs, lows, closes, 14) is None
    assert ind.calc_wave_trend(highs, lows, closes, 10, 21) is None
    assert _ref_calc_wave_trend(highs, lows, closes, 10, 21) is None
    assert ind.calc_stoch_rsi(closes[:10], 14, 14) == {"k": 50.0, "d": 50.0}
    assert ind.calc_rsi_series(closes[:5], 14) == []
    with pytest.raises(ValueError):
        ind.calc_adx(highs, lows, closes, 0)
    with pytest.raises(ValueError):
        ind.calc_rsi_series(closes, 0)


def test_no_nan_or_inf_introduced():
    """Optimizasyon yeni bir NaN/Inf yolu açmamalı."""
    highs, lows, closes = _random_ohlc(8, 400)
    res = ind.calc_adx(highs, lows, closes, 14)
    assert res is not None
    for v in res.values():
        assert math.isfinite(v)
    wt = ind.calc_wave_trend(highs, lows, closes, 10, 21)
    assert wt is not None
    assert math.isfinite(wt["wt1"]) and math.isfinite(wt["wt2"])
    st = ind.calc_stoch_rsi(closes, 14, 14)
    assert math.isfinite(st["k"]) and math.isfinite(st["d"])
