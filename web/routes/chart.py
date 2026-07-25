"""Chart data endpoint — serves OHLCV + strategy indicators for Lightweight Charts.

GET /chart/data?symbol=BTCUSDT&category=linear&interval=1&bars=500
GET /chart/data?symbol=BTCUSDT&category=linear&interval=1&start_ts=...&end_ts=...&spec_id=...
Returns JSON: {candles: [...], trades: [], indicators: {overlays:[...], panes:[...]}}
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/chart")


@router.get("/data", response_class=JSONResponse)
async def chart_data(
    symbol: str = Query(default="BTCUSDT"),
    category: str = Query(default="linear"),
    interval: str = Query(default="1"),
    bars: int = Query(default=500, ge=50, le=10080),
    start_ts: int = Query(default=0),  # unix seconds — if set, overrides bars
    end_ts: int = Query(default=0),
    spec_id: str = Query(default=""),  # strategy spec — extract indicators from here
):
    """Return OHLCV bars + strategy indicators. Window by ts range or last N bars."""
    from datetime import datetime, timedelta

    from data import load_bybit_bars

    _SEC_PER_BAR = {
        "1": 60,
        "5": 300,
        "15": 900,
        "30": 1800,
        "60": 3600,
        "240": 14400,
        "720": 43200,
        "D": 86400,
    }
    _MAX_WINDOW_CANDLES = 60_000  # browser + fetch protection

    try:
        if start_ts and end_ts:
            # Feasibility: window/TF combination must produce a reasonable
            # candle count — 6-year window × 1m = ~3.1M candles (kills the
            # browser and Bybit backfill). Reject WITHOUT loading data.
            # L9: estimate INCLUDES 10%+10% margin (est × 1.2) — the old
            # version didn't count the margin, so the backstop could clip the
            # FIRST bars of the request.
            est = (end_ts - start_ts) * 1.2 / _SEC_PER_BAR.get(interval, 60)
            if est > _MAX_WINDOW_CANDLES:
                return JSONResponse(
                    {
                        "error": (
                            f"{interval} is too fine for this range "
                            f"(~{est / 1000:.0f}k candles) — select a larger TF."
                        ),
                        "candles": [],
                        "trades": [],
                        "indicators": {"overlays": [], "panes": []},
                    }
                )
            start = datetime.fromtimestamp(start_ts, tz=UTC)
            end = datetime.fromtimestamp(end_ts, tz=UTC)
            margin = timedelta(seconds=(end_ts - start_ts) * 0.1)
            start -= margin
            end += margin
        else:
            ms_per_bar = {
                "1": 60,
                "5": 300,
                "15": 900,
                "30": 1800,
                "60": 3600,
                "240": 14400,
                "720": 43200,
                "D": 86400,
            }.get(interval, 60)
            end = datetime.now(UTC)
            start = end - timedelta(seconds=bars * ms_per_bar * 1.2)

        # M9: data loading + candle construction + indicator computation in a
        # SINGLE synchronous closure, via asyncio.to_thread — the old version
        # locked the event loop throughout parquet reads + iterrows + (on a
        # window spilling out of cache) the 0.15s-sleepy Bybit backfill, making
        # ALL requests wait.
        def _build_payload():
            try:
                df = load_bybit_bars(
                    symbol=symbol,
                    interval=interval,
                    category=category,
                    start=start,
                    end=end,
                )
            except Exception:
                # Offline / Bybit unreachable: the tail-extend fetch inside
                # load_bybit_bars died on the network call. The chart must
                # still render — serve whatever the parquet cache holds for
                # the requested window instead of erroring the whole panel.
                import pandas as pd

                from data import _bybit_cache_path

                cache_path = _bybit_cache_path(category, symbol, interval)
                if not cache_path.exists():
                    raise
                df = pd.read_parquet(cache_path).loc[start:end]
            if df.empty:
                return {
                    "candles": [],
                    "trades": [],
                    "indicators": {"overlays": [], "panes": []},
                }

            if not (start_ts and end_ts):
                df2 = df.iloc[-bars:]
            elif len(df) > _MAX_WINDOW_CANDLES:
                # L9: the backstop PRESERVES the core window — the requested
                # [start_ts, end_ts] slice is guaranteed first, and the
                # remaining budget is allocated to the margins (the old
                # iloc[-N:] silently dropped the oldest request bars).
                _core = df.loc[
                    datetime.fromtimestamp(start_ts, tz=UTC) : datetime.fromtimestamp(
                        end_ts, tz=UTC
                    )
                ]
                df2 = (
                    _core.iloc[-_MAX_WINDOW_CANDLES:]
                    if len(_core)
                    else df.iloc[-_MAX_WINDOW_CANDLES:]
                )
            else:
                df2 = df

            times = [int(ts.timestamp()) for ts in df2.index]
            closes = [float(x) for x in df2["close"]]
            # M9: column-based access instead of iterrows() (~10× faster build).
            opens = df2["open"].to_list()
            highs = df2["high"].to_list()
            lows = df2["low"].to_list()
            vols = df2["volume"].to_list()
            candles = [
                {
                    "time": times[i],
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(vols[i]),
                }
                for i in range(len(times))
            ]

            # Compute the strategy's actual indicators over this window
            indicators = {"overlays": [], "panes": []}
            if spec_id:
                try:
                    from chart_indicators import indicators_for_spec
                    from composer import load_catalog

                    spec = next((s for s in load_catalog() if s.id == spec_id), None)
                    if spec is not None:
                        indicators = indicators_for_spec(spec, times, closes)
                except Exception:
                    pass  # indicator computation must not block the chart

            return {"candles": candles, "trades": [], "indicators": indicators}

        import asyncio

        return JSONResponse(await asyncio.to_thread(_build_payload))

    except Exception as e:
        return JSONResponse(
            {
                "error": str(e),
                "candles": [],
                "trades": [],
                "indicators": {"overlays": [], "panes": []},
            },
            status_code=500,
        )
