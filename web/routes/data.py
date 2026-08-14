"""Instrument catalog — Nautilus-idiomatic /data screen.

Endpoints:
    GET  /data                         Full catalog page (Bybit + US-Index).
    POST /data/refresh/bybit           Fetch a (symbol, category, interval) cell.
    POST /data/refresh/index           Fetch a (ticker, granularity) row.
    POST /data/index/discover          Rebuild the US-index ticker registry.
    POST /data/catalog/write           Write pandas cache → Nautilus ParquetDataCatalog.

All fetch endpoints return a rendered fragment (single row / cell) so HTMX
can hot-swap the DOM in place without re-fetching the whole page.

Wiki References
---------------
See: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]],
[[index_backtest_via_equity_proxy]], [[precision_modes]], [[nau_deepr_toplu_sertlestirme_2026_08]]

The screen surfaces wiki-flagged pitfalls (size_precision=0 Equity trap; BarType DSL
origin distinction; book_type ↔ granularity mismatch) as badges.

`refresh_index` now validates `start`/`end` the same way
`web/routes/backtest.py`'s `_invalid_date_range` does (2026-08-08 DeepR
finding) — a bad date used to fall through to the blanket
`except Exception: 500` and leak the raw `ValueError` text to the client.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from data import (
    _BYBIT_MS,
    BYBIT_ALL_INTERVALS,
    BYBIT_CATEGORIES,
    BYBIT_SYMBOLS,
    discover_index_tickers,
    list_catalog,
    refresh_row,
    write_to_nautilus_catalog,
)
from web.templating import get_market_info, templates

router = APIRouter(prefix="/data")


def _invalid_date_range(start: str | None, end: str | None) -> str | None:
    """Same contract as web/routes/backtest.py's helper of the same name
    (DeepR 2026-08-08 [DÜŞÜK] — this route had no equivalent, so a bad date
    fell through to the generic `except Exception: 500` below and leaked the
    raw ValueError text to the client). Blank values are fine (blank = full
    cache); only rejects when both are given and start > end, or either
    fails to parse as YYYY-MM-DD.
    """
    s, e = (start or "").strip(), (end or "").strip()
    if not s and not e:
        return None
    try:
        sd = date.fromisoformat(s) if s else None
        ed = date.fromisoformat(e) if e else None
    except ValueError:
        return "Dates must be in YYYY-MM-DD format."
    if sd and ed and sd > ed:
        return "End date cannot be before the start date."
    return None


def _template_ctx(request, **extra):
    """Standard context that satisfies base.html's topbar."""

    ctx = {
        "active": "data",
        "page_title": "Instrument Catalog",
        "market": get_market_info(),
    }
    ctx.update(extra)
    return ctx


@router.get("/range")
async def coverage(
    source: str = Query(...),
    symbol: str = Query(default=""),
    category: str = Query(default="linear"),
    interval: str = Query(default=""),
    ticker: str = Query(default=""),
    granularity: str = Query(default=""),
    instrument_id: str = Query(default=""),
):
    """Cached coverage of one bar series — the date pickers' MAX button.

    Returns {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}; 404 when nothing is
    cached/ingested for the requested series (the button shows "no data").
    """
    from data import coverage_range

    rng = await asyncio.to_thread(
        coverage_range,
        source,
        symbol=symbol.strip().upper(),
        category=category,
        interval=interval,
        ticker=ticker.strip(),
        granularity=granularity,
        instrument_id=instrument_id.strip(),
    )
    if rng is None:
        raise HTTPException(404, "no cached data for this series")
    return rng


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    q: str | None = Query(default=None),
    xq: str | None = Query(default=None),
):

    # H93: heavy synchronous catalog scan in a thread — so the event loop is not blocked.
    cat = await asyncio.to_thread(
        list_catalog,
        index_query=q,
        index_limit=50,
        external_query=xq,
        external_limit=50,
    )
    ctx = _template_ctx(
        request,
        catalog=cat,
        bybit_symbols=BYBIT_SYMBOLS,
        bybit_categories=BYBIT_CATEGORIES,
        bybit_intervals=BYBIT_ALL_INTERVALS,
        supported_bybit_codes=set(_BYBIT_MS.keys()),
        index_query=q or "",
        external_query=xq or "",
    )
    return templates.TemplateResponse(request, "data.html", ctx)


@router.post("/refresh/bybit", response_class=HTMLResponse)
async def refresh_bybit(
    request: Request,
    symbol: str = Form(...),
    category: str = Form(...),
    interval: str = Form(...),
):

    if symbol not in BYBIT_SYMBOLS:
        raise HTTPException(400, f"unsupported symbol {symbol!r}")
    if category not in BYBIT_CATEGORIES:
        raise HTTPException(400, f"unsupported category {category!r}")
    try:
        row = await asyncio.to_thread(
            refresh_row, "bybit", symbol=symbol, category=category, interval=interval
        )
    except ValueError as e:
        # e.g. interval not in _BYBIT_MS
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )


@router.post("/refresh/index", response_class=HTMLResponse)
async def refresh_index(
    request: Request,
    ticker: str = Form(...),
    granularity: str = Form("1d"),
    start: str | None = Form(default=None),
    end: str | None = Form(default=None),
):
    from data import _GRAN_BARSPEC

    if granularity not in _GRAN_BARSPEC:
        raise HTTPException(
            400,
            f"unsupported granularity {granularity!r}; "
            f"supported: {list(_GRAN_BARSPEC)}",
        )
    date_err = _invalid_date_range(start, end)
    if date_err:
        raise HTTPException(400, date_err)
    try:
        row = await asyncio.to_thread(
            refresh_row,
            "index",
            ticker=ticker,
            granularity=granularity,
            start=start or None,
            end=end or None,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )


@router.post("/index/discover", response_class=HTMLResponse)
async def index_discover(request: Request, force: bool = Form(default=False)):
    """Rebuild ``_tickers.json`` from ``INDEX_ROOT``. Slow; returns an HTML
    fragment swapped into ``#discover-result`` (not JSON — the form does
    ``hx-swap="innerHTML"``)."""
    from data import index_root_warning

    # Kök hiç yoksa taramaya girme: 404'ün gövdesi de panelin gösterdiği
    # cümlenin AYNISI olsun (DeepR 2026-08-11 [ORTA]). Buton bu durumda zaten
    # disabled; buraya yine de doğrudan istekle gelinebilir.
    if (warn := index_root_warning()) is not None:
        raise HTTPException(404, warn)
    try:
        tickers = await asyncio.to_thread(discover_index_tickers, force=force)
    except FileNotFoundError as e:
        raise HTTPException(404, f"INDEX_ROOT not found: {e}") from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return templates.TemplateResponse(
        request,
        "fragments/data/discover_result.html",
        {"count": len(tickers), "sample": tickers[:5]},
    )


@router.post("/catalog/write", response_class=HTMLResponse)
async def catalog_write(
    request: Request,
    source: str = Form(...),
    symbol: str = Form(default="BTCUSDT"),
    category: str = Form(default="linear"),
    interval: str = Form(default="1"),
    ticker: str = Form(default=""),
    granularity: str = Form(default="1d"),
):
    """Write a pandas cache row → Nautilus ParquetDataCatalog.

    Converts the existing pandas Parquet to Nautilus Bar objects (fixed-point
    prices, int64 nanosecond timestamps) and writes them using
    ``ParquetDataCatalog.write_bars()``. Idempotent — re-writing the same
    range just overwrites.

    See wiki: [[parquet_data_catalog]], [[data_wranglers]], [[backtest_node]].
    """

    kw: dict = {}
    if source == "bybit":
        if symbol not in BYBIT_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol {symbol!r}")
        kw = {"symbol": symbol, "category": category, "interval": interval}
    elif source == "index":
        if not ticker or not ticker.strip():
            raise HTTPException(400, "ticker is required for index source")
        kw = {"ticker": ticker.strip(), "granularity": granularity}
    else:
        raise HTTPException(400, f"unknown source {source!r}")
    try:
        await asyncio.to_thread(write_to_nautilus_catalog, source, **kw)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

    # Re-render the updated row via refresh_row (builds only the target row,
    # avoids a full catalog scan).
    try:
        row = await asyncio.to_thread(refresh_row, source, **kw)
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )
