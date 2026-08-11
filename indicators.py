"""Pure-Python technical indicator library (dependency-free).

Source: E:/myAI_Projects/NAU_ev/backend/app/lib/indicators.py — M27/M33 port.
The math is preserved exactly; the only adaptation: instead of NAU's ``Kline``
object list, aligned ``highs/lows/closes/volumes`` float lists are taken (same
shape as the composer buffers and the custom block ``indicators`` dict). Kline-based
functions (calc_atr/calc_adx/calc_wave_trend/…) were therefore converted to the
``(highs, lows, closes, …)`` signature.

Both builtin blocks (composer) and LLM-generated custom blocks (codegate
whitelist + module injection) use the ``calc_*`` functions here —
RSI/ATR/ADX/StochRSI/WaveTrend math is not rewritten by hand.

``calc_ewma_vol`` computes ``sqrt(ewma(log_return^2, span))`` — the EWMA
daily volatility estimate used by the composer's ``vol_target`` trade-size
mode (``ComposedStrategy._compute_qty``) for position sizing. Alpha =
2/(span+1), same as ``ema()`` in this module.

Wiki References
---------------
See: [[webapp_module_map]], [[strategy_and_actor]], [[nau_performans_denetimi]]
"""

from __future__ import annotations

import math


def calc_rsi(closes: list[float], period: int = 14) -> float:
    if period <= 0:
        raise ValueError("period must be positive")
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
    """H4 (DeepR 2026-08-11): hot loop, çünkü ``calc_stoch_rsi`` bunu her barda
    260'lık pencerede baştan çağırıyor. Aritmetik BİREBİR korundu (parite
    zorunluluğu); yalnız yorumlayıcı yükü azaltıldı: ``closes[i-1]`` yerine
    ``prev`` taşınıyor, ``result.append`` ve ``period - 1`` döngü dışına alındı.
    Wilder güncellemesinin bölme sırası aynı — sonuç bit-bit aynı."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if n < period + 1:
        return []
    avg_gain = 0.0
    avg_loss = 0.0
    prev = closes[0]
    for i in range(1, period + 1):
        cur = closes[i]
        diff = cur - prev
        prev = cur
        if diff > 0:
            avg_gain += diff
        else:
            avg_loss -= diff
    avg_gain /= period
    avg_loss /= period
    result: list[float] = [
        100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    ]
    append = result.append
    pm1 = period - 1
    for i in range(period + 1, n):
        cur = closes[i]
        diff = cur - prev
        prev = cur
        avg_gain = (avg_gain * pm1 + (diff if diff > 0 else 0.0)) / period
        avg_loss = (avg_loss * pm1 + (-diff if diff < 0 else 0.0)) / period
        append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return result


def sma(values: list[float], period: int) -> list[float]:
    """O(n) running sum — was O(n*period), re-summing the whole window on
    every step (flagged by DeepR 2026-08-08 [ORTA]; ema() right below already
    does the O(n) equivalent). Adding the new value and dropping the one that
    fell out of the window is the same arithmetic in a different order — a
    handful of ULPs of drift on long series, not a behavior change."""
    n = len(values)
    if n < period:
        return []
    window_sum = sum(values[:period])
    result: list[float] = [window_sum / period]
    append = result.append  # attribute lookup döngü dışına (H4 hot path)
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        append(window_sum / period)
    return result


def ema(values: list[float], period: int) -> list[float]:
    n = len(values)
    if n < period:
        return []
    k = 2.0 / (period + 1)
    # ``1 - k`` her adımda yeniden hesaplanıyordu; sabit bir double, bir kez
    # hesaplamak sonucu bit-bit aynı bırakır (H4 hot path — calc_wave_trend
    # bar başına üç kez ema çağırıyor).
    one_minus_k = 1 - k
    prev = sum(values[:period]) / period
    result: list[float] = [prev]
    append = result.append
    for i in range(period, n):
        prev = values[i] * k + prev * one_minus_k
        append(prev)
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
    m = len(rsi_values)
    if m < stoch_period:
        return {"k": 50.0, "d": 50.0}
    # H4 (DeepR 2026-08-11): eskiden her adımda ``rsi_values[i-13:i+1]`` dilimi
    # kopyalanıp min+max baştan taranıyordu — 260'lık pencerede 233 dilim × 2
    # tarama. Şimdi kayan pencere ekstremumu TAŞINIYOR: yeni değer mevcut
    # min/max'ı geçerse O(1) güncellenir, yalnız ekstremumu TUTAN eleman
    # pencereden düştüğünde yeniden taranır (rastgele veride ~1/stoch_period
    # adımda bir). Döndürülen mn/mx aynı float DEĞERLERİ olduğu için — min/max
    # eşitlikte hangi indeksi seçerse seçsin değer aynı — çıktı bit-bit aynı.
    head = rsi_values[0:stoch_period]
    mn = min(head)
    mx = max(head)
    mn_i = head.index(mn)
    mx_i = head.index(mx)
    v = rsi_values[stoch_period - 1]
    rng = mx - mn
    stoch_k: list[float] = [50.0 if rng == 0 else ((v - mn) / rng) * 100.0]
    append = stoch_k.append
    left = 1  # pencerenin sol kenarı (dahil)
    for i in range(stoch_period, m):
        v = rsi_values[i]
        if v <= mn:
            mn = v
            mn_i = i
        elif mn_i < left:  # eski minimum pencereden düştü → yeniden tara
            window = rsi_values[left : i + 1]
            mn = min(window)
            mn_i = left + window.index(mn)
        if v >= mx:
            mx = v
            mx_i = i
        elif mx_i < left:
            window = rsi_values[left : i + 1]
            mx = max(window)
            mx_i = left + window.index(mx)
        rng = mx - mn
        append(50.0 if rng == 0 else ((v - mn) / rng) * 100.0)
        left += 1
    k_smoothed = sma(stoch_k, k_period)
    d_smoothed = sma(k_smoothed, d_period)
    return {
        "k": k_smoothed[-1] if k_smoothed else 50.0,
        "d": d_smoothed[-1] if d_smoothed else 50.0,
    }


def _tail3(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[list[float], list[float], list[float], int]:
    """Align three series to a common (shortest) length from the tail.

    H4: üç seri zaten aynı boydaysa (``_nau_win`` yolunda HER ZAMAN öyle)
    ``x[-n:]`` üç gereksiz liste kopyası üretiyordu. Aynı boyda orijinal liste
    döner — çağıranların hiçbiri bu listeleri DEĞİŞTİRMİYOR, yalnız okuyor.
    """
    ln_h, ln_l, ln_c = len(highs), len(lows), len(closes)
    n = ln_h if ln_h < ln_l else ln_l
    if ln_c < n:
        n = ln_c
    return (
        highs if ln_h == n else highs[-n:],
        lows if ln_l == n else lows[-n:],
        closes if ln_c == n else closes[-n:],
        n,
    )


def calc_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    if period <= 0:
        raise ValueError("period must be positive")
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
    if period <= 0:
        raise ValueError("period must be positive")
    h, l, c, n = _tail3(highs, lows, closes)  # noqa: E741
    if n < period * 2 + 1:
        return None
    # H4 (DeepR 2026-08-11): eskiden bar başına dört ara liste (tr/plus_dm/
    # minus_dm ~n, dx_values ~n-period) ve HER adımda bir `push_dx()` closure
    # çağrısı + tek kullanımlık {"pDI","mDI"} sözlüğü üretiliyordu — 260'lık
    # pencerede ~1000 float + 246 sözlük, hepsi tek bir son değer için.
    # Artık tek geçiş: tr/±DM anında hesaplanır, DX akışta tüketilir.
    #
    # PARİTE: aritmetik sıra birebir korundu. Wilder tohumları hâlâ ``sum()``
    # ile — Python 3.12 ``sum()`` float'larda Neumaier toplaması yapıyor, elle
    # ``+=`` biriktirmek son ULP'de sapabilirdi; bu yüzden ilk ``period``
    # eleman küçük listelerde toplanıp ``sum()``'a veriliyor.
    seed_tr: list[float] = []
    seed_plus: list[float] = []
    seed_minus: list[float] = []
    ap_tr, ap_plus, ap_minus = seed_tr.append, seed_plus.append, seed_minus.append
    prev_h, prev_l, prev_c = h[0], l[0], c[0]
    for i in range(1, period + 1):
        cur_h, cur_l = h[i], l[i]
        ap_tr(max(cur_h - cur_l, abs(cur_h - prev_c), abs(cur_l - prev_c)))
        up_move = cur_h - prev_h
        down_move = prev_l - cur_l
        ap_plus(up_move if (up_move > down_move and up_move > 0) else 0.0)
        ap_minus(down_move if (down_move > up_move and down_move > 0) else 0.0)
        prev_h, prev_l, prev_c = cur_h, cur_l, c[i]

    sm_tr = sum(seed_tr)
    sm_plus = sum(seed_plus)
    sm_minus = sum(seed_minus)

    if sm_tr > 0:
        p_di = (sm_plus / sm_tr) * 100
        m_di = (sm_minus / sm_tr) * 100
    else:
        p_di = m_di = 0.0
    total = p_di + m_di
    dx = (abs(p_di - m_di) / total) * 100 if total > 0 else 0.0

    pm1 = period - 1
    # Wilder ADX tohumu: ilk `period` DX değeri (yine ``sum()`` ile).
    dx_seed: list[float] = [dx]
    ap_dx = dx_seed.append
    i = period + 1
    seed_stop = i + pm1 if i + pm1 < n else n
    while i < seed_stop:
        cur_h, cur_l = h[i], l[i]
        tr = max(cur_h - cur_l, abs(cur_h - prev_c), abs(cur_l - prev_c))
        up_move = cur_h - prev_h
        down_move = prev_l - cur_l
        prev_h, prev_l, prev_c = cur_h, cur_l, c[i]
        sm_tr = sm_tr - sm_tr / period + tr
        sm_plus = (
            sm_plus
            - sm_plus / period
            + (up_move if (up_move > down_move and up_move > 0) else 0.0)
        )
        sm_minus = (
            sm_minus
            - sm_minus / period
            + (down_move if (down_move > up_move and down_move > 0) else 0.0)
        )
        if sm_tr > 0:
            p_di = (sm_plus / sm_tr) * 100
            m_di = (sm_minus / sm_tr) * 100
        else:
            p_di = m_di = 0.0
        total = p_di + m_di
        dx = (abs(p_di - m_di) / total) * 100 if total > 0 else 0.0
        ap_dx(dx)
        i += 1

    if len(dx_seed) < period:
        return {"adx": dx, "plusDI": p_di, "minusDI": m_di}

    adx = sum(dx_seed) / period
    while i < n:
        cur_h, cur_l = h[i], l[i]
        tr = max(cur_h - cur_l, abs(cur_h - prev_c), abs(cur_l - prev_c))
        up_move = cur_h - prev_h
        down_move = prev_l - cur_l
        prev_h, prev_l, prev_c = cur_h, cur_l, c[i]
        sm_tr = sm_tr - sm_tr / period + tr
        sm_plus = (
            sm_plus
            - sm_plus / period
            + (up_move if (up_move > down_move and up_move > 0) else 0.0)
        )
        sm_minus = (
            sm_minus
            - sm_minus / period
            + (down_move if (down_move > up_move and down_move > 0) else 0.0)
        )
        if sm_tr > 0:
            p_di = (sm_plus / sm_tr) * 100
            m_di = (sm_minus / sm_tr) * 100
        else:
            p_di = m_di = 0.0
        total = p_di + m_di
        adx = (
            adx * pm1 + ((abs(p_di - m_di) / total) * 100 if total > 0 else 0.0)
        ) / period
        i += 1
    return {"adx": adx, "plusDI": p_di, "minusDI": m_di}


def calc_volume_change(volumes: list[float], lookback: int = 20) -> float:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if len(volumes) < lookback + 1:
        return 0.0
    recent = volumes[-1]
    avg_volume = sum(volumes[-lookback - 1 : -1]) / lookback
    if avg_volume == 0:
        return 0.0
    return ((recent - avg_volume) / avg_volume) * 100.0


def _find_swing_points(prices, rsi_series, lookback, offset, *, is_high):
    points = []
    for i in range(lookback, len(prices) - lookback):
        is_swing = True
        for j in range(1, lookback + 1):
            if is_high:
                beats_neighbors = (
                    prices[i] >= prices[i - j] and prices[i] >= prices[i + j]
                )
            else:
                beats_neighbors = (
                    prices[i] <= prices[i - j] and prices[i] <= prices[i + j]
                )
            if not beats_neighbors:
                is_swing = False
                break
        if is_swing:
            # i - offset < 0: undefined in TS (ineffective point) — don't wrap around to the end in Python
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
    swing_highs = _find_swing_points(
        h, rsi_series, swing_lookback, rsi_offset, is_high=True
    )
    swing_lows = _find_swing_points(
        l, rsi_series, swing_lookback, rsi_offset, is_high=False
    )

    if len(swing_highs) >= 2:
        prev = swing_highs[-2]
        last = swing_highs[-1]
        # rsi None (TS: undefined) -> comparisons false
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
    if bandwidth <= 0:
        raise ValueError("bandwidth must be positive")
    if len(closes) < 30:
        return None
    n = len(closes)
    # The Gaussian kernel is ~0 past a few bandwidths: at |i-j| = 6*bandwidth
    # (dist=6) w = exp(-18) ≈ 1.5e-8, far below float noise from the other
    # terms in this sum. Capping each row's j-range to that radius turns the
    # O(n²) double loop into O(n * bandwidth) without changing any output —
    # points outside the radius would have contributed nothing measurable.
    radius = max(1, math.ceil(6 * bandwidth))
    y_hat: list[float] = []
    for i in range(n):
        weight_sum = 0.0
        value_sum = 0.0
        for j in range(max(0, i - radius), min(n, i + radius + 1)):
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


def calc_ewma_vol(closes: list[float], span: int = 10) -> float | None:
    """sqrt(ewma(log_return^2, span)) — EWMA daily vol estimate.

    Alpha = 2/(span+1), same convention as ema() in this module and
    pandas .ewm(span=N). Returns None when fewer than span+1 bars are
    available (warmup incomplete).
    """
    if len(closes) < span + 1:
        return None
    alpha = 2.0 / (span + 1)
    # log(a/b) needs BOTH sides positive — a bad bar (delisting, a zero/negative
    # print, or a genuinely negative future price) on either side of the ratio
    # raised ValueError here; treated as a zero log-return instead, the same
    # "skip this step" convention the denominator check already used.
    lr0 = math.log(closes[1] / closes[0]) if closes[0] > 0 and closes[1] > 0 else 0.0
    ewma = lr0 * lr0
    for i in range(2, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        lr = math.log(cur / prev) if prev > 0 and cur > 0 else 0.0
        ewma = alpha * lr * lr + (1 - alpha) * ewma
    return math.sqrt(ewma) if ewma > 0 else None


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
    # H4 (DeepR 2026-08-11): boru hattı yedi ayrı geçişti (hlc3 → ema → diff →
    # ema → ci → ema → sma); iki komşu geçiş kaynaştırıldı. `esa` üretilirken
    # `diff` aynı döngüde, `d` üretilirken `ci` aynı döngüde doluyor. Aritmetik
    # ve sıra birebir aynı: ema tohumu hâlâ ``sum(values[:period])/period``,
    # yineleme hâlâ ``x*k + prev*(1-k)`` — yalnız ara liste taramaları ve
    # indeks aritmetiği ortadan kalktı.
    hlc3 = [(a + b + d_) / 3 for a, b, d_ in zip(h, l, c, strict=True)]
    if n < channel_len:
        return None
    k = 2.0 / (channel_len + 1)
    one_minus_k = 1 - k
    # esa = ema(hlc3, channel_len)  +  diff = |hlc3 - esa|  (tek geçiş)
    esa_v = sum(hlc3[:channel_len]) / channel_len
    esa: list[float] = [esa_v]
    diff: list[float] = [abs(hlc3[channel_len - 1] - esa_v)]
    ap_esa, ap_diff = esa.append, diff.append
    for i in range(channel_len, n):
        x = hlc3[i]
        esa_v = x * k + esa_v * one_minus_k
        ap_esa(esa_v)
        ap_diff(abs(x - esa_v))
    esa_offset = n - len(esa)  # == channel_len - 1
    n_diff = len(diff)
    if n_diff < channel_len:
        return None
    # d = ema(diff, channel_len)  +  ci  (tek geçiş)
    d_val = sum(diff[:channel_len]) / channel_len
    d_offset = channel_len - 1
    base = d_offset + esa_offset
    ci: list[float] = [
        (hlc3[base] - esa[d_offset]) / (0.015 * d_val) if d_val > 1e-10 else 0.0
    ]
    ap_ci = ci.append
    for i in range(channel_len, n_diff):
        d_val = diff[i] * k + d_val * one_minus_k
        ap_ci(
            (hlc3[i + esa_offset] - esa[i]) / (0.015 * d_val) if d_val > 1e-10 else 0.0
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
