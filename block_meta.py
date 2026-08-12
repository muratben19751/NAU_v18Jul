"""Built-in block metadata — labels, param specs, help text, wiki refs.

composer.py decomposition (Adım 1, safe-first slice): extracted verbatim
from composer.py. Pure data, zero internal dependency on anything else in
that file — BLOCK_REGISTRY (staying in composer.py; the registry core is
out of this session's scope) reads _BUILTIN_META back via a plain import,
same as before the move.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

# Bir bloğun rol alanının kabul ettiği TÜM değerler. `composer_spec.validate`
# ve `agent._coerce_block` de bu ikiliyi varsayıyor; form uçları da aynı listeyi
# kullansın diye burada, meta'nın yanında duruyor.
BLOCK_ROLES = ("entry", "exit")


def coerce_params(meta: dict, raw: dict) -> dict:
    """Form/JSON parametrelerini katalog meta'sının TİP ve MİN/MAX'ına zorla.

    DeepR 2026-08-11 [DÜŞÜK]: `POST /strategy/drafts` parametreyi yalnız
    `int()`/`float()` ile çeviriyor, `_BUILTIN_META`'daki `min`/`max` ise
    SADECE HTML input attribute'u olarak kullanılıyordu — yani tarayıcı dışı
    her yol (curl, otomasyon, eski tarayıcı) sınırları geçiyordu. Sonuç sessiz
    bir ölü bloktu: `p_period=0` + donchian → `max([])` ValueError,
    `p_lookback=0` + price_breakout → `max([])`, `p_fast=0` + ma_cross →
    ZeroDivisionError; üçü de `composer._eval_block`'un geniş `except`'i
    tarafından yutuluyor, blok her barda `None` dönüyor ve kullanıcı HATA
    DEĞİL, "0 işlemli geçerli görünen bir backtest" görüyordu.

    `robustness._clamp_commission_pct` ("HTML min/max yalnız tarayıcı tarafı")
    ve `agent._coerce_block` (LLM önerileri için AYNI kırpma) aynı dersin
    başka yerlerdeki uygulamaları — bu, form yolundaki eksik halkaydı.

    Sözleşme `agent._coerce_block` ile birebir: çevrilemeyen değer default'a
    düşer, sayısal değer aralığa KIRPILIR, enum dışı seçim default olur.
    Meta'da olmayan parametre adları YOK SAYILIR (bilinmeyen `p_*` alanı
    spec'e sızmasın).
    """
    out: dict = {}
    for pname, pspec in (meta.get("params") or {}).items():
        if pname not in raw:
            continue
        value = raw[pname]
        try:
            if pspec["type"] == "int":
                v = max(pspec["min"], min(pspec["max"], int(value)))
            elif pspec["type"] == "float":
                v = max(pspec["min"], min(pspec["max"], float(value)))
            else:
                v = value if value in pspec["options"] else pspec["default"]
        except (TypeError, ValueError, KeyError):
            v = pspec.get("default", 0)
        out[pname] = v
    return out


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
