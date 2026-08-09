"""NAU-parity built-in block behavior: the 4 blocks built on the
pure-python NAU_ev-parity indicator library (indicators.py) — adx_threshold,
stoch_rsi_cross, wave_trend_cross, donchian_channel.

composer.py decomposition (Adım 3, safe-first slice — the last step for
composer.py this session). Extracted verbatim from composer.py, alongside
NAU_WINDOW/_NAU_RECURSIVE_BLOCKS/_nau_win (the H1940 fixed-window fix these
4 blocks' recursive calc_* calls depend on). Each function only duck-types
its `strategy` arg and calls `indicators.calc_*`; zero cross-reference to
anything else in composer.py.

This is the one step in this session's plan that touches
`ComposedStrategy.__init__` at all — it reads NAU_WINDOW/
_NAU_RECURSIVE_BLOCKS directly (buffer-sizing for the H1940 fix) and now
gets them via the re-export below instead of module-local names; __init__'s
own code is otherwise unchanged. `ComposedStrategy`/`ComposedStrategyConfig`
themselves never move — see the decomposition plan's critical-decision
section.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

# ── M27: builtins built on top of the NAU parity library (indicators.py) ──
# Pure-python calc_* calls instead of a Nautilus indicator object: numerically
# parity-tested against NAU_ev, requires no on_start hook, does not enter the sandbox.

# H1940: these blocks use recursive calc_* (Wilder ADX, StochRSI, EMA-chained
# WaveTrend); the value depends on the SERIES LENGTH. The on_bar buffer is 4×cap →
# when cap trims, the window shrinks ~4× in a single candle so the indicator value
# JUMPS and produced a spurious cross/threshold signal. Fix: ALWAYS give calc_* a
# fixed-length window (last NAU_WINDOW candles) — independent of compaction, a stable
# value. Same fixed-window approach as NAU generic_strategy.py deque(maxlen=260).
NAU_WINDOW = 260

# RECURSIVE (Wilder/EMA seed) blocks that require the buffer to hold at least
# NAU_WINDOW candles so _nau_win can consistently return the last NAU_WINDOW candles.
# donchian is non-recursive (max/min) so it is unaffected by the swinging window — not included.
_NAU_RECURSIVE_BLOCKS = {"adx_threshold", "stoch_rsi_cross", "wave_trend_cross"}


def _nau_win(series):
    """Stable (fixed-length) window for calc_* — removes the compaction jump
    (H1940). If the series is shorter than NAU_WINDOW, returns it as is."""
    return series[-NAU_WINDOW:] if len(series) > NAU_WINDOW else series


def _eval_adx_threshold(strategy, idx, block, closes):
    import indicators as _ind

    period = int(block.params.get("period", 14))
    threshold = float(block.params.get("threshold", 25.0))
    highs, lows = _nau_win(strategy._highs), _nau_win(strategy._lows)
    res = _ind.calc_adx(highs, lows, _nau_win(closes), period)
    if res is None:
        return None
    adx = res.get("adx", 0.0)
    if block.role == "exit":
        return "exit" if adx < threshold else None
    if adx < threshold:
        return None
    return "long" if res.get("plusDI", 0.0) > res.get("minusDI", 0.0) else "short"


def _snap_adx_threshold(strategy, idx, block, closes):
    import indicators as _ind

    period = int(block.params.get("period", 14))
    res = _ind.calc_adx(
        _nau_win(strategy._highs), _nau_win(strategy._lows), _nau_win(closes), period
    )
    if res is None:
        return None
    return {
        "adx": round(res.get("adx", 0.0), 2),
        "+DI": round(res.get("plusDI", 0.0), 2),
        "-DI": round(res.get("minusDI", 0.0), 2),
    }


def _lb_adx_threshold(params):
    return 2 * int(params.get("period", 14)) + 10


def _eval_stoch_rsi_cross(strategy, idx, block, closes):
    import indicators as _ind

    rsi_p = int(block.params.get("rsi_period", 14))
    st_p = int(block.params.get("stoch_period", 14))
    oversold = float(block.params.get("oversold", 20.0))
    overbought = float(block.params.get("overbought", 80.0))
    res = _ind.calc_stoch_rsi(_nau_win(closes), rsi_p, st_p)
    k, d = res.get("k", 50.0), res.get("d", 50.0)
    key = f"stochrsi_{idx}"
    # Warmup guard: calc_stoch_rsi returns a (50,50) sentinel (not None) until
    # enough candles accumulate. Do NOT SEED the sentinel as prev — otherwise the first
    # real (k,d) reads pk==pd_==50 and produces a spurious cross. A real k==d==50
    # cannot fire a signal anyway (both k>d and k<d are false), so skipping is safe
    # (same pattern as the wave_trend None-guard).
    if k == 50.0 and d == 50.0:
        return None
    prev = strategy._prev_state.get(key)
    strategy._prev_state[key] = (k, d)
    if prev is None:
        return None
    pk, pd_ = prev
    cross_up = pk <= pd_ and k > d
    cross_dn = pk >= pd_ and k < d
    if block.role == "exit":
        return "exit" if (cross_up or cross_dn) else None
    if cross_up and min(pk, k) < oversold:
        return "long"
    if cross_dn and max(pk, k) > overbought:
        return "short"
    return None


def _snap_stoch_rsi_cross(strategy, idx, block, closes):
    import indicators as _ind

    res = _ind.calc_stoch_rsi(
        _nau_win(closes),
        int(block.params.get("rsi_period", 14)),
        int(block.params.get("stoch_period", 14)),
    )
    return {"K": round(res.get("k", 50.0), 2), "D": round(res.get("d", 50.0), 2)}


def _lb_stoch_rsi_cross(params):
    return int(params.get("rsi_period", 14)) + int(params.get("stoch_period", 14)) + 12


def _eval_wave_trend_cross(strategy, idx, block, closes):
    import indicators as _ind

    ch = int(block.params.get("channel_len", 10))
    av = int(block.params.get("avg_len", 21))
    os_lv = float(block.params.get("os_level", -30.0))
    ob_lv = float(block.params.get("ob_level", 30.0))
    res = _ind.calc_wave_trend(
        _nau_win(strategy._highs), _nau_win(strategy._lows), _nau_win(closes), ch, av
    )
    if res is None:
        return None
    wt1, wt2 = res.get("wt1", 0.0), res.get("wt2", 0.0)
    key = f"wavetrend_{idx}"
    prev = strategy._prev_state.get(key)
    strategy._prev_state[key] = (wt1, wt2)
    if prev is None:
        return None
    p1, p2 = prev
    cross_up = p1 <= p2 and wt1 > wt2
    cross_dn = p1 >= p2 and wt1 < wt2
    if block.role == "exit":
        return "exit" if (cross_up or cross_dn) else None
    if cross_up and wt1 < os_lv:
        return "long"
    if cross_dn and wt1 > ob_lv:
        return "short"
    return None


def _snap_wave_trend_cross(strategy, idx, block, closes):
    import indicators as _ind

    res = _ind.calc_wave_trend(
        _nau_win(strategy._highs),
        _nau_win(strategy._lows),
        _nau_win(closes),
        int(block.params.get("channel_len", 10)),
        int(block.params.get("avg_len", 21)),
    )
    if res is None:
        return None
    return {"WT1": round(res.get("wt1", 0.0), 2), "WT2": round(res.get("wt2", 0.0), 2)}


def _lb_wave_trend_cross(params):
    return int(params.get("channel_len", 10)) + int(params.get("avg_len", 21)) + 4 + 15


def _eval_donchian_channel(strategy, idx, block, closes):
    period = int(block.params.get("period", 20))
    mode = block.params.get("mode", "breakout")
    highs, lows = strategy._highs, strategy._lows
    if len(highs) < period + 1 or len(lows) < period + 1 or not closes:
        return None
    upper = max(
        highs[-period - 1 : -1]
    )  # previous N candles EXCLUDING the current candle
    lower = min(lows[-period - 1 : -1])
    last = closes[-1]
    if block.role == "exit":
        # Channel-mid reverse-direction cross → exit.
        mid = (upper + lower) / 2.0
        key = f"donchian_{idx}"
        prev = strategy._prev_state.get(key)
        strategy._prev_state[key] = last
        if prev is None:
            return None
        crossed = (prev <= mid < last) or (prev >= mid > last)
        return "exit" if crossed else None
    if last > upper:
        return "long" if mode == "breakout" else "short"
    if last < lower:
        return "short" if mode == "breakout" else "long"
    return None


def _snap_donchian_channel(strategy, idx, block, closes):
    period = int(block.params.get("period", 20))
    highs, lows = strategy._highs, strategy._lows
    if len(highs) < period + 1 or len(lows) < period + 1:
        return None
    return {
        "upper": round(max(highs[-period - 1 : -1]), 4),
        "lower": round(min(lows[-period - 1 : -1]), 4),
    }


def _lb_donchian_channel(params):
    return int(params.get("period", 20)) + 5
