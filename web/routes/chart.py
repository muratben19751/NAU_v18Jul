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
    spec_id: str = Query(default=""),  # strateji spec — indikatörleri buradan çıkar
):
    """Return OHLCV bars + strategy indicators. Window by ts range or last N bars."""
    from datetime import datetime, timedelta

    from data import load_bybit_bars

    _SEC_PER_BAR = {
        "1": 60,
        "5": 300,
        "15": 900,
        "60": 3600,
        "240": 14400,
        "D": 86400,
    }
    _MAX_WINDOW_CANDLES = 60_000  # tarayıcı + fetch koruması

    try:
        if start_ts and end_ts:
            # Fizibilite: pencere/TF kombinasyonu makul mum sayısı üretmeli —
            # 6 yıllık pencere × 1m = ~3.1M mum (tarayıcıyı ve Bybit
            # backfill'ini öldürür). Veri YÜKLEMEDEN reddet.
            # L9: tahmine %10+%10 marj DAHİL (est × 1.2) — eski hâli marjı
            # saymadığından backstop isteğin İLK barlarını kırpabiliyordu.
            est = (end_ts - start_ts) * 1.2 / _SEC_PER_BAR.get(interval, 60)
            if est > _MAX_WINDOW_CANDLES:
                return JSONResponse(
                    {
                        "error": (
                            f"Bu aralık için {interval} çok ince "
                            f"(~{est / 1000:.0f}k mum) — daha büyük TF seçin."
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
                "60": 3600,
                "240": 14400,
                "D": 86400,
            }.get(interval, 60)
            end = datetime.now(UTC)
            start = end - timedelta(seconds=bars * ms_per_bar * 1.2)

        # M9: veri yükleme + mum inşası + indikatör hesabı TEK senkron
        # closure'da, asyncio.to_thread ile — eski hâli event loop'u parquet
        # okuma + iterrows + (cache dışına taşan pencerede) 0.15s uykulu
        # Bybit backfill'i boyunca kilitleyip TÜM istekleri bekletiyordu.
        def _build_payload():
            df = load_bybit_bars(
                symbol=symbol,
                interval=interval,
                category=category,
                start=start,
                end=end,
            )
            if df.empty:
                return {
                    "candles": [],
                    "trades": [],
                    "indicators": {"overlays": [], "panes": []},
                }

            if not (start_ts and end_ts):
                df2 = df.iloc[-bars:]
            elif len(df) > _MAX_WINDOW_CANDLES:
                # L9: backstop çekirdek pencereyi KORUR — önce istenen
                # [start_ts, end_ts] dilimi garanti edilir, kalan bütçe
                # marjlara pay edilir (eski iloc[-N:] en eski istek
                # barlarını sessizce atıyordu).
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
            # M9: iterrows() yerine kolon-bazlı erişim (~10× hızlı inşa).
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

            # Stratejinin gerçek indikatörlerini bu pencere üzerinde hesapla
            indicators = {"overlays": [], "panes": []}
            if spec_id:
                try:
                    from chart_indicators import indicators_for_spec
                    from composer import load_catalog

                    spec = next((s for s in load_catalog() if s.id == spec_id), None)
                    if spec is not None:
                        indicators = indicators_for_spec(spec, times, closes)
                except Exception:
                    pass  # indikatör hesabı grafiği bloke etmesin

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
