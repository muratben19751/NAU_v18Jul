"""Indicator registry (STUDIO_SPEC 1.3).

Maps schema indicator keys to parameter specs and to the host app's real
indicator implementations in ``indicators.py``.

``impl`` calling convention
---------------------------
Every wired ``impl`` has the same shape::

    impl(bars: Bars, **params) -> Any

where ``bars`` is any mapping with ``high`` / ``low`` / ``close`` / ``volume``
sequences (a dict of lists or an OHLCV DataFrame both work), and ``params``
are the *schema* parameter names from this registry — the thin adapters below
translate them to each feature function's own argument names (schema ``len``
→ ``period``, ``n1``/``n2`` → ``channel_len``/``avg_len``, and so on), so the
schema vocabulary stays stable even if a feature function is renamed.

Each impl returns the indicator value **at the last bar** of the window it is
given, matching what a rule comparison evaluates against.

``impl`` is still None for the entries the host app has no feature function
for (``macd``, ``funding_z``, ``oi_z``, ``cvd_divergence``, ``volume_profile``,
``time_stop``, ``session_filter``) — those stay declarative until an
implementation exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Bars = Mapping[str, Sequence[float]]


def _col(bars: Bars, name: str) -> list[float]:
    """OHLCV column as a plain list (dict-of-lists and DataFrame both work)."""
    return [float(x) for x in bars[name]]


def _impl_wavetrend(bars: Bars, n1: int = 10, n2: int = 21) -> Any:
    from indicators import calc_wave_trend

    return calc_wave_trend(
        _col(bars, "high"),
        _col(bars, "low"),
        _col(bars, "close"),
        channel_len=n1,
        avg_len=n2,
    )


def _impl_rsi(bars: Bars, len: int = 14) -> Any:  # noqa: A002 — schema param name
    from indicators import calc_rsi

    return calc_rsi(_col(bars, "close"), period=len)


def _impl_stochrsi(bars: Bars, len: int = 14, k: int = 3, d: int = 3) -> Any:  # noqa: A002
    from indicators import calc_stoch_rsi

    return calc_stoch_rsi(
        _col(bars, "close"), rsi_period=len, stoch_period=len, k_period=k, d_period=d
    )


def _impl_ema(bars: Bars, len: int = 200) -> Any:  # noqa: A002
    from indicators import ema

    series = ema(_col(bars, "close"), period=len)
    return series[-1] if series else None


def _impl_adx(bars: Bars, len: int = 14) -> Any:  # noqa: A002
    from indicators import calc_adx

    return calc_adx(
        _col(bars, "high"), _col(bars, "low"), _col(bars, "close"), period=len
    )


def _impl_nadaraya_watson(bars: Bars, bandwidth: float = 8, mult: float = 3) -> Any:
    from indicators import calc_nadaraya_watson

    return calc_nadaraya_watson(
        _col(bars, "close"), bandwidth=bandwidth, multiplier=mult
    )


def _impl_relative_volume(bars: Bars, window: int = 20) -> Any:
    from indicators import calc_volume_change

    return calc_volume_change(_col(bars, "volume"), lookback=window)


def _impl_atr(bars: Bars, len: int = 14) -> Any:  # noqa: A002
    from indicators import calc_atr

    return calc_atr(
        _col(bars, "high"), _col(bars, "low"), _col(bars, "close"), period=len
    )


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str = "float"  # float | int | str
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
        "crosses_above",
        "crosses_below",
        "gt",
        "lt",
        "gte",
        "lte",
    )
    # Host app's feature function — see the module docstring for the calling
    # convention. None where no implementation exists yet.
    impl: Callable[..., Any] | None = None


def _spec(key, label, cat, params=(), operators=None, impl=None) -> IndicatorSpec:
    kw: dict[str, Any] = {
        "key": key,
        "label": label,
        "category": cat,
        "params": tuple(params),
    }
    if operators is not None:
        kw["operators"] = tuple(operators)
    if impl is not None:
        kw["impl"] = impl
    return IndicatorSpec(**kw)


INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    s.key: s
    for s in [
        _spec(
            "wavetrend",
            "WaveTrend",
            "momentum",
            [
                ParamSpec("n1", "int", 2, 50, 10),
                ParamSpec("n2", "int", 2, 80, 21),
            ],
            impl=_impl_wavetrend,
        ),
        _spec(
            "rsi",
            "RSI",
            "momentum",
            [ParamSpec("len", "int", 2, 200, 14)],
            impl=_impl_rsi,
        ),
        _spec(
            "stochrsi",
            "StochRSI",
            "momentum",
            [
                ParamSpec("len", "int", 2, 200, 14),
                ParamSpec("k", "int", 1, 50, 3),
                ParamSpec("d", "int", 1, 50, 3),
            ],
            impl=_impl_stochrsi,
        ),
        _spec(
            "macd",
            "MACD",
            "momentum",
            [
                ParamSpec("fast", "int", 2, 100, 12),
                ParamSpec("slow", "int", 3, 200, 26),
                ParamSpec("signal", "int", 1, 100, 9),
            ],
        ),
        _spec(
            "ema",
            "EMA",
            "trend",
            [ParamSpec("len", "int", 2, 500, 200)],
            operators=["price_above", "price_below", "crosses_above", "crosses_below"],
            impl=_impl_ema,
        ),
        _spec(
            "adx",
            "ADX",
            "trend",
            [ParamSpec("len", "int", 2, 100, 14)],
            operators=["gt", "lt", "gte", "lte"],
            impl=_impl_adx,
        ),
        _spec(
            "nadaraya_watson",
            "Nadaraya-Watson",
            "trend",
            [
                ParamSpec("bandwidth", "float", 1, 50, 8),
                ParamSpec("mult", "float", 0.5, 10, 3),
            ],
            impl=_impl_nadaraya_watson,
        ),
        _spec(
            "relative_volume",
            "Relative Volume",
            "volume",
            [ParamSpec("window", "int", 2, 500, 20)],
            operators=["gt", "lt", "gte", "lte"],
            impl=_impl_relative_volume,
        ),
        _spec(
            "funding_z",
            "Funding z-score",
            "perp",
            [ParamSpec("lookback", "int", 8, 2000, 96)],
            operators=["gt", "lt", "gte", "lte"],
        ),
        _spec(
            "oi_z",
            "OI z-score",
            "perp",
            [ParamSpec("lookback", "int", 8, 2000, 96)],
            operators=["gt", "lt", "gte", "lte"],
        ),
        _spec(
            "cvd_divergence",
            "CVD Divergence",
            "flow",
            [ParamSpec("lookback", "int", 5, 500, 50)],
            operators=["true"],
        ),
        _spec(
            "volume_profile",
            "Volume Profile",
            "structure",
            [
                ParamSpec("rows", "int", 10, 200, 24),
                ParamSpec("window", "int", 20, 2000, 240),
            ],
            operators=["price_above", "price_below"],
        ),
        _spec(
            "atr",
            "ATR",
            "volatility",
            [ParamSpec("len", "int", 2, 100, 14)],
            operators=["gt", "lt"],
            impl=_impl_atr,
        ),
        _spec(
            "time_stop",
            "Time stop",
            "exit",
            [ParamSpec("bars", "int", 1, 5000, 48)],
            operators=["true"],
        ),
        _spec(
            "session_filter",
            "Session filter",
            "structure",
            [ParamSpec("sessions", "str", default="london,ny")],
            operators=["within_session"],
        ),
    ]
}


def library_by_category() -> dict[str, list[IndicatorSpec]]:
    out: dict[str, list[IndicatorSpec]] = {}
    for spec in INDICATOR_REGISTRY.values():
        out.setdefault(spec.category, []).append(spec)
    return out
