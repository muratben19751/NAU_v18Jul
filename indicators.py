"""Saf-Python teknik indikatör kütüphanesi (bağımlılıksız).

Kaynak: E:/myAI_Projects/NAU_ev/backend/app/lib/indicators.py — M27/M33 portu.
Matematik birebir korunmuştur; tek uyarlama: NAU'daki ``Kline`` nesne listesi
yerine hizalı ``highs/lows/closes/volumes`` float listeleri alınır (composer
buffer'ları ve custom blok ``indicators`` sözlüğüyle aynı şekil). Kline tabanlı
fonksiyonlar (calc_atr/calc_adx/calc_wave_trend/…) bu yüzden
``(highs, lows, closes, …)`` imzasına çevrildi.

Hem builtin bloklar (composer) hem LLM-üretimi custom bloklar (codegate
whitelist + modül enjeksiyonu) buradaki ``calc_*`` fonksiyonlarını kullanır —
RSI/ATR/ADX/StochRSI/WaveTrend matematiği elle yeniden yazılmaz.
"""

from __future__ import annotations

import math


def calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
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
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def calc_rsi_series(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return []
    result: list[float] = []
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


def sma(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    result: list[float] = []
    for i in range(period - 1, len(values)):
        s = 0.0
        for j in range(i - period + 1, i + 1):
            s += values[j]
        result.append(s / period)
    return result


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    prev = sum(values[:period]) / period
    result.append(prev)
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        result.append(prev)
    return result


def calc_stoch_rsi(
    closes: list[float],
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_period: int = 3,
    d_period: int = 3,
) -> dict:
    if len(closes) < rsi_period + stoch_period + 1:
        return {"k": 50.0, "d": 50.0}
    rsi_values = calc_rsi_series(closes, rsi_period)
    if len(rsi_values) < stoch_period:
        return {"k": 50.0, "d": 50.0}
    stoch_k: list[float] = []
    for i in range(stoch_period - 1, len(rsi_values)):
        window = rsi_values[i - stoch_period + 1 : i + 1]
        mn = min(window)
        mx = max(window)
        rng = mx - mn
        stoch_k.append(50.0 if rng == 0 else ((rsi_values[i] - mn) / rng) * 100.0)
    k_smoothed = sma(stoch_k, k_period)
    d_smoothed = sma(k_smoothed, d_period)
    return {
        "k": k_smoothed[-1] if k_smoothed else 50.0,
        "d": d_smoothed[-1] if d_smoothed else 50.0,
    }


def _tail3(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[list[float], list[float], list[float], int]:
    """Üç seriyi ortak (en kısa) uzunluğa kuyruktan hizala."""
    n = min(len(highs), len(lows), len(closes))
    return highs[-n:], lows[-n:], closes[-n:], n


def calc_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    h, l, c, n = _tail3(highs, lows, closes)  # noqa: E741
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


def calc_adx(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> dict | None:
    h, l, c, n = _tail3(highs, lows, closes)  # noqa: E741
    if n < period * 2 + 1:
        return None
    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        up_move = h[i] - h[i - 1]
        down_move = l[i - 1] - l[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    sm_tr = sum(tr[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])

    dx_values: list[float] = []

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


def calc_volume_change(volumes: list[float], lookback: int = 20) -> float:
    if len(volumes) < lookback + 1:
        return 0.0
    recent = volumes[-1]
    avg_volume = sum(volumes[-lookback - 1 : -1]) / lookback
    if avg_volume == 0:
        return 0.0
    return ((recent - avg_volume) / avg_volume) * 100.0


def _find_swing_highs(prices, rsi_series, lookback, offset):
    points = []
    for i in range(lookback, len(prices) - lookback):
        is_high = True
        for j in range(1, lookback + 1):
            if prices[i] < prices[i - j] or prices[i] < prices[i + j]:
                is_high = False
                break
        if is_high:
            # i - offset < 0: TS'de undefined (etkisiz nokta) — Python'da sona sarmasın
            rsi = rsi_series[i - offset] if i - offset >= 0 else None
            points.append({"index": i, "price": prices[i], "rsi": rsi})
    return points


def _find_swing_lows(prices, rsi_series, lookback, offset):
    points = []
    for i in range(lookback, len(prices) - lookback):
        is_low = True
        for j in range(1, lookback + 1):
            if prices[i] > prices[i - j] or prices[i] > prices[i + j]:
                is_low = False
                break
        if is_low:
            rsi = rsi_series[i - offset] if i - offset >= 0 else None
            points.append({"index": i, "price": prices[i], "rsi": rsi})
    return points


def detect_rsi_divergence(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    rsi_period: int = 14,
    swing_lookback: int = 5,
) -> dict:
    h, l, c, n = _tail3(highs, lows, closes)  # noqa: E741
    if n < rsi_period + swing_lookback * 2 + 10:
        return {"type": "none", "strength": 0.0}
    rsi_series = calc_rsi_series(c, rsi_period)
    rsi_offset = n - len(rsi_series)
    swing_highs = _find_swing_highs(h, rsi_series, swing_lookback, rsi_offset)
    swing_lows = _find_swing_lows(l, rsi_series, swing_lookback, rsi_offset)

    if len(swing_highs) >= 2:
        prev = swing_highs[-2]
        last = swing_highs[-1]
        # rsi None (TS: undefined) -> karşılaştırmalar false
        if (
            last["rsi"] is not None
            and prev["rsi"] is not None
            and last["price"] > prev["price"]
            and last["rsi"] < prev["rsi"]
        ):
            price_ratio = (last["price"] - prev["price"]) / prev["price"]
            rsi_diff = prev["rsi"] - last["rsi"]
            return {
                "type": "bearish",
                "strength": min(1.0, (price_ratio * 100 + rsi_diff) / 20),
            }

    if len(swing_lows) >= 2:
        prev = swing_lows[-2]
        last = swing_lows[-1]
        if (
            last["rsi"] is not None
            and prev["rsi"] is not None
            and last["price"] < prev["price"]
            and last["rsi"] > prev["rsi"]
        ):
            price_ratio = (prev["price"] - last["price"]) / prev["price"]
            rsi_diff = last["rsi"] - prev["rsi"]
            return {
                "type": "bullish",
                "strength": min(1.0, (price_ratio * 100 + rsi_diff) / 20),
            }

    last_idx = n - 1
    last_close = c[last_idx]
    last_rsi = rsi_series[-1]

    if len(swing_highs) >= 1:
        prev = swing_highs[-1]
        if (
            prev["rsi"] is not None
            and last_close > prev["price"]
            and last_rsi < prev["rsi"] - 2
        ):
            price_ratio = (last_close - prev["price"]) / prev["price"]
            rsi_diff = prev["rsi"] - last_rsi
            return {
                "type": "forming_bearish",
                "strength": min(1.0, (price_ratio * 100 + rsi_diff) / 25),
            }

    if len(swing_lows) >= 1:
        prev = swing_lows[-1]
        if (
            prev["rsi"] is not None
            and last_close < prev["price"]
            and last_rsi > prev["rsi"] + 2
        ):
            price_ratio = (prev["price"] - last_close) / prev["price"]
            rsi_diff = last_rsi - prev["rsi"]
            return {
                "type": "forming_bullish",
                "strength": min(1.0, (price_ratio * 100 + rsi_diff) / 25),
            }

    return {"type": "none", "strength": 0.0}


def calc_nadaraya_watson(
    closes: list[float], bandwidth: float = 6, multiplier: float = 3.0
) -> dict | None:
    if len(closes) < 30:
        return None
    n = len(closes)
    y_hat: list[float] = []
    for i in range(n):
        weight_sum = 0.0
        value_sum = 0.0
        for j in range(n):
            dist = (i - j) / bandwidth
            w = math.exp(-0.5 * dist * dist)
            weight_sum += w
            value_sum += w * closes[j]
        y_hat.append(value_sum / weight_sum if weight_sum > 1e-10 else closes[i])
    residual_sq_sum = 0.0
    for i in range(n):
        residual_sq_sum += (closes[i] - y_hat[i]) ** 2
    std = math.sqrt(residual_sq_sum / n)
    reg = y_hat[n - 1]
    upper = reg + multiplier * std
    lower = reg - multiplier * std
    half_band = multiplier * std
    pos = (
        max(-1.0, min(1.0, (closes[n - 1] - reg) / half_band))
        if half_band > 1e-10
        else 0.0
    )
    slope_len = min(5, n - 1)
    slope = y_hat[n - 1] - y_hat[n - 1 - slope_len]
    slope_threshold = std * 0.1
    trend = (
        "up"
        if slope > slope_threshold
        else "down"
        if slope < -slope_threshold
        else "flat"
    )
    return {
        "regression": reg,
        "upper": upper,
        "lower": lower,
        "position": pos,
        "trend": trend,
    }


def calc_wave_trend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    channel_len: int = 10,
    avg_len: int = 21,
    signal_len: int = 4,
) -> dict | None:
    h, l, c, n = _tail3(highs, lows, closes)  # noqa: E741
    if n < channel_len + avg_len + signal_len + 10:
        return None
    hlc3 = [(h[i] + l[i] + c[i]) / 3 for i in range(n)]
    esa = ema(hlc3, channel_len)
    if not esa:
        return None
    esa_offset = len(hlc3) - len(esa)
    diff = [abs(hlc3[i + esa_offset] - esa[i]) for i in range(len(esa))]
    d = ema(diff, channel_len)
    if not d:
        return None
    d_offset = len(diff) - len(d)
    ci: list[float] = []
    for i in range(len(d)):
        esa_idx = i + d_offset
        hlc_idx = esa_idx + esa_offset
        d_val = d[i]
        ci.append(
            (hlc3[hlc_idx] - esa[esa_idx]) / (0.015 * d_val) if d_val > 1e-10 else 0.0
        )
    wt1_arr = ema(ci, avg_len)
    if len(wt1_arr) < signal_len:
        return None
    wt2_arr = sma(wt1_arr, signal_len)
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



