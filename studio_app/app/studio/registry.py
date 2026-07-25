"""Indicator registry (STUDIO_SPEC 1.3).

Maps schema indicator keys to parameter specs and, at integration time, to
the host app's real indicator implementations. `impl` stays None here; in
nautilus_web_app wire each entry to your existing indicator class/factory.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str = "float"           # float | int | str
    min: float | None = None
    max: float | None = None
    default: Any = None


@dataclass(frozen=True)
class IndicatorSpec:
    key: str
    label: str
    category: str
    params: tuple[ParamSpec, ...] = ()
    operators: tuple[str, ...] = (
        "crosses_above", "crosses_below", "gt", "lt", "gte", "lte",
    )
    impl: Callable[..., Any] | None = None  # INTEGRATION POINT


def _spec(key, label, cat, params=(), operators=None) -> IndicatorSpec:
    kw: dict[str, Any] = {"key": key, "label": label, "category": cat, "params": tuple(params)}
    if operators is not None:
        kw["operators"] = tuple(operators)
    return IndicatorSpec(**kw)


INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    s.key: s
    for s in [
        _spec("wavetrend", "WaveTrend", "momentum", [
            ParamSpec("n1", "int", 2, 50, 10),
            ParamSpec("n2", "int", 2, 80, 21),
        ]),
        _spec("rsi", "RSI", "momentum", [ParamSpec("len", "int", 2, 200, 14)]),
        _spec("stochrsi", "StochRSI", "momentum", [
            ParamSpec("len", "int", 2, 200, 14),
            ParamSpec("k", "int", 1, 50, 3),
            ParamSpec("d", "int", 1, 50, 3),
        ]),
        _spec("macd", "MACD", "momentum", [
            ParamSpec("fast", "int", 2, 100, 12),
            ParamSpec("slow", "int", 3, 200, 26),
            ParamSpec("signal", "int", 1, 100, 9),
        ]),
        _spec("ema", "EMA", "trend",
              [ParamSpec("len", "int", 2, 500, 200)],
              operators=["price_above", "price_below", "crosses_above", "crosses_below"]),
        _spec("adx", "ADX", "trend", [ParamSpec("len", "int", 2, 100, 14)],
              operators=["gt", "lt", "gte", "lte"]),
        _spec("nadaraya_watson", "Nadaraya-Watson", "trend", [
            ParamSpec("bandwidth", "float", 1, 50, 8),
            ParamSpec("mult", "float", 0.5, 10, 3),
        ]),
        _spec("relative_volume", "Relative Volume", "volume",
              [ParamSpec("window", "int", 2, 500, 20)],
              operators=["gt", "lt", "gte", "lte"]),
        _spec("funding_z", "Funding z-score", "perp",
              [ParamSpec("lookback", "int", 8, 2000, 96)],
              operators=["gt", "lt", "gte", "lte"]),
        _spec("oi_z", "OI z-score", "perp",
              [ParamSpec("lookback", "int", 8, 2000, 96)],
              operators=["gt", "lt", "gte", "lte"]),
        _spec("cvd_divergence", "CVD Divergence", "flow",
              [ParamSpec("lookback", "int", 5, 500, 50)],
              operators=["true"]),
        _spec("volume_profile", "Volume Profile", "structure", [
            ParamSpec("rows", "int", 10, 200, 24),
            ParamSpec("window", "int", 20, 2000, 240),
        ], operators=["price_above", "price_below"]),
        _spec("atr", "ATR", "volatility",
              [ParamSpec("len", "int", 2, 100, 14)],
              operators=["gt", "lt"]),
        _spec("time_stop", "Time stop", "exit",
              [ParamSpec("bars", "int", 1, 5000, 48)], operators=["true"]),
        _spec("session_filter", "Session filter", "structure",
              [ParamSpec("sessions", "str", default="london,ny")],
              operators=["within_session"]),
    ]
}


def library_by_category() -> dict[str, list[IndicatorSpec]]:
    out: dict[str, list[IndicatorSpec]] = {}
    for spec in INDICATOR_REGISTRY.values():
        out.setdefault(spec.category, []).append(spec)
    return out
