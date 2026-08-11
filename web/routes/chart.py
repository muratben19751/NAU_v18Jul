"""Chart data endpoint — serves OHLCV + strategy indicators for Lightweight Charts.

GET /chart/data?symbol=BTCUSDT&category=linear&interval=1&bars=500
GET /chart/data?symbol=BTCUSDT&category=linear&interval=1&start_ts=...&end_ts=...&spec_id=...
Returns JSON: {candles: [...], trades: [], indicators: {overlays:[...], panes:[...]}}
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/chart")

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")

# Bar süresi (saniye) — hem pencere büyüklüğü tahmininde hem indirme
# bütçesinde kullanılır. Eskiden istek gövdesinde iki ayrı yerel kopyası vardı.
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

# ── İndirme bütçesi ────────────────────────────────────────────────────────
# DeepR 2026-08-11 [ORTA]: pencere-genişliği kontrolü tek başına yeterli
# DEĞİLDİ. `load_bybit_bars` istenen başlangıç önbelleğin başından eskiyse
# ARADAKİ TÜM BOŞLUĞU doldurmaya çalışır; yani 2 GÜNLÜK bir pencere bile
# (kapıdan rahat geçer) 2020→bugün arası 1 dakikalık bir backfill başlatabilir.
# Sayfa başına ağ isteği + 429/5xx'te 2-32 sn uyku ve toplam sayfa sayısında
# hiçbir üst sınır olmadığı için istek saatlerce asılı kalıyordu; üstelik
# yazma yolu per-key kilidi tuttuğu için aynı seriyi okuyan backtest'ler de
# bekliyordu. Tetiklemek için kötü niyet gerekmiyor: /reports'ta eski bir
# koşunun bir işlemine tıklamak tam bu URL'i üretiyor (chart.js _reloadForTrade).
#
# Kural: bir istek önbelleğin DIŞINA en fazla bu kadar bar taşabilir. Aşımda
# pencere erişilebilir aralığa kırpılır ve ne olduğu söylenir; hiçbir şey
# kalmıyorsa veri uydurmak yerine sebebi yazan bir hata döner.
_MAX_BACKFILL_BARS = 20_000


def _clamp_backfill_window(
    symbol: str,
    category: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime, str]:
    """Pencereyi "önbellek ± bütçe" aralığına kırp → (start, end, notice).

    ``notice`` boşsa kırpma olmamıştır. Dönen ``start >= end`` ise istenen
    pencere erişilebilir aralığın TAMAMEN dışındadır (çağıran hata basar).
    Önbellek hiç yoksa pencere olduğu gibi döner: o durumda indirilecek şey
    zaten ``_MAX_WINDOW_CANDLES`` ile sınırlı olan pencerenin kendisidir.
    """
    from data import coverage_range

    try:
        cov = coverage_range(
            "bybit", symbol=symbol, category=category, interval=interval
        )
    except Exception:  # noqa: BLE001 — bir kapsam okuması grafiği düşürmemeli
        cov = None
    if not cov:
        return start, end, ""

    budget = timedelta(seconds=_MAX_BACKFILL_BARS * _SEC_PER_BAR.get(interval, 60))
    # coverage_range GÜN döndürür; bitiş günü dahil sayılır (+1 gün).
    cached_start = datetime.fromisoformat(cov["start"]).replace(tzinfo=UTC)
    cached_end = datetime.fromisoformat(cov["end"]).replace(tzinfo=UTC) + timedelta(
        days=1
    )
    new_start = max(start, cached_start - budget)
    new_end = min(end, cached_end + budget)
    if new_start >= new_end:
        # Kırpmadan sonra geriye bir şey kalmadı — çağıranın hata metni için
        # önbelleğin gerçek kapsamını taşı.
        return new_start, new_end, f"{cov['start']} … {cov['end']}"
    if (new_start, new_end) == (start, end):
        return start, end, ""
    return (
        new_start,
        new_end,
        (
            f"Range trimmed to the download budget ({_MAX_BACKFILL_BARS:,} bars "
            f"per request). Cached data covers {cov['start']} … {cov['end']}; "
            "reload to fetch further, or download the range from the Data screen."
        ),
    )


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
    from data import BYBIT_ALL_INTERVALS, BYBIT_CATEGORIES, load_bybit_bars

    # /backtest/run and /data validate against the same whitelists; this
    # endpoint didn't, so a malformed/adversarial symbol|category|interval
    # went straight into load_bybit_bars and its exception text (file paths,
    # internal names) came back verbatim to the client (see the except below).
    symbol = symbol.upper()
    if not _SYMBOL_RE.match(symbol):
        return JSONResponse({"error": "invalid symbol"}, status_code=400)
    if category not in BYBIT_CATEGORIES:
        return JSONResponse({"error": "invalid category"}, status_code=400)
    if interval not in dict(BYBIT_ALL_INTERVALS):
        return JSONResponse({"error": "invalid interval"}, status_code=400)

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
            end = datetime.now(UTC)
            start = end - timedelta(seconds=bars * _SEC_PER_BAR.get(interval, 60) * 1.2)

        # İndirme bütçesi: pencerenin önbelleğin dışına taşan kısmını sınırla
        # (bkz. _clamp_backfill_window). Bu kontrol pencere-genişliği testinden
        # SONRA gelir, çünkü orası zaten "tarayıcıyı öldürecek kadar mum" hâlini
        # eliyor; buradaki soru ise "kaç bar İNDİRİLECEK".
        start, end, notice = _clamp_backfill_window(
            symbol, category, interval, start, end
        )
        if start >= end:
            return JSONResponse(
                {
                    "error": (
                        f"That window is more than {_MAX_BACKFILL_BARS:,} bars "
                        f"outside the downloaded data ({notice}). Download the "
                        "range from the Data screen first."
                    ),
                    "candles": [],
                    "trades": [],
                    "indicators": {"overlays": [], "panes": []},
                }
            )

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
                    "notice": notice,
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
            indicators_error = None
            if spec_id:
                try:
                    from chart_indicators import indicators_for_spec
                    from composer import load_catalog

                    spec = next((s for s in load_catalog() if s.id == spec_id), None)
                    if spec is not None:
                        indicators = indicators_for_spec(spec, times, closes)
                except Exception as exc:
                    # Gösterge hesabı grafiği BLOKLAMAMALI (mumlar geçerli), ama
                    # sessizce boş dönmemeli: katman hiç görünmeyince kullanıcı
                    # "bu stratejide gösterge yok" sonucuna varıyordu. Hatayı
                    # logla ve payload'da taşı (DeepR 2026-08-11 [ORTA]).
                    logging.warning(
                        "indicator computation failed for spec %s: %s",
                        spec_id,
                        exc,
                        exc_info=True,
                    )
                    indicators_error = f"{type(exc).__name__}: {exc}"

            return {
                "candles": candles,
                "trades": [],
                "indicators": indicators,
                "indicators_error": indicators_error,
                # Pencere indirme bütçesine kırpıldıysa bunu SÖYLE: kullanıcı
                # istediğinden dar bir grafiğe bakıyor olabilir (chart.js bunu
                # toast'a çevirir).
                "notice": notice,
            }

        import asyncio

        return JSONResponse(await asyncio.to_thread(_build_payload))

    except Exception as e:
        logging.warning(
            "chart_data failed for %s/%s/%s: %s",
            symbol,
            category,
            interval,
            e,
            exc_info=True,
        )
        return JSONResponse(
            {
                "error": "chart data unavailable",
                "candles": [],
                "trades": [],
                "indicators": {"overlays": [], "panes": []},
            },
            status_code=500,
        )
