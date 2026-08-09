"""Classic built-in block behavior: eval / snapshot / on_start / lookback /
validate for the 9 non-NAU-parity blocks (ma_cross, rsi_threshold,
price_breakout, momentum, volume_spike, ema_cross, bollinger_break,
macd_cross, atr_stop).

composer.py decomposition (Adım 2, safe-first slice): extracted verbatim
from composer.py. Each function only duck-types its `strategy` argument
(reads `_prev_state`/`_indicators`/`_highs`/`_lows`/`_closes`/`_volumes`,
same as before the move) — no cross-reference to anything else in
composer.py, and BLOCK_REGISTRY (staying in composer.py; the registry
core is out of this session's scope) reads these back via a plain import,
same as before.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

from nautilus_trader.indicators import (
    AverageTrueRange,
    BollingerBands,
    ExponentialMovingAverage,
    RelativeStrengthIndex,
)


def _eval_ma_cross(strategy, idx, block, closes):
    fast_n = block.params.get("fast", 10)
    slow_n = block.params.get("slow", 30)
    direction = block.params.get("direction", "up")
    if len(closes) < slow_n:
        return None
    fast_ma = sum(closes[-fast_n:]) / fast_n
    slow_ma = sum(closes[-slow_n:]) / slow_n
    diff = fast_ma - slow_ma
    prev = strategy._prev_state.get(idx, diff)
    strategy._prev_state[idx] = diff
    fired_up = prev <= 0 < diff
    fired_down = prev >= 0 > diff
    if block.role == "exit":
        return "exit" if (fired_up if direction == "up" else fired_down) else None
    if direction == "up" and fired_up:
        return "long"
    if direction == "down" and fired_down:
        return "short"
    return None


def _eval_rsi_threshold(strategy, idx, block, closes):
    ind = strategy._indicators.get(idx, {})
    rsi = ind.get("rsi")
    if rsi is None or not rsi.initialized:
        return None
    thr = block.params.get("threshold", 30.0)
    cross = block.params.get("cross", "below")
    # H6: Nautilus RelativeStrengthIndex.value produces ∈ [0,1); the threshold is
    # on a 0-100 scale (default 30). The scale mismatch made the block DEAD CODE
    # (prev>=30 never happens). Scale rsi.value to 0-100 (NAU calc_rsi convention).
    val = rsi.value * 100.0
    prev = strategy._prev_state.get(idx, val)
    strategy._prev_state[idx] = val
    fired_below = prev >= thr > val
    fired_above = prev <= thr < val
    if block.role == "exit":
        return "exit" if (fired_below if cross == "below" else fired_above) else None
    if cross == "below" and fired_below:
        return "long"
    if cross == "above" and fired_above:
        return "short"
    return None


def _eval_price_breakout(strategy, idx, block, closes):
    n = block.params.get("lookback", 20)
    direction = block.params.get("direction", "high")
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1) : -1]
    last = closes[-1]
    fired_high = last > max(window)
    fired_low = last < min(window)
    if block.role == "exit":
        return "exit" if (fired_high if direction == "high" else fired_low) else None
    if direction == "high" and fired_high:
        return "long"
    if direction == "low" and fired_low:
        return "short"
    return None


def _eval_momentum(strategy, idx, block, closes):
    n = block.params.get("lookback", 10)
    sign = block.params.get("sign", "positive")
    if len(closes) < n + 1:
        return None
    change = closes[-1] - closes[-n - 1]
    prev = strategy._prev_state.get(idx, change)
    strategy._prev_state[idx] = change
    fired_pos = prev <= 0 < change
    fired_neg = prev >= 0 > change
    if block.role == "exit":
        return "exit" if (fired_pos if sign == "positive" else fired_neg) else None
    if sign == "positive" and fired_pos:
        return "long"
    if sign == "negative" and fired_neg:
        return "short"
    return None


def _eval_volume_spike(strategy, idx, block, closes):
    """Volume spike/dry-up: last volume vs average of the previous N candles."""
    n = int(block.params.get("period", 20))
    mult = float(block.params.get("mult", 2.0))
    direction = block.params.get("direction", "above")
    vols = strategy._volumes  # flat list buffer — no copy needed (read-only)
    if n < 1 or len(vols) < n + 1:
        return None
    avg = sum(vols[-n - 1 : -1]) / n
    if avg <= 0:
        return None
    ratio = vols[-1] / avg
    fired = ratio >= mult if direction == "above" else ratio <= (1.0 / mult)
    # Edge trigger: don't re-fire on every candle while the condition persists
    prev_fired = strategy._prev_state.get(idx, False)
    strategy._prev_state[idx] = fired
    if not fired or prev_fired:
        return None
    if block.role == "exit":
        return "exit"
    # Volume carries no direction info — at the spike, long/short by candle direction
    return "long" if closes[-1] >= closes[-2] else "short"


def _eval_ema_cross(strategy, idx, block, closes):
    ind = strategy._indicators.get(idx, {})
    fast = ind.get("fast")
    slow = ind.get("slow")
    if fast is None or slow is None or not fast.initialized or not slow.initialized:
        return None
    diff = fast.value - slow.value
    prev = strategy._prev_state.get(idx, diff)
    strategy._prev_state[idx] = diff
    direction = block.params.get("direction", "up")
    fired_up = prev <= 0 < diff
    fired_down = prev >= 0 > diff
    if block.role == "exit":
        return "exit" if (fired_up if direction == "up" else fired_down) else None
    if direction == "up" and fired_up:
        return "long"
    if direction == "down" and fired_down:
        return "short"
    return None


def _eval_bollinger_break(strategy, idx, block, closes):
    ind = strategy._indicators.get(idx, {})
    bb = ind.get("bb")
    if bb is None or not bb.initialized:
        return None
    side = block.params.get("side", "lower")
    # L14: mode parameter — 'legacy' (default) preserves the current behavior
    # EXACTLY (both bands → long; old specs in the catalog are not broken).
    # 'breakout': upper→long, lower→short. 'revert' (mean reversion):
    # upper→short, lower→long. Short signals are subject to the allow_short gate.
    mode = block.params.get("mode", "legacy")
    last = closes[-1] if closes else 0.0
    fired_upper = last >= bb.upper
    fired_lower = last <= bb.lower
    fired = fired_upper if side == "upper" else fired_lower
    if block.role == "exit":
        return "exit" if fired else None
    if not fired:
        return None
    if mode == "breakout":
        return "long" if fired_upper and side == "upper" else "short"
    if mode == "revert":
        return "short" if fired_upper and side == "upper" else "long"
    return "long"  # legacy


def _eval_macd_cross(strategy, idx, block, closes):
    ind = strategy._indicators.get(idx, {})
    fast = ind.get("fast")
    slow = ind.get("slow")
    if fast is None or slow is None or not fast.initialized or not slow.initialized:
        return None
    macd = fast.value - slow.value
    prev = strategy._prev_state.get(idx, macd)
    strategy._prev_state[idx] = macd
    direction = block.params.get("direction", "up")
    fired_up = prev <= 0 < macd
    fired_down = prev >= 0 > macd
    if block.role == "exit":
        return "exit" if (fired_up if direction == "up" else fired_down) else None
    if direction == "up" and fired_up:
        return "long"
    if direction == "down" and fired_down:
        return "short"
    return None


def _eval_atr_stop(strategy, idx, block, closes):
    if block.role != "exit":
        return None
    ind = strategy._indicators.get(idx, {})
    atr = ind.get("atr")
    if atr is None or not atr.initialized or not closes:
        return None
    mult = float(block.params.get("mult", 3.0))
    key_hi = f"atr_hi_{idx}"
    key_lo = f"atr_lo_{idx}"
    last = closes[-1]
    hi = strategy._prev_state.get(key_hi, last)
    lo = strategy._prev_state.get(key_lo, last)
    is_long = strategy.portfolio.is_net_long(strategy._iid())
    is_short = strategy.portfolio.is_net_short(strategy._iid())
    if not is_long and not is_short:
        strategy._prev_state[key_hi] = last
        strategy._prev_state[key_lo] = last
        return None
    if is_long:
        hi = max(hi, last)
        strategy._prev_state[key_hi] = hi
        if last <= hi - atr.value * mult:
            return "exit"
    elif is_short:
        lo = min(lo, last)
        strategy._prev_state[key_lo] = lo
        if last >= lo + atr.value * mult:
            return "exit"
    return None


# Snapshot (indicator values at decision time) per built-in block. Called when
# the signal fires; the returned dict is shown in the trade's "entry/exit reason"
# line. On error returns None (caller wraps in try/except). Custom blocks have no
# hook → only label+params are shown.


def _snap_ma_cross(strategy, idx, block, closes):
    fast_n = block.params.get("fast", 10)
    slow_n = block.params.get("slow", 30)
    if len(closes) < slow_n:
        return None
    fast_ma = sum(closes[-fast_n:]) / fast_n
    slow_ma = sum(closes[-slow_n:]) / slow_n
    return {"fast": round(fast_ma, 4), "slow": round(slow_ma, 4)}


def _snap_rsi_threshold(strategy, idx, block, closes):
    rsi = strategy._indicators.get(idx, {}).get("rsi")
    if rsi is None or not rsi.initialized:
        return None
    return {"rsi": round(rsi.value * 100.0, 2)}  # H6: 0-100 scale (same as thresholds)


def _snap_price_breakout(strategy, idx, block, closes):
    n = block.params.get("lookback", 20)
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1) : -1]
    return {
        "close": round(closes[-1], 4),
        "max": round(max(window), 4),
        "min": round(min(window), 4),
    }


def _snap_momentum(strategy, idx, block, closes):
    n = block.params.get("lookback", 10)
    if len(closes) < n + 1:
        return None
    return {"change": round(closes[-1] - closes[-n - 1], 4)}


def _snap_volume_spike(strategy, idx, block, closes):
    n = int(block.params.get("period", 20))
    vols = strategy._volumes
    if n < 1 or len(vols) < n + 1:
        return None
    avg = sum(vols[-n - 1 : -1]) / n
    if avg <= 0:
        return None
    return {"ratio": round(vols[-1] / avg, 2), "avg": round(avg, 2)}


def _snap_ema_pair(strategy, idx, block, closes):
    ind = strategy._indicators.get(idx, {})
    fast, slow = ind.get("fast"), ind.get("slow")
    if fast is None or slow is None or not fast.initialized or not slow.initialized:
        return None
    return {"fast": round(fast.value, 4), "slow": round(slow.value, 4)}


def _snap_bollinger_break(strategy, idx, block, closes):
    bb = strategy._indicators.get(idx, {}).get("bb")
    if bb is None or not bb.initialized or not closes:
        return None
    return {
        "close": round(closes[-1], 4),
        "upper": round(bb.upper, 4),
        "lower": round(bb.lower, 4),
    }


def _snap_atr_stop(strategy, idx, block, closes):
    atr = strategy._indicators.get(idx, {}).get("atr")
    if atr is None or not atr.initialized or not closes:
        return None
    out = {"close": round(closes[-1], 4), "atr": round(atr.value, 4)}
    hi = strategy._prev_state.get(f"atr_hi_{idx}")
    lo = strategy._prev_state.get(f"atr_lo_{idx}")
    if hi is not None:
        out["hi"] = round(hi, 4)
    if lo is not None:
        out["lo"] = round(lo, 4)
    return out


# on_start (indicator registration) per built-in block. Only defined for blocks
# that need Nautilus indicators.


def _onstart_rsi_threshold(strategy, idx, block):
    rsi = RelativeStrengthIndex(int(block.params.get("period", 14)))
    strategy._indicators[idx] = {"rsi": rsi}
    strategy.register_indicator_for_bars(strategy.config.bar_type, rsi)


def _onstart_ema_cross(strategy, idx, block):
    fast = ExponentialMovingAverage(int(block.params.get("fast", 12)))
    slow = ExponentialMovingAverage(int(block.params.get("slow", 26)))
    strategy._indicators[idx] = {"fast": fast, "slow": slow}
    strategy.register_indicator_for_bars(strategy.config.bar_type, fast)
    strategy.register_indicator_for_bars(strategy.config.bar_type, slow)


def _onstart_bollinger_break(strategy, idx, block):
    bb = BollingerBands(
        int(block.params.get("period", 20)),
        float(block.params.get("k", 2.0)),
    )
    strategy._indicators[idx] = {"bb": bb}
    strategy.register_indicator_for_bars(strategy.config.bar_type, bb)


def _onstart_macd_cross(strategy, idx, block):
    fast = ExponentialMovingAverage(int(block.params.get("fast", 12)))
    slow = ExponentialMovingAverage(int(block.params.get("slow", 26)))
    strategy._indicators[idx] = {"fast": fast, "slow": slow}
    strategy.register_indicator_for_bars(strategy.config.bar_type, fast)
    strategy.register_indicator_for_bars(strategy.config.bar_type, slow)


def _onstart_atr_stop(strategy, idx, block):
    atr = AverageTrueRange(int(block.params.get("period", 14)))
    strategy._indicators[idx] = {"atr": atr}
    strategy.register_indicator_for_bars(strategy.config.bar_type, atr)


# max_lookback per built-in.


def _lb_ma_cross(params):
    return params.get("slow", 30)


def _lb_rsi_threshold(params):
    return params.get("period", 14) + 1


def _lb_price_breakout(params):
    return params.get("lookback", 20)


def _lb_momentum(params):
    return params.get("lookback", 10) + 1


def _lb_volume_spike(params):
    return int(params.get("period", 20)) + 1


def _lb_ema_cross(params):
    return params.get("slow", 26)


def _lb_bollinger_break(params):
    return params.get("period", 20)


def _lb_macd_cross(params):
    return params.get("slow", 26)


def _lb_atr_stop(params):
    return params.get("period", 14) + 1


# validate per built-in (only for those with cross-param constraints).


def _validate_cross_fast_slow(block):
    if block.params.get("slow", 0) <= block.params.get("fast", 0):
        return f"{block.type}: slow must be > fast."
    return None


def _validate_atr_stop(block):
    if block.role != "exit":
        return "atr_stop block can only be used in the exit role."
    return None
