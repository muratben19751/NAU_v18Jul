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
Bkz: [[strategy_and_actor]], [[order_flow_pipeline]]

Blocks emit signals; the composer wires them into a Nautilus `Strategy`. Emir gönderimi tam olarak [[order_flow_pipeline]]'a giriyor (`submit_order` → OrderEmulator/ExecutionAlgorithms/RiskEngine/Adapter).
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
TradeSizeMode = Literal["fixed", "fixed_usdt", "percent_equity", "atr_target"]


# --------------------------------------------------------------------------
# Built-in block metadata (was BLOCK_CATALOG; now part of BLOCK_REGISTRY meta)

_BUILTIN_META: dict[str, dict] = {
    "ma_cross": {
        "label": "MA Kesişimi",
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
            "Hızlı ve yavaş hareketli ortalama kesişimi. `up` = hızlı yavaşı yukarı "
            "keserse tetiklenir. `on_bar`'da her yeni kapanışla yeniden hesaplanır."
        ),
    },
    "rsi_threshold": {
        "label": "RSI Eşik",
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
            "RSI belirtilen eşiği yukarı/aşağı geçtiğinde tetiklenir. "
            "`below` = eşiğin altına inince (aşırı satım sinyali)."
        ),
    },
    "price_breakout": {
        "label": "Fiyat Kırılımı",
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
            "Son N barın en yüksek/düşük kapanışı kırıldığında tetiklenir. "
            "Donchian mantığı."
        ),
    },
    "momentum": {
        "label": "Momentum İşareti",
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
            "Son N bar getirisinin işareti. `positive` = son N barda net yükseliş."
        ),
    },
    "volume_spike": {
        "label": "Hacim Patlaması",
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
            "Son barın hacmi, önceki N barın ortalama hacminin `mult` katını "
            "aşarsa (`above`) veya altına inerse (`below` — hacim kuruması) "
            "tetiklenir. Hacim-teyitli giriş/çıkışlar için diğer bloklarla "
            "AND mantığında birleştirilebilir."
        ),
    },
    "ema_cross": {
        "label": "EMA Kesişimi (Nautilus)",
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
            "Nautilus native `ExponentialMovingAverage` indicator kullanır. "
            "EMA yumuşak MA'dır: son barlara daha fazla ağırlık verir. `up` = fast EMA "
            "slow EMA'yı yukarı keser (short için `down`)."
        ),
    },
    "bollinger_break": {
        "label": "Bollinger Kırılımı (Nautilus)",
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
            "Nautilus `BollingerBands(period, k)` indicator. Fiyat üst banda değerse "
            "`upper` (breakout/momentum girişi), alt banda değerse `lower` (mean reversion). "
            "mode=legacy her iki bantta LONG (eski davranış); breakout: upper→long, "
            "lower→short; revert: upper→short, lower→long (short'lar allow_short ister)."
        ),
    },
    "macd_cross": {
        "label": "EMA Fark Kesişimi (MACD benzeri)",
        "params": {
            "fast": {"type": "int", "min": 2, "max": 60, "default": 12},
            "slow": {"type": "int", "min": 5, "max": 200, "default": 26},
            "direction": {"type": "enum", "options": ["up", "down"], "default": "up"},
        },
        "wiki_refs": ["wiki/entities/strategy_and_actor.md"],
        "help": (
            "İki Nautilus `ExponentialMovingAverage` indicator'ının farkı sıfırı "
            "keçtiğinde tetiklenir. `up` = fast EMA - slow EMA sıfırı yukarı keser "
            "(momentum girişi). Sinyal çizgisi içermez."
        ),
    },
    "atr_stop": {
        "label": "ATR Stop (yalnız exit)",
        "params": {
            "period": {"type": "int", "min": 5, "max": 100, "default": 14},
            "mult": {"type": "float", "min": 0.5, "max": 10.0, "default": 3.0},
        },
        "wiki_refs": ["wiki/entities/execution_engine.md"],
        "help": (
            "Nautilus `AverageTrueRange` indicator kullanır. Fiyat son close'dan "
            "ATR × mult kadar aşağı çekildiğinde exit tetiklenir. Yalnız exit rolünde kullanılır."
        ),
    },
    # ── M27: NAU parite kütüphanesi (indicators.py) üstüne kurulu builtin'ler ──
    "adx_threshold": {
        "label": "ADX Trend Gücü (NAU)",
        "params": {
            "period": {"type": "int", "min": 7, "max": 50, "default": 14},
            "threshold": {"type": "float", "min": 10.0, "max": 50.0, "default": 25.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_adx (Wilder) — NAU_ev ile birebir parite. Entry: "
            "ADX ≥ threshold iken +DI>−DI → long, −DI>+DI → short. Exit: ADX "
            "threshold altına düşerse (trend zayıfladı) çıkış."
        ),
    },
    "stoch_rsi_cross": {
        "label": "StochRSI K/D Kesişimi (NAU)",
        "params": {
            "rsi_period": {"type": "int", "min": 5, "max": 50, "default": 14},
            "stoch_period": {"type": "int", "min": 5, "max": 50, "default": 14},
            "oversold": {"type": "float", "min": 5.0, "max": 40.0, "default": 20.0},
            "overbought": {"type": "float", "min": 60.0, "max": 95.0, "default": 80.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_stoch_rsi (K=3/D=3 SMA yumuşatma). Entry: K, D'yi "
            "aşırı-satım bölgesinde yukarı keserse long; aşırı-alımda aşağı "
            "keserse short. Exit: ters kesişim."
        ),
    },
    "wave_trend_cross": {
        "label": "WaveTrend Kesişimi (NAU)",
        "params": {
            "channel_len": {"type": "int", "min": 5, "max": 30, "default": 10},
            "avg_len": {"type": "int", "min": 10, "max": 50, "default": 21},
            "os_level": {"type": "float", "min": -80.0, "max": 0.0, "default": -30.0},
            "ob_level": {"type": "float", "min": 0.0, "max": 80.0, "default": 30.0},
        },
        "wiki_refs": [],
        "help": (
            "indicators.calc_wave_trend (LazyBear WT1/WT2). Entry: WT1, WT2'yi "
            "os_level altında yukarı keserse long; ob_level üstünde aşağı "
            "keserse short. Exit: ters kesişim."
        ),
    },
    "donchian_channel": {
        "label": "Donchian Kanalı",
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
            "Gerçek high/low Donchian kanalı (price_breakout yalnız close "
            "kırılımıydı). breakout: close, önceki N barın en yükseğini aşarsa "
            "long / en düşüğünü kırarsa short. revert: tersi. Exit: close kanal "
            "ortasını ters yönde keserse."
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
    # H6: Nautilus RelativeStrengthIndex.value ∈ [0,1) üretir; threshold ise
    # 0-100 ölçeğinde (varsayılan 30). Ölçek uyumsuzluğu bloğu ÖLÜ KOD yapıyordu
    # (prev>=30 asla olmaz). rsi.value'yu 0-100'e çek (NAU calc_rsi konvansiyonu).
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
    """Hacim patlaması/kuruması: son hacim vs önceki N barın ortalaması."""
    n = int(block.params.get("period", 20))
    mult = float(block.params.get("mult", 2.0))
    direction = block.params.get("direction", "above")
    vols = strategy._volumes  # düz list buffer — kopya gereksiz (salt okunur)
    if n < 1 or len(vols) < n + 1:
        return None
    avg = sum(vols[-n - 1 : -1]) / n
    if avg <= 0:
        return None
    ratio = vols[-1] / avg
    fired = ratio >= mult if direction == "above" else ratio <= (1.0 / mult)
    # Kenar tetikleme: koşul sürerken her bar yeniden ateşlemesin
    prev_fired = strategy._prev_state.get(idx, False)
    strategy._prev_state[idx] = fired
    if not fired or prev_fired:
        return None
    if block.role == "exit":
        return "exit"
    # Hacim yön bilgisi taşımaz — spike anında bar yönüne göre long/short
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
    # L14: mode parametresi — 'legacy' (varsayılan) mevcut davranışı AYNEN
    # korur (her iki bant → long; katalogdaki eski spec'ler kırılmaz).
    # 'breakout': upper→long, lower→short. 'revert' (ortalamaya dönüş):
    # upper→short, lower→long. Short sinyalleri allow_short kapısına tabi.
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


# Snapshot (karar anındaki indikatör değerleri) per built-in block. Sinyal
# ateşlediğinde çağrılır; dönen dict trade'in "giriş/çıkış sebebi" satırında
# gösterilir. Hata halinde None (çağıran try/except sarar). Custom bloklarda
# hook yoktur → yalnız label+params gösterilir.


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
    return {"rsi": round(rsi.value * 100.0, 2)}  # H6: 0-100 ölçek (eşiklerle aynı)


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
        return f"{block.type}: slow > fast olmalı."
    return None


def _validate_atr_stop(block):
    if block.role != "exit":
        return "atr_stop bloğu yalnızca exit rolünde kullanılabilir."
    return None


# --------------------------------------------------------------------------
# BLOCK_REGISTRY — the single source of truth for block behavior.
# Each entry: { meta, eval, on_start, max_lookback, validate, builtin }
# Custom blocks are added by `_load_custom_blocks()` at import time and via
# `register_custom_block()` at runtime.

# ── M27: NAU parite kütüphanesi (indicators.py) üstüne kurulu builtin'ler ──
# Nautilus indicator nesnesi yerine saf-python calc_* çağrıları: NAU_ev ile
# sayısal parite testli, on_start hook'u gerektirmez, sandbox'a girmez.

# H1940: bu bloklar özyinelemeli calc_* (Wilder ADX, StochRSI, EMA-zincirli
# WaveTrend) kullanır; değer SERİ UZUNLUĞUNA bağlıdır. on_bar buffer'ı 4×cap →
# cap kırptığında pencere tek barda ~4× küçülüp indikatör değeri SIÇRIYOR ve
# sahte kesişim/eşik sinyali üretiyordu. Çözüm: calc_*'a HER ZAMAN sabit
# uzunlukta pencere ver (son NAU_WINDOW bar) — kompaksiyondan bağımsız, kararlı
# değer. NAU generic_strategy.py deque(maxlen=260) ile aynı sabit-pencere
# yaklaşımı.
NAU_WINDOW = 260

# _nau_win'in son NAU_WINDOW barı tutarlı döndürebilmesi için buffer'ı en az
# NAU_WINDOW tutması gereken RECURSIVE (Wilder/EMA tohum) bloklar. donchian
# non-recursive (max/min) olduğundan salınan pencereden etkilenmez — dahil değil.
_NAU_RECURSIVE_BLOCKS = {"adx_threshold", "stoch_rsi_cross", "wave_trend_cross"}


def _nau_win(series):
    """calc_* için kararlı (sabit uzunluklu) pencere — kompaksiyon sıçramasını
    (H1940) kaldırır. Seri NAU_WINDOW'dan kısaysa olduğu gibi döner."""
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
    # Warmup guard: calc_stoch_rsi yeterli bar birikene kadar (50,50) sentinel
    # döner (None değil). Sentinel'i prev olarak TOHUMLAMA — yoksa ilk gerçek
    # (k,d) pk==pd_==50 okuyup sahte kesişim üretir. Gerçek k==d==50 zaten sinyal
    # ateşleyemez (k>d ve k<d ikisi de yanlış), o yüzden atlamak güvenli
    # (wave_trend None-guard'ı ile aynı desen).
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
    upper = max(highs[-period - 1 : -1])  # mevcut bar HARİÇ önceki N bar
    lower = min(lows[-period - 1 : -1])
    last = closes[-1]
    if block.role == "exit":
        # Kanal ortası ters-yön kesişimi → exit.
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
        "üst": round(max(highs[-period - 1 : -1]), 4),
        "alt": round(min(lows[-period - 1 : -1]), 4),
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
    # Yeni dict'i önce kur, sonra tek clear+update ile uygula — kilitsiz
    # okuyucunun boş/yarım katalog (veya 'changed size during iteration')
    # görme penceresini daraltır. Dict KİMLİĞİ korunur (template ref'leri).
    new = {k: entry["meta"] for k, entry in BLOCK_REGISTRY.items()}
    BLOCK_CATALOG.clear()
    BLOCK_CATALOG.update(new)


_REGISTRY_LOCK = threading.Lock()
_rebuild_catalog()


class _PortfolioView:
    """L25: custom bloklara geçen minimal Portfolio görünümü.

    Blok taramasında hiçbir mevcut blok portfolio kullanmıyor; whitelist'teki
    üç sorgu passthrough olarak açılır (argümansız çağrıda stratejinin kendi
    enstrümanı varsayılır). Gerçek Portfolio'nun mutasyon yüzeyi bloklara
    kapalı kalır.
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
    # H8: üretim smoke ortamıyla PARİTE — smoke exec'i math/statistics enjekte
    # ederken yükleyici etmiyordu; math.* kullanan bloklar her barda NameError
    # ile sessiz no-op oluyordu (canlı doğrulandı). `ind` = NAU parite
    # kütüphanesi (indicators.py, M27/M33).
    module.__dict__["math"] = _math
    module.__dict__["statistics"] = _statistics
    module.__dict__["ind"] = _ind_mod
    # Restrict builtins to the same whitelist the smoke test uses (parity is
    # asserted by this module's own docstring). Setting `__builtins__` here stops
    # CPython from injecting the FULL builtins on exec, so even a codegate miss
    # cannot resolve eval/exec/open/getattr at runtime — defense in depth.
    module.__dict__["__builtins__"] = safe_builtins()
    # M25: doğrulanmış kaynak döngü-bütçeli AST ile derlenir — `while True`
    # sınıfı sonsuz döngüler 5M adımda RuntimeError üretir. Bütçe module-level
    # validate/max_lookback hook'larını da kapsar (sunucuda timeout'suz
    # çağrılan tek yol buydu).
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
        # Kullanıcı kodu buffer'ı MUTASYONA uğratabilir → izolasyon kopyası.
        # Pencere eski deque genişliğiyle (buf_cap) birebir: custom kod tüm
        # listeyi tararsa (örn. sum(volumes)) aynı elemanları görmeli.
        cap = getattr(strategy, "_buf_cap", None)
        closes_view = closes[-cap:] if cap else list(closes)
        # Hacim + high/low serilerini indicators üzerinden aç — imza
        # değişmeden (eski bloklar görmezden gelir; yeniler volumes/highs/lows
        # okur). Kullanıcı kodu mutasyon yapabilir → hepsi pencere KOPYASI,
        # eski deque genişliğiyle (buf_cap) birebir hizalı.
        indicators = dict(strategy._indicators.get(idx, {}))

        def _view(series):
            return series[-cap:] if cap else list(series)

        indicators["volumes"] = _view(strategy._volumes)
        indicators["highs"] = _view(getattr(strategy, "_highs", []))
        indicators["lows"] = _view(getattr(strategy, "_lows", []))
        # L25: gerçek SignalBlock yerine params KOPYASI taşıyan görünüm —
        # `block.params.update(...)` yazan bir blok spec'in canlı dict'ini
        # değil kendi kopyasını değiştirir (raporlanan parametre == koşulan).
        # Portfolio da minimal facade'la geçer (blok taramasında hiçbir blok
        # portfolio kullanmıyor; is_net_long/short/flat passthrough yeterli).
        block_view = SimpleNamespace(
            params=dict(block.params), role=block.role, type=block.type
        )
        return _fn(state, block_view, closes_view, indicators, _PortfolioView(strategy))

    max_lookback_fn = getattr(module, "max_lookback", None)
    validate_fn = getattr(module, "validate", None)

    # M16: deklare lookback, params'taki period-benzeri değerlerin altındaysa
    # pencere sessizce kırpılıyordu ('SMA-200' bloğu yalnız son 55 barı
    # görüyordu). Deklareyi param imasıyla tabanla ve uyar.
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
                "custom blok '%s': deklare lookback %d < param iması %d — "
                "%d kullanılıyor (M16)",
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
            emulate=bool(d.get("emulate", False)),
            trend_filter=bool(d.get("trend_filter", False)),
            trend_interval=str(d.get("trend_interval", "60")),
            trend_ema_period=int(d.get("trend_ema_period", 50)),
            delay_fill=bool(d.get("delay_fill", True)),
        )

    def validate(self) -> str | None:
        if not self.blocks:
            return "En az bir sinyal bloğu gerekli."
        if not any(b.role == "entry" for b in self.blocks):
            return "En az bir 'entry' bloğu gerekli."
        for b in self.blocks:
            reg_entry = BLOCK_REGISTRY.get(b.type)
            if reg_entry is None:
                return f"Bilinmeyen blok tipi: {b.type}"
            v = reg_entry.get("validate")
            if v is not None:
                # L25 izolasyonunu validate'e de taşı: custom validate hook'una
                # canlı SignalBlock yerine params KOPYASI taşıyan görünüm ver —
                # `b.params.update(...)` spec'in canlı dict'ini bozamasın. Built-in
                # validator'lar salt-okur, gerçek block ile geçer (davranış aynı).
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
                return "Bracket açıkken sl_value > 0 olmalı."
            if self.tp_type != "off" and self.tp_value <= 0:
                return "TP kapalı değilse tp_value > 0 olmalı."
        if self.trade_size_mode == "percent_equity" and self.trade_size_percent <= 0:
            return "trade_size_percent > 0 olmalı."
        if self.trade_size_mode == "atr_target" and self.trade_size_atr_risk <= 0:
            return "trade_size_atr_risk > 0 olmalı."
        if self.trade_size_mode == "fixed_usdt" and self.trade_size_usdt <= 0:
            return "trade_size_usdt > 0 olmalı."
        return None


CATALOG_FILE = Path.home() / ".cache" / "nautilus_web_app" / "strategy_catalog.json"


def _catalog_block_names(spec: ComposedStrategySpec) -> list[str]:
    return [b.type for b in spec.blocks]


def _catalog_is_valid(spec: ComposedStrategySpec, custom_names: set[str]) -> bool:
    # H1338: diskte VAR olan (custom_names) ama registry'ye YÜKLENEMEMİŞ custom
    # blokları koru — spec.validate() registry'ye baktığından böyle bir spec'i
    # 'bilinmeyen blok' diye siliyordu. Blok diskte varsa spec'i geçerli say
    # (blok yeniden yüklenebilir; kalıcı silmek geri dönüşsüz veri kaybı).
    on_disk_custom = False
    for block_type in _catalog_block_names(spec):
        if block_type in BLOCK_CATALOG:
            continue
        if block_type in custom_names:
            on_disk_custom = True
            continue
        return False  # ne builtin ne diskte custom → gerçekten geçersiz
    if on_disk_custom:
        # validate() registry-miss'te başarısız olur; diskteki custom bloklu
        # spec'i yalnız yapısal (blok-tipi bağımsız) kontrollerle koru.
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

    # M1342: kayıt-başına try/except — TEK bozuk kayıt (bilinmeyen alan,
    # exception fırlatan custom validate) tüm kataloğu [] yapıp, ardından bir
    # RMW-save ile 30 stratejiyi silebiliyordu. Bozuğu atla, gerisini koru.
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
            n_broken += 1  # validate hook'u patladı — spec'i düşür ama sayaçla
    # Yalnızca GEÇERLİ kayıtlar elendiğinde (bozuk parse YOK) kilitli yeniden
    # yaz — bozuk kayıt varken save etmek onları kalıcı silerdi.
    if len(filtered) != len(catalog) and n_broken == 0:
        with _CATALOG_LOCK:
            save_catalog(filtered)
    return filtered


# M14: katalog mutasyonları için süreç-içi kilit — lab/agent/strategy rotaları
# kilitsiz load→append→save yapıyordu (son yazan kazanır, strateji kaybı).
# RLock: append_to_catalog kilidi tutarken load_catalog'un içindeki kilitli
# auto-save aynı thread'de yeniden girebilsin (deadlock önlemi).
_CATALOG_LOCK = threading.RLock()


def save_catalog(specs: list[ComposedStrategySpec]) -> None:
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # M14: atomik yazım (tmp + os.replace) — yarım yazım dosyayı bozamaz.
    payload = json.dumps([s.to_dict() for s in specs], indent=2)
    tmp = CATALOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    os.replace(tmp, CATALOG_FILE)


def append_to_catalog(spec: ComposedStrategySpec) -> None:
    """Kataloğa TEK kilit altında load→append→save (M14).

    Eşzamanlı iki koşunun birbirinin yeni stratejisini ezmesini önler.
    Çağıranlar (lab/agent/strategy) kilitsiz RMW yerine bunu kullanmalı.
    """
    with _CATALOG_LOCK:
        cat = load_catalog()
        cat.append(spec)
        save_catalog(cat)


def mutate_catalog(fn) -> None:
    """Katalog üzerinde keyfi mutasyonu kilit altında uygula: fn(list)→list."""
    with _CATALOG_LOCK:
        cat = load_catalog()
        result = fn(cat)
        # fn kasıtlı BOŞ liste ([]) dönebilir (son stratejiyi sil) — `or cat`
        # bunu sessizce yutup silmeyi iptal ediyordu. Yalnız None'ı no-op say.
        save_catalog(result if result is not None else cat)


def new_spec_id() -> str:
    return uuid.uuid4().hex[:12]


class ComposedStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    spec_json: str
    trade_size: Decimal = Decimal("0.1")
    # Optional secondary bar type for multi-timeframe trend filter.
    # ImportableStrategyConfig yolunda msgspec str -> BarType decode eder;
    # doğrudan yapımda (backtest.py) BarType nesnesi geçirilmelidir.
    secondary_bar_type: BarType | None = None


class ComposedStrategy(Strategy):
    """Nautilus Strategy that interprets a list of SignalBlock records."""

    def __init__(self, config: ComposedStrategyConfig) -> None:
        super().__init__(config)
        spec_dict = json.loads(config.spec_json)
        self.spec = ComposedStrategySpec.from_dict(spec_dict)
        self.instrument = None
        max_lookback = self._max_lookback()
        # Fiyat/hacim buffer'ları: düz list + amortize kompaksiyon. Eskiden
        # deque idi ve her bar list(deque) TAM KOPYASI alınıyordu (200k barda
        # ölçülen ~0.5s). Bloklar kuyruk-göreli dilim okur (closes[-n:]) —
        # değerler birebir aynı; yalnız kopya maliyeti kalkar. Custom blok
        # adaptörü izolasyon için pencere kopyası vermeye devam eder.
        self._buf_cap = max_lookback + 5
        # H1940 (etkin düzeltme): adx/stoch_rsi/wave_trend RECURSIVE (Wilder/EMA)
        # bloklar _nau_win ile son NAU_WINDOW=260 barlık SABİT pencere ister. Buffer
        # buf_cap↔4·buf_cap arası salınırken 260'a hiç ulaşmazsa (buf_cap küçükken)
        # _nau_win salınan pencere döndürür; recursive tohum her kompaksiyonda ~4×
        # sıçrayıp SAHTE kesişim üretir (metrik/skor bozulur). Bu bloklar varsa
        # buffer'ı en az NAU_WINDOW tut → kompaksiyon 260'ın altına inmez, _nau_win
        # her bar tutarlı 260 döndürür (NAU deque(maxlen=260) ile aynı).
        if any(b.type in _NAU_RECURSIVE_BLOCKS for b in self.spec.blocks):
            self._buf_cap = max(self._buf_cap, NAU_WINDOW)
        self._closes: list[float] = []
        # Hacim serisi — volume_spike bloğu + custom blokların
        # indicators["volumes"] erişimi için (closes ile aynı hizada).
        self._volumes: list[float] = []
        # High/low serileri — custom bloklar indicators["highs"]/["lows"] ile
        # gerçek OHLC-tabanlı indikatör (ADX/ATR/WaveTrend/Stochastic/Donchian)
        # hesaplayabilsin diye. closes/volumes ile aynı hizada, aynı buffer
        # yaşam döngüsü. Bar zaten tam OHLCV taşır (backtest._bars_from_df);
        # yalnızca kapanışa ek olarak high/low de yakalanır.
        self._highs: list[float] = []
        self._lows: list[float] = []
        # _iid() her çağrıda isinstance/from_str yapmasın (bar başına 4-6 çağrı)
        _iid_raw = config.instrument_id
        self._iid_obj = (
            InstrumentId.from_str(_iid_raw) if isinstance(_iid_raw, str) else _iid_raw
        )
        # _current_equity hızlı yolu: ilk başarılı strateji sonrakilerde
        # doğrudan kullanılır ("portfolio" | "balances" | None=henüz bilinmiyor)
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
        # L19: MTM anlık görüntülerinin bar zaman damgaları (ns) — backtest.py
        # bar-çözünürlüklü equity_curve_mtm'i (ts, eq) çifti olarak kurar.
        self._mtm_ts: list[int] = []
        # delay_fill buffer: pending entry order side when delay_fill=True
        self._pending_entry: str | None = None  # "BUY" | "SELL" | None
        # Karar günlüğü: her giriş/çıkış sinyalinde ateşleyen bloklar +
        # indikatör değerleri. Emirlere "dr:<seq>"/"xr:<seq>" tag'i basılır;
        # backtest sonrası positions↔fills join'i ile trade başına giriş/çıkış
        # sebebi üretilir (harvest: _mtm_equity ile aynı yaşam döngüsü).
        self._decision_log: list[dict] = []
        self._decision_seq: int = 0
        # delay_fill'de sinyal barındaki sebep, sonraki barın submit'ine taşınır
        self._pending_entry_reason: dict | None = None
        # L13: delay_fill'de ertelenen çıkış sebebi (giriş simetrisi).
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
        """Best-effort account equity. Portfolio.equity() → USDT balance fallback → sabit.

        Her bar çağrıldığından (MTM eğrisi) ilk başarılı yol ``_equity_mode``a
        kaydedilir ve sonraki barlarda deneme/fallback zinciri atlanır —
        aynı API, aynı değer, daha az Python maliyeti.
        """
        venue = self._iid_obj.venue
        # 1) Portfolio.equity(venue) — v2 native yol
        if self._equity_mode in (None, "portfolio"):
            try:
                eq = self.portfolio.equity(venue)
                if eq is not None:
                    self._equity_mode = "portfolio"
                    return float(eq.as_double() if hasattr(eq, "as_double") else eq)
            except Exception:
                pass
        # 2) Hesap bakiyelerini tara — USDT/USD öncelikli, sonra ilk bulduğu
        try:
            account = self.portfolio.account(venue)
            if account is not None:
                balances = account.balances()
                # Önce USDT veya USD ara
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
                                        # Hızlı-yol kilidi: sonraki barlarda
                                        # portfolio.equity() denemesi atlanır
                                        # (dokümante edilen _equity_mode cache'i;
                                        # eskiden 'balances' HİÇ set edilmiyordu).
                                        self._equity_mode = "balances"
                                        return _eq
                                    except Exception:
                                        continue
                # Fallback: ilk non-None balance (para birimi bilinmiyor, uyarı logla)
                for bal in balances.values():
                    for attr in ("total", "free"):
                        v = getattr(bal, attr, None)
                        if v is not None:
                            try:
                                result = float(
                                    v.as_double() if hasattr(v, "as_double") else v
                                )
                                self._equity_mode = "balances"  # hızlı-yol kilidi
                                self.log.warning(
                                    f"_current_equity: USDT/USD bulunamadı, ilk balance kullanılıyor ({result})"
                                )
                                return result
                            except Exception:
                                continue
        except Exception:
            pass
        # L43: fallback tek kaynaktan (app_constants.STARTING_CASH) — kopya
        # sabit sessizce ayrışabiliyordu. app_constants bağımsız modül,
        # döngüsel import riski yok.
        from app_constants import STARTING_CASH

        return float(STARTING_CASH)

    def _compute_qty(self, price: float) -> float:
        mode = self.spec.trade_size_mode
        if mode == "fixed":
            return float(self.spec.trade_size)
        if price <= 0:
            return float(self.spec.trade_size)
        if mode == "fixed_usdt":
            # Sabit dolar → quantity: USDT tutarını fiyata böl
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
    # Karar günlüğü (giriş/çıkış sebepleri)

    def _build_reason(self, kind, side, fires_per, blocks_list, bar, closes) -> dict:
        """Ateşleyen blokların label+params+indikatör-değer anlık görüntüsü."""
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
            "seq": None,  # _log_decision atar
            "kind": kind,
            "side": side,
            "bar_ts": int(bar.ts_event // 1_000_000_000),
            "submit_ts": None,
            "logic": self.spec.entry_logic if kind == "entry" else self.spec.exit_logic,
            "blocks": fired,
            "trend_bias": self._trend_bias,
        }

    def _log_decision(self, reason: dict, submit_ts: int) -> int:
        """Kararı günlüğe yaz, emir tag'inde kullanılacak seq'i döndür."""
        self._decision_seq += 1
        reason["seq"] = self._decision_seq
        reason["submit_ts"] = submit_ts
        self._decision_log.append(reason)
        return self._decision_seq

    def _can_submit_entry(self, side: OrderSide, bar: Bar) -> bool:
        """M17: emir GÖNDERMEDEN ön-kontrol.

        Flip yolundaki 'önce kapat, sonra gir' sırası, giriş zaten başarısız
        olacaksa (qty→0, make_qty hatası, bracket SL'i için ATR hazır değil)
        eski pozisyonu kapatıp stratejiyi istenmeden FLAT bırakıyordu. Flip
        ancak bu kontrol True ise kapanışla başlar.
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
        """H1999/M1840: enstrümanın açık (çalışan) emirlerini iptal et.

        Çıkış ve flip'te yalnız close_all_positions çağrılıyordu; bekleyen
        SL/TP koruma emirleri (bracket/SL-only) ve dolmamış GTC limit giriş
        emirleri iptal edilmiyordu. Sonuç: (a) bayat SL/TP sonraki pozisyonu
        eski seviyeden kapatabilir; (b) dolmamış limit girişler birikip
        çift/üçlü pozisyon açabilir. Nautilus manage_contingent_orders default
        kapalı olduğundan bunu elle yapıyoruz.
        """
        try:
            self.cancel_all_orders(self._iid())
        except Exception as e:
            self.log.error(f"_cancel_working başarısız: {e}")

    def _rollback_decision(self, seq: int | None) -> None:
        """L12: emir gönderilemedi — az önce yazılan kararı geri al.

        Karar logu submit'ten ÖNCE yazılıyor; emir çıkmazsa 'dr:<seq>' tag'i
        hiçbir emirde olmaz ve log hayalet 'entry' kayıtlarıyla şişerdi.
        Strateji tek iş parçacıklı — pop güvenli.
        """
        if seq is None:
            return
        if self._decision_log and self._decision_log[-1].get("seq") == seq:
            self._decision_log.pop()
            self._decision_seq -= 1

    def _submit_entry(
        self, side: OrderSide, bar: Bar, reason_seq: int | None = None
    ) -> bool:
        """Giriş emrini gönderir; GÖNDERİLDİYSE True (L12: bool sözleşmesi —
        False dönüşünde çağıran kararı geri alır)."""
        entry_tags = [f"dr:{reason_seq}"] if reason_seq is not None else None
        price = float(bar.close)
        qty_raw = self._compute_qty(price)
        if qty_raw <= 0:
            return False
        try:
            qty = self.instrument.make_qty(qty_raw)
        except Exception:
            return False
        # Rounding sonrası sıfıra düşen miktarla emir gönderme
        if float(qty) <= 0:
            return False

        if self.spec.use_bracket:
            sl_price, tp_price = self._compute_bracket_prices(side, price)
            if sl_price <= 0:
                # ATR henüz hazır değil — bracket order için SL hesaplanamıyor
                return False
            entry_price_obj = None
            entry_order_type = OrderType.MARKET
            if self.spec.order_type == "limit":
                entry_order_type = OrderType.LIMIT
                entry_price_obj = self.instrument.make_price(
                    self._entry_limit_price(side, price)
                )

            if tp_price is None:
                # tp_type == 'off': Nautilus bracket() her zaman TP LIMIT emri
                # kurar ve price=None'ı KABUL ETMEZ (TypeError — agent loglarında
                # 8× görülen canlı bug). Entry + SL-only'a düş: entry emrini
                # normal gönder, SL'i reduce-only STOP_MARKET olarak ekle.
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
                    reduce_only=True,  # pozisyon yoksa ters pozisyon AÇMAZ
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
                self._mtm_ts.append(int(bar.ts_event))  # L19: hizalı zaman
        except Exception:
            pass

        # L13: delay_fill — ertelenen ÇIKIŞ, ertelenen girişten ÖNCE işlenir
        # (aynı barda hem çıkış hem giriş kuyruktaysa sıra: çıkış → giriş;
        # flip semantiği pending_entry dalındaki close_all ile aynı kalır).
        if self.spec.delay_fill and self._pending_exit_reason is not None:
            reason = self._pending_exit_reason
            self._pending_exit_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            if self.portfolio.is_net_long(self._iid()) or self.portfolio.is_net_short(
                self._iid()
            ):
                seq = self._log_decision(reason, submit_ts)
                self._cancel_working()  # H1999: bayat SL/TP + dolmamış limit
                self.close_all_positions(self._iid(), tags=[f"xr:{seq}"])

        # delay_fill: execute deferred entry from previous bar
        if self.spec.delay_fill and self._pending_entry is not None:
            side = self._pending_entry
            self._pending_entry = None
            # Sinyal barında biriken sebep — yalnız emir GERÇEKTEN gönderilirse
            # günlüğe geçer (pozisyon zaten açıksa karar sessizce düşer).
            reason = self._pending_entry_reason
            self._pending_entry_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            is_long = self.portfolio.is_net_long(self._iid())
            is_short = self.portfolio.is_net_short(self._iid())
            if side == "BUY" and not is_long:
                # M17: flip ön-kontrolü (bkz. anlık yol) — giriş çıkamayacaksa
                # eski pozisyon kapatılmaz.
                if is_short and not self._can_submit_entry(OrderSide.BUY, bar):
                    pass
                else:
                    # H1999 (flip: bayat SL/TP) + #4 (flat: dolmamış GTC limit
                    # girişi). Koşulsuz — yeni giriş göndermeden önce açık emirleri
                    # daima temizle ki limit girişleri birikip birlikte dolmasın.
                    self._cancel_working()
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.BUY, bar, reason_seq=seq):
                        self._rollback_decision(seq)
            elif side == "SELL" and not is_short:
                if is_long and not self._can_submit_entry(OrderSide.SELL, bar):
                    pass
                else:
                    self._cancel_working()  # H1999 (flip) + #4 (flat limit birikmesi)
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                        self._rollback_decision(seq)

        # Buffer'lara ekle; amortize kompaksiyon (4×cap'te kuyruk kırp) — her
        # bar tam kopya (eski list(deque)) yerine sıfır-kopya paylaşım.
        # Dört seri (close/volume/high/low) SENKRON kırpılır → hizalı kalır.
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
                # Yükseliş trendinde short'ları bastır. Eskiden bu dal
                # `and not allow_short` ile korunuyordu — oysa short'ların
                # ateşlendiği TEK durum allow_short=True'dur; guard tam o durumu
                # atlayıp ters-trend short'ları filtresiz geçiriyordu (filtre
                # hiç iş yapmıyordu). allow_short=False iken short_fires zaten
                # False olduğundan bu satır o durumda zararsız.
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
                # L13: delay_fill artık ÇIKIŞLARA da uygulanıyor — eskiden
                # girişler bir bar gecikirken çıkışlar sinyal barında işlenir,
                # çıkışlar sistematik olarak bir bar avantajlı olurdu
                # (asimetrik/iyimser zamanlama). Bilinçli davranış değişikliği.
                self._pending_exit_reason = reason
            else:
                seq = self._log_decision(reason, reason["bar_ts"])
                self._cancel_working()  # H1999: bayat SL/TP + dolmamış limit
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
                # M17: flip'te ÖNCE giriş ön-kontrolü — yeni emir zaten
                # çıkamayacaksa eski pozisyonu kapatma (istenmeden flat kalma).
                if is_short:
                    if not self._can_submit_entry(OrderSide.BUY, bar):
                        return
                    self._cancel_working()  # H1999
                    self.close_all_positions(self._iid(), tags=["flip"])
                else:
                    self._cancel_working()  # #4: flat'te dolmamış GTC limit girişi birikmesin
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
                    self._cancel_working()  # #4: flat'te dolmamış GTC limit girişi birikmesin
                seq = self._log_decision(reason, reason["bar_ts"])
                if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                    self._rollback_decision(seq)

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid(), tags=["eob"])
