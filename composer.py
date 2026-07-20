"""Visual signal-block composer for Nautilus strategies.

A `ComposedStrategy` is a Nautilus `Strategy` subclass whose behavior is
determined by a list of `SignalBlock` records — no dynamic code, safe by
construction. Each block emits +1 (long entry), -1 (long exit), or 0 on
each bar. Signals are OR-combined: any entry block firing opens a long;
any exit block firing closes it.

Aligned with Nautilus wiki (Strategy & Actor + Order Flow Pipeline):
- on_bar callback drives all logic
- self.order_factory.market → self.submit_order (default Risk Engine route)
- self.close_all_positions on exit signal

Wiki References
---------------
See: [[strategy_and_actor]], [[order_flow_pipeline]]

Blocks emit signals; the composer wires them into a Nautilus `Strategy`. Order submission enters exactly into [[order_flow_pipeline]] (`submit_order` → OrderEmulator/ExecutionAlgorithms/RiskEngine/Adapter).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from nautilus_trader.indicators import (
    AverageTrueRange,
    BollingerBands,
    ExponentialMovingAverage,
    RelativeStrengthIndex,
)
from nautilus_trader.model import (
    Bar,
    BarType,
    InstrumentId,
)
from nautilus_trader.model.enums import (
    OrderSide,
    OrderType,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

BlockRole = Literal["entry", "exit"]
BlockType = str  # dynamic — built-ins + custom

# Strategy-level option literals
EntryExitLogic = Literal["OR", "AND"]
OrderTypeOpt = Literal["market", "limit"]
SLType = Literal["percent", "atr"]
TPType = Literal["percent", "atr", "off"]
TradeSizeMode = Literal[
    "fixed", "fixed_usdt", "percent_equity", "atr_target", "vol_target"
]


# --------------------------------------------------------------------------
# Built-in block metadata (was BLOCK_CATALOG; now part of BLOCK_REGISTRY meta)

_BUILTIN_META: dict[str, dict] = {
    "ma_cross": {
        "label": "MA Cross",
        "params": {
            "fast": {"type": "int", "min": 2, "max": 100, "default": 10},
            "slow": {"type": "int", "min": 5, "max": 300, "default": 30},
            "direction": {"type": "enum", "options": ["up", "down"], "default": "up"},
        },
        "wiki_refs": [
            "wiki/entities/strategy_and_actor.md",
            "wiki/concepts/event_driven_architecture.md",
        ],
        "help": (
            "Fast and slow moving average cross. `up` = triggers when the fast crosses "
            "the slow upward. Recalculated on each new close in `on_bar`."
        ),
    },
    "rsi_threshold": {
        "label": "RSI Threshold",
        "params": {
            "period": {"type": "int", "min": 2, "max": 50, "default": 14},
            "threshold": {"type": "float", "min": 5.0, "max": 95.0, "default": 30.0},
            "cross": {
                "type": "enum",
                "options": ["below", "above"],
                "default": "below",
            },
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "Triggers when RSI crosses the given threshold upward/downward. "
            "`below` = when it drops below the threshold (oversold signal)."
        ),
    },
    "price_breakout": {
        "label": "Price Breakout",
        "params": {
            "lookback": {"type": "int", "min": 3, "max": 200, "default": 20},
            "direction": {
                "type": "enum",
                "options": ["high", "low"],
                "default": "high",
            },
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "Triggers when the highest/lowest close of the last N candles is broken. "
            "Donchian logic."
        ),
    },
    "momentum": {
        "label": "Momentum Signal",
        "params": {
            "lookback": {"type": "int", "min": 2, "max": 100, "default": 10},
            "sign": {
                "type": "enum",
                "options": ["positive", "negative"],
                "default": "positive",
            },
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "The sign of the return over the last N candles. `positive` = net rise over the last N candles."
        ),
    },
    "volume_spike": {
        "label": "Volume Spike",
        "params": {
            "period": {"type": "int", "min": 5, "max": 100, "default": 20},
            "mult": {"type": "float", "min": 1.1, "max": 10.0, "default": 2.0},
            "direction": {
                "type": "enum",
                "options": ["above", "below"],
                "default": "above",
            },
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "Triggers when the last candle's volume exceeds `mult` times the average "
            "volume of the previous N candles (`above`), or drops below it (`below` — "
            "volume dry-up). Can be combined in AND logic with other blocks for "
            "volume-confirmed entries/exits."
        ),
    },
    "ema_cross": {
        "label": "EMA Cross (Nautilus)",
        "params": {
            "fast": {"type": "int", "min": 2, "max": 100, "default": 12},
            "slow": {"type": "int", "min": 5, "max": 300, "default": 26},
            "direction": {"type": "enum", "options": ["up", "down"], "default": "up"},
        },
        "wiki_refs": [
            "wiki/entities/strategy_and_actor.md",
            "wiki/concepts/event_driven_architecture.md",
        ],
        "help": (
            "Uses the Nautilus native `ExponentialMovingAverage` indicator. "
            "EMA is a smoothed MA: it gives more weight to recent candles. `up` = fast EMA "
            "crosses slow EMA upward (`down` for short)."
        ),
    },
    "bollinger_break": {
        "label": "Bollinger Breakout (Nautilus)",
        "params": {
            "period": {"type": "int", "min": 5, "max": 200, "default": 20},
            "k": {"type": "float", "min": 0.5, "max": 5.0, "default": 2.0},
            "side": {"type": "enum", "options": ["upper", "lower"], "default": "lower"},
            "mode": {
                "type": "enum",
                "options": ["legacy", "breakout", "revert"],
                "default": "legacy",
            },
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "Nautilus `BollingerBands(period, k)` indicator. When price touches the upper band "
            "`upper` (breakout/momentum entry), when it touches the lower band `lower` (mean reversion). "
            "mode=legacy LONG on both bands (old behavior); breakout: upper→long, "
            "lower→short; revert: upper→short, lower→long (shorts require allow_short)."
        ),
    },
    "macd_cross": {
        "label": "EMA Diff Cross (MACD-like)",
        "params": {
            "fast": {"type": "int", "min": 2, "max": 60, "default": 12},
            "slow": {"type": "int", "min": 5, "max": 200, "default": 26},
            "direction": {"type": "enum", "options": ["up", "down"], "default": "up"},
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "Triggers when the difference of two Nautilus `ExponentialMovingAverage` indicators "
            "crosses zero. `up` = fast EMA - slow EMA crosses zero upward "
            "(momentum entry). Does not include a signal line."
        ),
    },
    "atr_stop": {
        "label": "ATR Stop (exit only)",
        "params": {
            "period": {"type": "int", "min": 5, "max": 100, "default": 14},
            "mult": {"type": "float", "min": 0.5, "max": 10.0, "default": 3.0},
        },
        "wiki_refs": ["wiki/entities/execution_engine.md"],
        "help": (
            "Uses the Nautilus `AverageTrueRange` indicator. Exit triggers when price is pulled "
            "down by ATR × mult from the last close. Used only in the exit role."
        ),
    },
    # ── M27: builtins built on top of the NAU parity library (indicators.py) ──
    "adx_threshold": {
        "label": "ADX Trend Strength (NAU)",
        "params": {
            "period": {"type": "int", "min": 7, "max": 50, "default": 14},
            "threshold": {"type": "float", "min": 10.0, "max": 50.0, "default": 25.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_adx (Wilder) — exact parity with NAU_ev. Entry: "
            "when ADX ≥ threshold, +DI>−DI → long, −DI>+DI → short. Exit: when ADX "
            "drops below threshold (trend weakened) exit."
        ),
    },
    "stoch_rsi_cross": {
        "label": "StochRSI K/D Cross (NAU)",
        "params": {
            "rsi_period": {"type": "int", "min": 5, "max": 50, "default": 14},
            "stoch_period": {"type": "int", "min": 5, "max": 50, "default": 14},
            "oversold": {"type": "float", "min": 5.0, "max": 40.0, "default": 20.0},
            "overbought": {"type": "float", "min": 60.0, "max": 95.0, "default": 80.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_stoch_rsi (K=3/D=3 SMA smoothing). Entry: when K crosses D "
            "upward in the oversold zone, long; when it crosses down in the "
            "overbought zone, short. Exit: reverse cross."
        ),
    },
    "wave_trend_cross": {
        "label": "WaveTrend Cross (NAU)",
        "params": {
            "channel_len": {"type": "int", "min": 5, "max": 30, "default": 10},
            "avg_len": {"type": "int", "min": 10, "max": 50, "default": 21},
            "os_level": {"type": "float", "min": -80.0, "max": 0.0, "default": -30.0},
            "ob_level": {"type": "float", "min": 0.0, "max": 80.0, "default": 30.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_wave_trend (LazyBear WT1/WT2). Entry: when WT1 crosses WT2 "
            "upward below os_level, long; when it crosses down above ob_level, "
            "short. Exit: reverse cross."
        ),
    },
    "donchian_channel": {
        "label": "Donchian Channel",
        "params": {
            "period": {"type": "int", "min": 5, "max": 100, "default": 20},
            "mode": {
                "type": "enum",
                "options": ["breakout", "revert"],
                "default": "breakout",
            },
        },
        "wiki_refs": [],
        "help": (
            "Actual high/low Donchian channel (price_breakout was a close-only "
            "breakout). breakout: when close exceeds the highest of the previous N candles, "
            "long / when it breaks the lowest, short. revert: the opposite. Exit: when close crosses "
            "the channel mid in the reverse direction."
        ),
    },
}


# --------------------------------------------------------------------------
# Built-in eval / lookback / on_start functions
# Each takes `strategy` (ComposedStrategy) as first arg so it can read
# indicators / _prev_state / portfolio. Keeps behavior identical to prior
# monolithic _eval_block/on_start.


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


# --------------------------------------------------------------------------
# BLOCK_REGISTRY — the single source of truth for block behavior.
# Each entry: { meta, eval, on_start, max_lookback, validate, builtin }
# Custom blocks are added by `_load_custom_blocks()` at import time and via
# `register_custom_block()` at runtime.

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
    upper = max(highs[-period - 1 : -1])  # previous N candles EXCLUDING the current candle
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


BLOCK_REGISTRY: dict[str, dict[str, Any]] = {
    "ma_cross": {
        "meta": _BUILTIN_META["ma_cross"],
        "eval": _eval_ma_cross,
        "snapshot": _snap_ma_cross,
        "on_start": None,
        "max_lookback": _lb_ma_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "rsi_threshold": {
        "meta": _BUILTIN_META["rsi_threshold"],
        "eval": _eval_rsi_threshold,
        "snapshot": _snap_rsi_threshold,
        "on_start": _onstart_rsi_threshold,
        "max_lookback": _lb_rsi_threshold,
        "validate": None,
        "builtin": True,
    },
    "price_breakout": {
        "meta": _BUILTIN_META["price_breakout"],
        "eval": _eval_price_breakout,
        "snapshot": _snap_price_breakout,
        "on_start": None,
        "max_lookback": _lb_price_breakout,
        "validate": None,
        "builtin": True,
    },
    "momentum": {
        "meta": _BUILTIN_META["momentum"],
        "eval": _eval_momentum,
        "snapshot": _snap_momentum,
        "on_start": None,
        "max_lookback": _lb_momentum,
        "validate": None,
        "builtin": True,
    },
    "volume_spike": {
        "meta": _BUILTIN_META["volume_spike"],
        "eval": _eval_volume_spike,
        "snapshot": _snap_volume_spike,
        "on_start": None,
        "max_lookback": _lb_volume_spike,
        "validate": None,
        "builtin": True,
    },
    "ema_cross": {
        "meta": _BUILTIN_META["ema_cross"],
        "eval": _eval_ema_cross,
        "snapshot": _snap_ema_pair,
        "on_start": _onstart_ema_cross,
        "max_lookback": _lb_ema_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "bollinger_break": {
        "meta": _BUILTIN_META["bollinger_break"],
        "eval": _eval_bollinger_break,
        "snapshot": _snap_bollinger_break,
        "on_start": _onstart_bollinger_break,
        "max_lookback": _lb_bollinger_break,
        "validate": None,
        "builtin": True,
    },
    "macd_cross": {
        "meta": _BUILTIN_META["macd_cross"],
        "eval": _eval_macd_cross,
        "snapshot": _snap_ema_pair,
        "on_start": _onstart_macd_cross,
        "max_lookback": _lb_macd_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "atr_stop": {
        "meta": _BUILTIN_META["atr_stop"],
        "eval": _eval_atr_stop,
        "snapshot": _snap_atr_stop,
        "on_start": _onstart_atr_stop,
        "max_lookback": _lb_atr_stop,
        "validate": _validate_atr_stop,
        "builtin": True,
    },
    "adx_threshold": {
        "meta": _BUILTIN_META["adx_threshold"],
        "eval": _eval_adx_threshold,
        "snapshot": _snap_adx_threshold,
        "on_start": None,
        "max_lookback": _lb_adx_threshold,
        "validate": None,
        "builtin": True,
    },
    "stoch_rsi_cross": {
        "meta": _BUILTIN_META["stoch_rsi_cross"],
        "eval": _eval_stoch_rsi_cross,
        "snapshot": _snap_stoch_rsi_cross,
        "on_start": None,
        "max_lookback": _lb_stoch_rsi_cross,
        "validate": None,
        "builtin": True,
    },
    "wave_trend_cross": {
        "meta": _BUILTIN_META["wave_trend_cross"],
        "eval": _eval_wave_trend_cross,
        "snapshot": _snap_wave_trend_cross,
        "on_start": None,
        "max_lookback": _lb_wave_trend_cross,
        "validate": None,
        "builtin": True,
    },
    "donchian_channel": {
        "meta": _BUILTIN_META["donchian_channel"],
        "eval": _eval_donchian_channel,
        "snapshot": _snap_donchian_channel,
        "on_start": None,
        "max_lookback": _lb_donchian_channel,
        "validate": None,
        "builtin": True,
    },
}


# BLOCK_CATALOG — meta-only view of BLOCK_REGISTRY. Kept as a plain dict for
# template compatibility (iteration, .items(), [key]). Rebuilt whenever the
# registry changes.

BLOCK_CATALOG: dict[str, dict] = {}


def _rebuild_catalog() -> None:
    # Build the new dict first, then apply it with a single clear+update — narrows
    # the window in which a lock-free reader sees an empty/half catalog (or
    # 'changed size during iteration'). The dict IDENTITY is preserved (template refs).
    new = {k: entry["meta"] for k, entry in BLOCK_REGISTRY.items()}
    BLOCK_CATALOG.clear()
    BLOCK_CATALOG.update(new)


_REGISTRY_LOCK = threading.Lock()
_rebuild_catalog()


class _PortfolioView:
    """L25: minimal Portfolio view passed to custom blocks.

    No existing block uses portfolio in block scanning; the three whitelisted
    queries are exposed as passthroughs (when called without arguments the
    strategy's own instrument is assumed). The real Portfolio's mutation surface
    stays closed to blocks.
    """

    __slots__ = ("_strategy",)

    def __init__(self, strategy) -> None:
        self._strategy = strategy

    def _resolve(self, instrument_id):
        return instrument_id if instrument_id is not None else self._strategy._iid()

    def is_net_long(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_net_long(self._resolve(instrument_id))

    def is_net_short(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_net_short(self._resolve(instrument_id))

    def is_flat(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_flat(self._resolve(instrument_id))


def register_custom_block(name: str, entry: dict[str, Any]) -> None:
    """Register a custom block at runtime. `entry` must have keys:
    meta, eval, and optionally on_start, max_lookback, validate.
    """
    required = {"meta", "eval"}
    missing = required - set(entry.keys())
    if missing:
        raise ValueError(f"custom block '{name}' missing keys: {missing}")
    with _REGISTRY_LOCK:
        if name in BLOCK_REGISTRY and BLOCK_REGISTRY[name].get("builtin"):
            raise ValueError(f"cannot override built-in block '{name}'")
        BLOCK_REGISTRY[name] = {
            "meta": entry["meta"],
            "eval": entry["eval"],
            "on_start": entry.get("on_start"),
            "max_lookback": entry.get("max_lookback") or (lambda params: 50),
            "validate": entry.get("validate"),
            "builtin": False,
        }
        _rebuild_catalog()


def unregister_custom_block(name: str) -> None:
    with _REGISTRY_LOCK:
        if name in BLOCK_REGISTRY and not BLOCK_REGISTRY[name].get("builtin"):
            del BLOCK_REGISTRY[name]
            _rebuild_catalog()


def _load_module_from_path(name: str, path: Path):
    """Import a Python file at `path` under module name `name` without sys.path.

    Re-validates the source through ``codegate`` BEFORE executing it. Generation
    time already validates, but a stored ``.py`` could have been hand-edited or
    corrupted on disk; without this check ``exec_module`` would run arbitrary
    code with full privileges at every server startup. ``codegate`` imports only
    ``ast`` so this stays cheap and pulls in no heavy deps.
    """
    import importlib.util
    import math as _math
    import statistics as _statistics

    import indicators as _ind_mod
    from codegate import (
        GeneratedCodeError,
        compile_with_loop_budget,
        safe_builtins,
        validate_generated_code,
    )

    src = path.read_text(encoding="utf-8")
    try:
        validate_generated_code(src)
    except GeneratedCodeError as e:
        raise ImportError(f"custom block {name!r} failed AST re-validation: {e}") from e

    spec = importlib.util.spec_from_file_location(
        f"nautilus_custom_blocks.{name}", str(path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # H8: PARITY with the generation smoke environment — the smoke exec injected
    # math/statistics while the loader did not; blocks using math.* silently no-op'd
    # every candle with a NameError (validated live). `ind` = the NAU parity
    # library (indicators.py, M27/M33).
    module.__dict__["math"] = _math
    module.__dict__["statistics"] = _statistics
    module.__dict__["ind"] = _ind_mod
    # Restrict builtins to the same whitelist the smoke test uses (parity is
    # asserted by this module's own docstring). Setting `__builtins__` here stops
    # CPython from injecting the FULL builtins on exec, so even a codegate miss
    # cannot resolve eval/exec/open/getattr at runtime — defense in depth.
    module.__dict__["__builtins__"] = safe_builtins()
    # M25: the validated source is compiled with a loop-budget AST — `while True`
    # class infinite loops raise a RuntimeError after 5M steps. The budget also covers
    # module-level validate/max_lookback hooks (this was the only path called on the
    # server without a timeout).
    code = compile_with_loop_budget(src, filename=str(path))
    exec(code, module.__dict__)
    return module


def register_custom_from_disk(name: str) -> None:
    """Load a single custom block from the store and register it.

    Raises on load failure or missing `evaluate` function.
    """
    import custom_block_store as cbs

    info = cbs.get_custom(name)
    if info is None:
        raise ValueError(f"no such custom block: {name}")
    module = _load_module_from_path(name, cbs.module_path(name))
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"{name}.py has no callable `evaluate`")

    def _eval_wrapper(strategy, idx, block, closes, _fn=evaluate):
        # Give custom blocks a small mutable state dict scoped by block idx.
        key = f"custom_state_{idx}"
        state = strategy._prev_state.setdefault(key, {})
        # User code may MUTATE the buffer → isolation copy.
        # The window matches the old deque width (buf_cap) exactly: if custom code
        # scans the whole list (e.g. sum(volumes)) it must see the same elements.
        cap = getattr(strategy, "_buf_cap", None)
        closes_view = closes[-cap:] if cap else list(closes)
        # Expose volume + high/low series via indicators — without changing the
        # signature (old blocks ignore them; new ones read volumes/highs/lows).
        # User code may mutate → all are window COPIES, aligned exactly with the
        # old deque width (buf_cap).
        indicators = dict(strategy._indicators.get(idx, {}))

        def _view(series):
            return series[-cap:] if cap else list(series)

        indicators["volumes"] = _view(strategy._volumes)
        indicators["highs"] = _view(getattr(strategy, "_highs", []))
        indicators["lows"] = _view(getattr(strategy, "_lows", []))
        # L25: a view carrying a COPY of params instead of the real SignalBlock —
        # a block that writes `block.params.update(...)` changes its own copy, not
        # the spec's live dict (reported params == the ones that ran).
        # Portfolio is also passed via a minimal facade (no block uses portfolio
        # in block scanning; is_net_long/short/flat passthrough is sufficient).
        block_view = SimpleNamespace(
            params=dict(block.params), role=block.role, type=block.type
        )
        return _fn(state, block_view, closes_view, indicators, _PortfolioView(strategy))

    max_lookback_fn = getattr(module, "max_lookback", None)
    validate_fn = getattr(module, "validate", None)

    # M16: when the declared lookback is below the period-like values in params,
    # the window was silently trimmed (an 'SMA-200' block only saw the last 55
    # candles). Floor the declared value by the param implication and warn.
    _periodish = ("period", "length", "lookback", "window", "slow", "fast")

    def _lookback_with_floor(params, _decl=max_lookback_fn, _name=name):
        try:
            declared = int(_decl(params)) if callable(_decl) else 50
        except Exception:
            declared = 50
        implied = 0
        for k, v in (params or {}).items():
            if any(p in str(k).lower() for p in _periodish):
                try:
                    implied = max(implied, int(float(v)))
                except (TypeError, ValueError):
                    continue
        if implied and declared < implied + 5:
            logging.warning(
                "custom block '%s': declared lookback %d < param implication %d — "
                "using %d (M16)",
                _name,
                declared,
                implied,
                implied + 5,
            )
            return implied + 5
        return declared

    register_custom_block(
        name,
        {
            "meta": info["meta"],
            "eval": _eval_wrapper,
            "on_start": None,
            "max_lookback": _lookback_with_floor,
            "validate": validate_fn if callable(validate_fn) else None,
        },
    )


def _load_custom_blocks() -> None:
    """Load all custom blocks from the on-disk store into BLOCK_REGISTRY.

    Broken modules are skipped with a warning printed to stderr — one bad
    block must not take down the whole catalog.
    """
    try:
        import custom_block_store as cbs
    except Exception as e:  # pragma: no cover
        print(f"[composer] cannot import custom_block_store: {e}")
        return
    for info in cbs.list_custom():
        name = info["name"]
        try:
            register_custom_from_disk(name)
        except Exception as e:
            print(f"[composer] skipping broken custom block '{name}': {e}")


_load_custom_blocks()


# --------------------------------------------------------------------------


@dataclass
class SignalBlock:
    type: BlockType
    role: BlockRole
    params: dict


@dataclass
class ComposedStrategySpec:
    id: str
    name: str
    description: str
    blocks: list[SignalBlock]
    trade_size: float = 0.1
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Optional strategy-level Nautilus features (backward-compatible defaults)
    entry_logic: EntryExitLogic = "OR"
    exit_logic: EntryExitLogic = "OR"
    order_type: OrderTypeOpt = "market"
    limit_offset_bps: float = 0.0
    use_bracket: bool = False
    sl_type: SLType = "percent"
    sl_value: float = 2.0
    tp_type: TPType = "off"
    tp_value: float = 4.0
    atr_period: int = 14
    allow_short: bool = False
    trade_size_mode: TradeSizeMode = "fixed"
    trade_size_percent: float = 5.0
    trade_size_atr_risk: float = 1.0
    trade_size_usdt: float = 1000.0
    # vol_target sizing: size = (vol_target / ewma_vol) * capital / price.
    # capital is a FIXED notional (not live equity), fed from the form's
    # Initial Capital. See _compute_qty and [[vol_targeted_trend]].
    trade_size_vol_target: float = 0.02
    trade_size_vol_span: int = 10
    trade_size_capital: float = 10000.0
    emulate: bool = False
    # Multi-timeframe trend filter (optional)
    trend_filter: bool = False
    trend_interval: str = "60"  # Bybit interval code for the trend bar feed
    trend_ema_period: int = 50
    # Delay fill: execute entry on next bar's open instead of signal bar's close
    # Eliminates same-bar look-ahead bias; default True for more realistic execution
    delay_fill: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocks"] = [asdict(b) for b in self.blocks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ComposedStrategySpec:
        blocks = [SignalBlock(**b) for b in d.get("blocks", [])]
        return cls(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            blocks=blocks,
            trade_size=d.get("trade_size", 0.1),
            created_at=d.get("created_at", datetime.now(UTC).isoformat()),
            entry_logic=d.get("entry_logic", "OR"),
            exit_logic=d.get("exit_logic", "OR"),
            order_type=d.get("order_type", "market"),
            limit_offset_bps=float(d.get("limit_offset_bps", 0.0)),
            use_bracket=bool(d.get("use_bracket", False)),
            sl_type=d.get("sl_type", "percent"),
            sl_value=float(d.get("sl_value", 2.0)),
            tp_type=d.get("tp_type", "off"),
            tp_value=float(d.get("tp_value", 4.0)),
            atr_period=int(d.get("atr_period", 14)),
            allow_short=bool(d.get("allow_short", False)),
            trade_size_mode=d.get("trade_size_mode", "fixed"),
            trade_size_percent=float(d.get("trade_size_percent", 5.0)),
            trade_size_atr_risk=float(d.get("trade_size_atr_risk", 1.0)),
            trade_size_usdt=float(d.get("trade_size_usdt", 1000.0)),
            trade_size_vol_target=float(d.get("trade_size_vol_target", 0.02)),
            trade_size_vol_span=int(d.get("trade_size_vol_span", 10)),
            trade_size_capital=float(d.get("trade_size_capital", 10000.0)),
            emulate=bool(d.get("emulate", False)),
            trend_filter=bool(d.get("trend_filter", False)),
            trend_interval=str(d.get("trend_interval", "60")),
            trend_ema_period=int(d.get("trend_ema_period", 50)),
            delay_fill=bool(d.get("delay_fill", True)),
        )

    def validate(self) -> str | None:
        if not self.blocks:
            return "At least one signal block is required."
        if not any(b.role == "entry" for b in self.blocks):
            return "At least one 'entry' block is required."
        for b in self.blocks:
            reg_entry = BLOCK_REGISTRY.get(b.type)
            if reg_entry is None:
                return f"Unknown block type: {b.type}"
            v = reg_entry.get("validate")
            if v is not None:
                # Carry the L25 isolation into validate too: give the custom validate
                # hook a view carrying a COPY of params instead of the live SignalBlock —
                # so `b.params.update(...)` cannot corrupt the spec's live dict. Built-in
                # validators are read-only, pass with the real block (behavior unchanged).
                vb = (
                    b
                    if reg_entry.get("builtin")
                    else SimpleNamespace(
                        params=dict(b.params), role=b.role, type=b.type
                    )
                )
                err = v(vb)
                if err:
                    return err
        if self.use_bracket:
            if self.sl_value <= 0:
                return "sl_value must be > 0 when bracket is enabled."
            if self.tp_type != "off" and self.tp_value <= 0:
                return "tp_value must be > 0 when TP is not off."
        if self.trade_size_mode == "percent_equity" and self.trade_size_percent <= 0:
            return "trade_size_percent must be > 0."
        if self.trade_size_mode == "atr_target" and self.trade_size_atr_risk <= 0:
            return "trade_size_atr_risk must be > 0."
        if self.trade_size_mode == "fixed_usdt" and self.trade_size_usdt <= 0:
            return "trade_size_usdt must be > 0."
        if self.trade_size_mode == "vol_target":
            if self.trade_size_vol_target <= 0:
                return "trade_size_vol_target must be > 0."
            if self.trade_size_vol_span < 2:
                return "trade_size_vol_span must be >= 2."
            if self.trade_size_capital <= 0:
                return "trade_size_capital must be > 0."
        return None


CATALOG_FILE = Path.home() / ".cache" / "nautilus_web_app" / "strategy_catalog.json"


def _catalog_block_names(spec: ComposedStrategySpec) -> list[str]:
    return [b.type for b in spec.blocks]


def _catalog_is_valid(spec: ComposedStrategySpec, custom_names: set[str]) -> bool:
    # H1338: preserve custom blocks that EXIST on disk (custom_names) but COULD NOT
    # be LOADED into the registry — since spec.validate() looks at the registry it
    # was deleting such a spec as an 'unknown block'. If the block exists on disk,
    # treat the spec as valid (the block can be reloaded; permanent deletion is
    # irreversible data loss).
    on_disk_custom = False
    for block_type in _catalog_block_names(spec):
        if block_type in BLOCK_CATALOG:
            continue
        if block_type in custom_names:
            on_disk_custom = True
            continue
        return False  # neither builtin nor custom-on-disk → genuinely invalid
    if on_disk_custom:
        # validate() fails on a registry miss; preserve a spec with an on-disk
        # custom block using only structural (block-type-independent) checks.
        return not (not spec.blocks or not any(b.role == "entry" for b in spec.blocks))
    return spec.validate() is None


# Perf: memoize the parsed catalog.json by (mtime, size). load_catalog runs on
# hot paths (~18 call sites incl. the agent inner loop) and re-read + re-parsed
# the file every call. The cache stores ONLY the raw list of dicts — fresh
# ComposedStrategySpec objects are rebuilt from it on every call (from_dict is
# read-only over the dicts), so no caller can mutate a shared/cached spec. A
# save (mtime change) invalidates the cache automatically.
_CATALOG_RAW_CACHE: tuple[int, int, list] | None = None


def _read_catalog_raw() -> list | None:
    """Return catalog.json's raw list of dicts, memoized by (mtime, size).

    Returns None for a missing / unparseable / non-list file — callers treat
    that as an empty catalog and NEVER save (M1342), matching the old inline
    behaviour byte-for-byte.
    """
    global _CATALOG_RAW_CACHE
    try:
        st = CATALOG_FILE.stat()
    except OSError:
        return None
    cached = _CATALOG_RAW_CACHE
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        raw = json.loads(CATALOG_FILE.read_text())
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    _CATALOG_RAW_CACHE = (st.st_mtime_ns, st.st_size, raw)
    return raw


def load_catalog() -> list[ComposedStrategySpec]:
    raw = _read_catalog_raw()
    if raw is None:
        return []
    import custom_block_store as cbs

    try:
        custom_names = {rec["name"] for rec in cbs.list_custom()}
    except Exception:
        custom_names = set()

    # M1342: per-record try/except — a SINGLE broken record (unknown field,
    # exception-raising custom validate) could turn the whole catalog into [], then
    # via an RMW-save delete 30 strategies. Skip the broken one, keep the rest.
    catalog: list[ComposedStrategySpec] = []
    n_broken = 0
    for d in raw:
        try:
            catalog.append(ComposedStrategySpec.from_dict(d))
        except Exception:
            n_broken += 1
    filtered = []
    for spec in catalog:
        try:
            if _catalog_is_valid(spec, custom_names):
                filtered.append(spec)
        except Exception:
            n_broken += 1  # the validate hook blew up — drop the spec but count it
    # Only rewrite under lock when VALID records were filtered out (NO broken
    # parse) — saving while a broken record exists would permanently delete them.
    if len(filtered) != len(catalog) and n_broken == 0:
        with _CATALOG_LOCK:
            save_catalog(filtered)
    return filtered


# M14: in-process lock for catalog mutations — the lab/agent/strategy routes did
# lock-free load→append→save (last writer wins, strategy loss).
# RLock: so the locked auto-save inside load_catalog can re-enter on the same
# thread while append_to_catalog holds the lock (deadlock prevention).
_CATALOG_LOCK = threading.RLock()


def save_catalog(specs: list[ComposedStrategySpec]) -> None:
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # M14: atomic write (tmp + os.replace) — a partial write cannot corrupt the file.
    payload = json.dumps([s.to_dict() for s in specs], indent=2)
    tmp = CATALOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    os.replace(tmp, CATALOG_FILE)


def append_to_catalog(spec: ComposedStrategySpec) -> None:
    """load→append→save to the catalog under a SINGLE lock (M14).

    Prevents two concurrent runs from overwriting each other's new strategy.
    Callers (lab/agent/strategy) should use this instead of a lock-free RMW.
    """
    with _CATALOG_LOCK:
        cat = load_catalog()
        cat.append(spec)
        save_catalog(cat)


def mutate_catalog(fn) -> None:
    """Apply an arbitrary mutation on the catalog under lock: fn(list)→list."""
    with _CATALOG_LOCK:
        cat = load_catalog()
        result = fn(cat)
        # fn may intentionally return an EMPTY list ([]) (delete the last strategy) —
        # `or cat` was silently swallowing this and canceling the deletion. Only
        # treat None as a no-op.
        save_catalog(result if result is not None else cat)


def new_spec_id() -> str:
    return uuid.uuid4().hex[:12]


class ComposedStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    spec_json: str
    trade_size: Decimal = Decimal("0.1")
    # Optional secondary bar type for multi-timeframe trend filter.
    # On the ImportableStrategyConfig path msgspec decodes str -> BarType;
    # in direct construction (backtest.py) a BarType object must be passed.
    secondary_bar_type: BarType | None = None


class ComposedStrategy(Strategy):
    """Nautilus Strategy that interprets a list of SignalBlock records."""

    def __init__(self, config: ComposedStrategyConfig) -> None:
        super().__init__(config)
        spec_dict = json.loads(config.spec_json)
        self.spec = ComposedStrategySpec.from_dict(spec_dict)
        self.instrument = None
        max_lookback = self._max_lookback()
        # Price/volume buffers: flat list + amortized compaction. Formerly a deque,
        # taking a FULL COPY of list(deque) every candle (~0.5s measured over 200k
        # candles). Blocks read queue-relative slices (closes[-n:]) — the values are
        # exactly the same; only the copy cost is removed. The custom-block adapter
        # still gives a window copy for isolation.
        self._buf_cap = max_lookback + 5
        # H1940 (effective fix): the adx/stoch_rsi/wave_trend RECURSIVE (Wilder/EMA)
        # blocks require a FIXED window of the last NAU_WINDOW=260 candles via _nau_win.
        # While the buffer swings between buf_cap↔4·buf_cap, if it never reaches 260
        # (when buf_cap is small) _nau_win returns a swinging window; the recursive seed
        # jumps ~4× on each compaction producing a SPURIOUS cross (metrics/score corrupted).
        # If these blocks are present, keep the buffer at least NAU_WINDOW → compaction
        # never drops below 260, _nau_win consistently returns 260 every candle
        # (same as NAU deque(maxlen=260)).
        if any(b.type in _NAU_RECURSIVE_BLOCKS for b in self.spec.blocks):
            self._buf_cap = max(self._buf_cap, NAU_WINDOW)
        # vol_target sizing reads calc_ewma_vol(self._closes, span); keep the
        # close buffer at least span+5 so the EWMA window is consistent
        # regardless of block lookbacks (buffer is periodically trimmed).
        if self.spec.trade_size_mode == "vol_target":
            self._buf_cap = max(self._buf_cap, int(self.spec.trade_size_vol_span) + 5)
        self._closes: list[float] = []
        # Volume series — for the volume_spike block + custom blocks'
        # indicators["volumes"] access (aligned with closes).
        self._volumes: list[float] = []
        # High/low series — so custom blocks can compute real OHLC-based indicators
        # (ADX/ATR/WaveTrend/Stochastic/Donchian) via indicators["highs"]/["lows"].
        # Aligned with closes/volumes, same buffer lifecycle. The Bar already carries
        # full OHLCV (backtest._bars_from_df); in addition to the close, high/low are
        # captured too.
        self._highs: list[float] = []
        self._lows: list[float] = []
        # so _iid() doesn't do isinstance/from_str on every call (4-6 calls per candle)
        _iid_raw = config.instrument_id
        self._iid_obj = (
            InstrumentId.from_str(_iid_raw) if isinstance(_iid_raw, str) else _iid_raw
        )
        # _current_equity fast path: the first successful strategy is used directly
        # by subsequent ones ("portfolio" | "balances" | None=not yet known)
        self._equity_mode: str | None = None
        self._prev_state: dict = {}
        # Per-block Nautilus indicators, keyed by block index.
        self._indicators: dict[int, dict] = {}
        # Shared ATR (for sl/tp=atr or trade_size_mode=atr_target)
        self._atr = None
        # Track blocks whose evaluate() raised, keyed by (idx, error_type) so
        # new error types on the same block are still logged.
        self._eval_failed: set[tuple[int, str]] = set()
        # Pre-partition blocks by role — constant for strategy lifetime.
        self._entry_blocks = [
            (i, b) for i, b in enumerate(self.spec.blocks) if b.role == "entry"
        ]
        self._exit_blocks = [
            (i, b) for i, b in enumerate(self.spec.blocks) if b.role == "exit"
        ]
        # MTM equity snapshots: one value per bar for real drawdown calculation
        self._mtm_equity: list[float] = []
        # L19: bar timestamps (ns) of the MTM snapshots — backtest.py builds a
        # bar-resolution equity_curve_mtm as (ts, eq) pairs.
        self._mtm_ts: list[int] = []
        # delay_fill buffer: pending entry order side when delay_fill=True
        self._pending_entry: str | None = None  # "BUY" | "SELL" | None
        # Decision log: on each entry/exit signal, the firing blocks +
        # indicator values. Orders are stamped with a "dr:<seq>"/"xr:<seq>" tag;
        # after the backtest, a positions↔fills join produces the per-trade
        # entry/exit reason (harvest: same lifecycle as _mtm_equity).
        self._decision_log: list[dict] = []
        self._decision_seq: int = 0
        # in delay_fill, the reason on the signal candle is carried to the next candle's submit
        self._pending_entry_reason: dict | None = None
        # L13: deferred exit reason in delay_fill (entry symmetry).
        self._pending_exit_reason: dict | None = None
        # Multi-timeframe trend filter state
        trend_period = max(self.spec.trend_ema_period, 10)
        self._trend_closes: deque[float] = deque(maxlen=trend_period + 5)
        self._trend_bias: str | None = None  # "bullish" | "bearish" | None

    def _max_lookback(self) -> int:
        best = 30
        for b in self.spec.blocks:
            entry = BLOCK_REGISTRY.get(b.type)
            if entry is None:
                continue
            try:
                lb = int(entry["max_lookback"](b.params))
            except Exception:
                lb = 50
            best = max(best, lb)
        return best

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self.subscribe_bars(self.config.bar_type)

        # Subscribe to secondary (trend) bar feed if trend filter is enabled
        if self.spec.trend_filter and self.config.secondary_bar_type is not None:
            self.subscribe_bars(self.config.secondary_bar_type)

        # Delegate indicator registration to each block's on_start hook.
        for i, b in enumerate(self.spec.blocks):
            entry = BLOCK_REGISTRY.get(b.type)
            if entry is None:
                continue
            hook = entry.get("on_start")
            if hook is not None:
                try:
                    hook(self, i, b)
                except Exception as e:
                    self.log.error(f"on_start hook failed for block {b.type}: {e}")

        # Shared ATR for SL/TP or ATR-target sizing.
        needs_atr = (
            self.spec.sl_type == "atr"
            or self.spec.tp_type == "atr"
            or self.spec.trade_size_mode == "atr_target"
        )
        if needs_atr:
            self._atr = AverageTrueRange(int(self.spec.atr_period))
            self.register_indicator_for_bars(self.config.bar_type, self._atr)

    # ------------------------------------------------------------------
    # Signal evaluation

    def _eval_block(
        self, idx: int, block: SignalBlock, closes: list[float]
    ) -> str | None:
        """Dispatch to the block-type's eval function via BLOCK_REGISTRY.

        Custom-block eval failures are caught and logged once — the block
        yields None for that bar rather than crashing the strategy.
        """
        entry = BLOCK_REGISTRY.get(block.type)
        if entry is None:
            return None
        try:
            return entry["eval"](self, idx, block, closes)
        except Exception as e:
            # Log the first failure. After warmup (enough bars in deque), log
            # every new error type so persistent bugs are visible.
            err_key = (idx, type(e).__name__)
            if err_key not in self._eval_failed:
                self._eval_failed.add(err_key)
                self.log.error(f"block {block.type} eval failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Order helpers

    def _iid(self) -> InstrumentId:
        return self._iid_obj

    def _current_equity(self) -> float:
        """Best-effort account equity. Portfolio.equity() → USDT balance fallback → constant.

        Since it is called every candle (MTM curve), the first successful path is
        saved to ``_equity_mode`` and on subsequent candles the attempt/fallback chain
        is skipped — same API, same value, less Python cost.
        """
        venue = self._iid_obj.venue
        # 1) Portfolio.equity(venue) — v2 native path
        if self._equity_mode in (None, "portfolio"):
            try:
                eq = self.portfolio.equity(venue)
                if eq is not None:
                    self._equity_mode = "portfolio"
                    return float(eq.as_double() if hasattr(eq, "as_double") else eq)
            except Exception:
                pass
        # 2) Scan account balances — USDT/USD preferred, then the first found
        try:
            account = self.portfolio.account(venue)
            if account is not None:
                balances = account.balances()
                # First look for USDT or USD
                for preferred in ("USDT", "USD"):
                    for currency, bal in balances.items():
                        if str(currency) == preferred:
                            for attr in ("total", "free"):
                                v = getattr(bal, attr, None)
                                if v is not None:
                                    try:
                                        _eq = float(
                                            v.as_double()
                                            if hasattr(v, "as_double")
                                            else v
                                        )
                                        # Fast-path lock: on subsequent candles the
                                        # portfolio.equity() attempt is skipped
                                        # (the documented _equity_mode cache;
                                        # 'balances' was NEVER set before).
                                        self._equity_mode = "balances"
                                        return _eq
                                    except Exception:
                                        continue
                # Fallback: first non-None balance (currency unknown, log a warning)
                for bal in balances.values():
                    for attr in ("total", "free"):
                        v = getattr(bal, attr, None)
                        if v is not None:
                            try:
                                result = float(
                                    v.as_double() if hasattr(v, "as_double") else v
                                )
                                self._equity_mode = "balances"  # fast-path lock
                                self.log.warning(
                                    f"_current_equity: USDT/USD not found, using first balance ({result})"
                                )
                                return result
                            except Exception:
                                continue
        except Exception:
            pass
        # L43: fallback from a single source (app_constants.STARTING_CASH) — a
        # copied constant could silently diverge. app_constants is an independent
        # module, no circular import risk.
        from app_constants import STARTING_CASH

        return float(STARTING_CASH)

    def _compute_qty(self, price: float) -> float:
        mode = self.spec.trade_size_mode
        if mode == "fixed":
            return float(self.spec.trade_size)
        if price <= 0:
            return float(self.spec.trade_size)
        if mode == "fixed_usdt":
            # Fixed dollar → quantity: divide the USDT amount by the price
            return max(0.0, float(self.spec.trade_size_usdt) / price)
        if mode == "percent_equity":
            equity = self._current_equity()
            return max(0.0, equity * (self.spec.trade_size_percent / 100.0) / price)
        if mode == "atr_target":
            if self._atr is None or not self._atr.initialized or self._atr.value <= 0:
                return float(self.spec.trade_size)
            equity = self._current_equity()
            risk_usd = equity * (self.spec.trade_size_atr_risk / 100.0)
            return max(0.0, risk_usd / self._atr.value)
        if mode == "vol_target":
            # size = (vol_target / ewma_vol) * capital / price — the vol-targeted
            # trend sizing (formerly a standalone strategy). capital is FIXED
            # (spec.trade_size_capital), not live equity. Warmup (<span+1 closes)
            # → fixed trade_size fallback.
            from indicators import calc_ewma_vol

            vol = calc_ewma_vol(self._closes, int(self.spec.trade_size_vol_span))
            if vol is None or vol <= 0:
                return float(self.spec.trade_size)
            cap = float(self.spec.trade_size_capital)
            size = (float(self.spec.trade_size_vol_target) / vol) * cap / price
            # Upper clamp: never risk more than 95% of capital notional (no leverage
            # blowup). make_qty rounds to size_increment; a rounded-to-zero size is
            # skipped by the qty<=0 guard in _submit_entry.
            return max(0.0, min(size, 0.95 * cap / price))
        return float(self.spec.trade_size)

    def _compute_bracket_prices(
        self, side: OrderSide, price: float
    ) -> tuple[float, float | None]:
        """Return (sl_price, tp_price_or_None) for a bracket entry."""
        atr_ready = self._atr is not None and self._atr.initialized

        sl_dist: float
        if self.spec.sl_type == "atr":
            if not atr_ready:
                return 0.0, None  # caller checks sl_price <= 0 and skips order
            sl_dist = float(self._atr.value) * float(self.spec.sl_value)
        else:
            sl_dist = price * (float(self.spec.sl_value) / 100.0)

        if self.spec.tp_type == "off":
            tp_dist: float | None = None
        elif self.spec.tp_type == "atr":
            if not atr_ready:
                return 0.0, None
            tp_dist = float(self._atr.value) * float(self.spec.tp_value)
        else:
            tp_dist = price * (float(self.spec.tp_value) / 100.0)

        if side == OrderSide.BUY:
            sl_price = price - sl_dist
            tp_price = price + tp_dist if tp_dist is not None else None
        else:  # SELL
            sl_price = price + sl_dist
            tp_price = price - tp_dist if tp_dist is not None else None

        return max(sl_price, 0.01), (
            max(tp_price, 0.01) if tp_price is not None else None
        )

    def _entry_limit_price(self, side: OrderSide, price: float) -> float:
        offset = float(self.spec.limit_offset_bps) / 10_000.0
        if side == OrderSide.BUY:
            return price * (1.0 - offset)
        return price * (1.0 + offset)

    # ------------------------------------------------------------------
    # Decision log (entry/exit reasons)

    def _build_reason(self, kind, side, fires_per, blocks_list, bar, closes) -> dict:
        """label+params+indicator-value snapshot of the firing blocks."""
        fired = []
        for (i, b), f in zip(blocks_list, fires_per):
            if not f:
                continue
            entry = BLOCK_REGISTRY.get(b.type) or {}
            label = (entry.get("meta") or {}).get("label") or b.type
            values = None
            snap = entry.get("snapshot")
            if snap is not None:
                try:
                    values = snap(self, i, b, closes)
                except Exception:
                    values = None
            fired.append(
                {
                    "idx": i,
                    "type": b.type,
                    "label": label,
                    "params": dict(b.params or {}),
                    "values": values,
                }
            )
        return {
            "seq": None,  # _log_decision sets this
            "kind": kind,
            "side": side,
            "bar_ts": int(bar.ts_event // 1_000_000_000),
            "submit_ts": None,
            "logic": self.spec.entry_logic if kind == "entry" else self.spec.exit_logic,
            "blocks": fired,
            "trend_bias": self._trend_bias,
        }

    def _log_decision(self, reason: dict, submit_ts: int) -> int:
        """Write the decision to the log, return the seq to use in the order tag."""
        self._decision_seq += 1
        reason["seq"] = self._decision_seq
        reason["submit_ts"] = submit_ts
        self._decision_log.append(reason)
        return self._decision_seq

    def _can_submit_entry(self, side: OrderSide, bar: Bar) -> bool:
        """M17: pre-check BEFORE SENDING an order.

        On the flip path the 'close first, then enter' order was leaving the strategy
        unintentionally FLAT if the entry was going to fail anyway (qty→0, make_qty
        error, ATR not ready for the bracket SL) by closing the old position. A flip
        only begins with a close if this check is True.
        """
        price = float(bar.close)
        qty_raw = self._compute_qty(price)
        if qty_raw <= 0:
            return False
        try:
            qty = self.instrument.make_qty(qty_raw)
        except Exception:
            return False
        if float(qty) <= 0:
            return False
        if self.spec.use_bracket:
            sl_price, _tp = self._compute_bracket_prices(side, price)
            if sl_price <= 0:
                return False
        return True

    def _cancel_working(self) -> None:
        """H1999/M1840: cancel the instrument's open (working) orders.

        On exit and flip only close_all_positions was being called; pending
        SL/TP protective orders (bracket/SL-only) and unfilled GTC limit entry
        orders were not canceled. Result: (a) a stale SL/TP could close the next
        position at the old level; (b) unfilled limit entries could accumulate and
        open double/triple positions. Since Nautilus manage_contingent_orders is
        off by default, we do this manually.
        """
        try:
            self.cancel_all_orders(self._iid())
        except Exception as e:
            self.log.error(f"_cancel_working failed: {e}")

    def _rollback_decision(self, seq: int | None) -> None:
        """L12: order could not be sent — roll back the decision just written.

        The decision log is written BEFORE the submit; if the order does not go out
        the 'dr:<seq>' tag is on no order and the log would bloat with ghost 'entry'
        records. The strategy is single-threaded — the pop is safe.
        """
        if seq is None:
            return
        if self._decision_log and self._decision_log[-1].get("seq") == seq:
            self._decision_log.pop()
            self._decision_seq -= 1

    def _submit_entry(
        self, side: OrderSide, bar: Bar, reason_seq: int | None = None
    ) -> bool:
        """Submits the entry order; True IF SENT (L12: bool contract —
        on a False return the caller rolls back the decision)."""
        entry_tags = [f"dr:{reason_seq}"] if reason_seq is not None else None
        price = float(bar.close)
        qty_raw = self._compute_qty(price)
        if qty_raw <= 0:
            return False
        try:
            qty = self.instrument.make_qty(qty_raw)
        except Exception:
            return False
        # Don't send an order with a quantity that drops to zero after rounding
        if float(qty) <= 0:
            return False

        if self.spec.use_bracket:
            sl_price, tp_price = self._compute_bracket_prices(side, price)
            if sl_price <= 0:
                # ATR not ready yet — SL cannot be computed for the bracket order
                return False
            entry_price_obj = None
            entry_order_type = OrderType.MARKET
            if self.spec.order_type == "limit":
                entry_order_type = OrderType.LIMIT
                entry_price_obj = self.instrument.make_price(
                    self._entry_limit_price(side, price)
                )

            if tp_price is None:
                # tp_type == 'off': Nautilus bracket() always sets up a TP LIMIT
                # order and does NOT ACCEPT price=None (TypeError — a live bug seen
                # 8× in the agent logs). Fall back to entry + SL-only: send the entry
                # order normally, add the SL as a reduce-only STOP_MARKET.
                if entry_order_type == OrderType.LIMIT:
                    entry = self.order_factory.limit(
                        instrument_id=self._iid(),
                        order_side=side,
                        quantity=qty,
                        price=entry_price_obj,
                        time_in_force=TimeInForce.GTC,
                        tags=entry_tags,
                    )
                else:
                    entry = self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=side,
                        quantity=qty,
                        tags=entry_tags,
                    )
                self.submit_order(entry)
                sl_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
                sl_order = self.order_factory.stop_market(
                    instrument_id=self._iid(),
                    order_side=sl_side,
                    quantity=qty,
                    trigger_price=self.instrument.make_price(sl_price),
                    time_in_force=TimeInForce.GTC,
                    reduce_only=True,  # does NOT open a reverse position if no position exists
                    tags=["sl"],
                )
                self.submit_order(sl_order)
                return True

            order_list = self.order_factory.bracket(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                entry_order_type=entry_order_type,
                entry_price=entry_price_obj,
                sl_trigger_price=self.instrument.make_price(sl_price),
                tp_price=self.instrument.make_price(tp_price),
                time_in_force=TimeInForce.GTC,
                emulation_trigger=TriggerType.LAST_PRICE
                if self.spec.emulate
                else TriggerType.NO_TRIGGER,
                entry_tags=entry_tags,
                sl_tags=["sl"],
                tp_tags=["tp"],
            )
            self.submit_order_list(order_list)
            return True

        if self.spec.order_type == "market":
            order = self.order_factory.market(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                tags=entry_tags,
            )
        else:
            order = self.order_factory.limit(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                price=self.instrument.make_price(self._entry_limit_price(side, price)),
                time_in_force=TimeInForce.GTC,
                tags=entry_tags,
            )
        self.submit_order(order)
        return True

    # ------------------------------------------------------------------
    # Bar handler

    @staticmethod
    def _ema(closes: deque[float], period: int) -> float | None:
        """Simple EMA over the last `period` values. Returns None if not enough data."""
        vals = list(closes)[-period:]
        if len(vals) < period:
            return None
        k = 2.0 / (period + 1)
        ema = vals[0]
        for v in vals[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def on_bar(self, bar: Bar) -> None:
        # Multi-timeframe routing: secondary feed updates trend bias only
        if (
            self.spec.trend_filter
            and self.config.secondary_bar_type is not None
            and bar.bar_type == self.config.secondary_bar_type
        ):
            self._trend_closes.append(float(bar.close))
            ema = self._ema(self._trend_closes, self.spec.trend_ema_period)
            if ema is not None:
                self._trend_bias = "bullish" if float(bar.close) > ema else "bearish"
            return

        # Snapshot MTM equity every bar for real drawdown calculation
        try:
            eq = self._current_equity()
            if eq > 0:
                self._mtm_equity.append(eq)
                self._mtm_ts.append(int(bar.ts_event))  # L19: aligned time
        except Exception:
            pass

        # L13: delay_fill — the deferred EXIT is processed BEFORE the deferred entry
        # (if both an exit and an entry are queued on the same candle, order: exit → entry;
        # flip semantics stay the same as the close_all in the pending_entry branch).
        if self.spec.delay_fill and self._pending_exit_reason is not None:
            reason = self._pending_exit_reason
            self._pending_exit_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            if self.portfolio.is_net_long(self._iid()) or self.portfolio.is_net_short(
                self._iid()
            ):
                seq = self._log_decision(reason, submit_ts)
                self._cancel_working()  # H1999: stale SL/TP + unfilled limit
                self.close_all_positions(self._iid(), tags=[f"xr:{seq}"])

        # delay_fill: execute deferred entry from previous bar
        if self.spec.delay_fill and self._pending_entry is not None:
            side = self._pending_entry
            self._pending_entry = None
            # The reason accumulated on the signal candle — only reaches the log if
            # the order is ACTUALLY sent (if a position is already open the decision
            # silently drops).
            reason = self._pending_entry_reason
            self._pending_entry_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            is_long = self.portfolio.is_net_long(self._iid())
            is_short = self.portfolio.is_net_short(self._iid())
            if side == "BUY" and not is_long:
                # M17: flip pre-check (see the synchronous path) — if the entry
                # cannot go out, the old position is not closed.
                if is_short and not self._can_submit_entry(OrderSide.BUY, bar):
                    pass
                else:
                    # H1999 (flip: stale SL/TP) + #4 (flat: unfilled GTC limit
                    # entry). Unconditional — always clear open orders before sending
                    # a new entry so limit entries don't accumulate and fill together.
                    self._cancel_working()
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.BUY, bar, reason_seq=seq):
                        self._rollback_decision(seq)
            elif side == "SELL" and not is_short:
                if is_long and not self._can_submit_entry(OrderSide.SELL, bar):
                    pass
                else:
                    self._cancel_working()  # H1999 (flip) + #4 (flat limit accumulation)
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                        self._rollback_decision(seq)

        # Append to the buffers; amortized compaction (trim the queue at 4×cap) — zero-copy
        # sharing instead of a full copy every candle (the old list(deque)).
        # The four series (close/volume/high/low) are trimmed IN SYNC → stay aligned.
        self._closes.append(float(bar.close))
        self._volumes.append(float(bar.volume))
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        if len(self._closes) > 4 * self._buf_cap:
            _cut = len(self._closes) - self._buf_cap
            del self._closes[:_cut]
            del self._volumes[:_cut]
            del self._highs[:_cut]
            del self._lows[:_cut]
        closes = self._closes

        long_fires_per: list[bool] = []
        short_fires_per: list[bool] = []
        for i, b in self._entry_blocks:
            r = self._eval_block(i, b, closes)
            long_fires_per.append(r == "long")
            short_fires_per.append(r == "short")

        exit_fires_per: list[bool] = []
        for i, b in self._exit_blocks:
            r = self._eval_block(i, b, closes)
            exit_fires_per.append(bool(r))

        if self.spec.entry_logic == "AND":
            long_fires = bool(self._entry_blocks) and all(long_fires_per)
            short_fires = bool(self._entry_blocks) and all(short_fires_per)
        else:
            long_fires = any(long_fires_per)
            short_fires = any(short_fires_per)

        if not self.spec.allow_short:
            short_fires = False

        # Multi-timeframe trend filter: suppress entries against the trend
        if self.spec.trend_filter and self._trend_bias is not None:
            if self._trend_bias == "bearish":
                long_fires = False  # no longs in downtrend
            elif self._trend_bias == "bullish":
                # Suppress shorts in an uptrend. This branch was formerly guarded
                # by `and not allow_short` — but the ONLY case where shorts fire is
                # allow_short=True; the guard skipped exactly that case and let
                # counter-trend shorts through unfiltered (the filter did nothing).
                # When allow_short=False, short_fires is already False so this line
                # is harmless in that case.
                short_fires = False  # no shorts in uptrend

        if self.spec.exit_logic == "AND":
            exit_fires = bool(self._exit_blocks) and all(exit_fires_per)
        else:
            exit_fires = any(exit_fires_per)

        is_long = self.portfolio.is_net_long(self._iid())
        is_short = self.portfolio.is_net_short(self._iid())

        if exit_fires and (is_long or is_short):
            reason = self._build_reason(
                "exit", None, exit_fires_per, self._exit_blocks, bar, closes
            )
            if self.spec.delay_fill:
                # L13: delay_fill is now applied to EXITS too — formerly entries
                # were delayed by one candle while exits were processed on the
                # signal candle, so exits were systematically one candle early
                # (asymmetric/optimistic timing). A deliberate behavior change.
                self._pending_exit_reason = reason
            else:
                seq = self._log_decision(reason, reason["bar_ts"])
                self._cancel_working()  # H1999: stale SL/TP + unfilled limit
                self.close_all_positions(self._iid(), tags=[f"xr:{seq}"])
            return

        if long_fires and not is_long:
            reason = self._build_reason(
                "entry", "BUY", long_fires_per, self._entry_blocks, bar, closes
            )
            if self.spec.delay_fill:
                self._pending_entry = "BUY"  # execute next bar
                self._pending_entry_reason = reason
            else:
                # M17: on a flip, entry pre-check FIRST — if the new order
                # cannot go out anyway, don't close the old position (avoid staying flat unintentionally).
                if is_short:
                    if not self._can_submit_entry(OrderSide.BUY, bar):
                        return
                    self._cancel_working()  # H1999
                    self.close_all_positions(self._iid(), tags=["flip"])
                else:
                    self._cancel_working()  # #4: prevent unfilled GTC limit entry accumulation when flat
                seq = self._log_decision(reason, reason["bar_ts"])
                if not self._submit_entry(OrderSide.BUY, bar, reason_seq=seq):
                    self._rollback_decision(seq)
        elif short_fires and not is_short:
            reason = self._build_reason(
                "entry", "SELL", short_fires_per, self._entry_blocks, bar, closes
            )
            if self.spec.delay_fill:
                self._pending_entry = "SELL"  # execute next bar
                self._pending_entry_reason = reason
            else:
                if is_long:
                    if not self._can_submit_entry(OrderSide.SELL, bar):
                        return
                    self._cancel_working()  # H1999
                    self.close_all_positions(self._iid(), tags=["flip"])
                else:
                    self._cancel_working()  # #4: prevent unfilled GTC limit entry accumulation when flat
                seq = self._log_decision(reason, reason["bar_ts"])
                if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                    self._rollback_decision(seq)

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid(), tags=["eob"])
