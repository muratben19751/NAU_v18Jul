"""Instrument catalog — Nautilus-idiomatic /data screen.

Endpoints:
    GET  /data                         Full catalog page (Bybit + US-Index).
    POST /data/refresh/bybit           Fetch a (symbol, category, interval) cell.
    POST /data/refresh/index           Fetch a (ticker, granularity) row.
    POST /data/index/discover          Rebuild the US-index ticker registry.
    POST /data/catalog/write           Write pandas cache → Nautilus ParquetDataCatalog.
    GET  /data/fragments/row/{source}/{key}   Return one row for HTMX polling.

All fetch endpoints return a rendered fragment (single row / cell) so HTMX
can hot-swap the DOM in place without re-fetching the whole page.

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]],
[[index_backtest_via_equity_proxy]], [[precision_modes]]

Ekran wiki-flagged pitfall'lara (size_precision=0 Equity trap; BarType DSL
origin ayrımı; book_type ↔ granularity uyuşmazlığı) rozet olarak yer verir.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

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

router = APIRouter(prefix="/data")


def _template_ctx(request, **extra):
    """Standard context that satisfies base.html's topbar."""
    from server import get_market_info

    ctx = {
        "active": "data",
        "page_title": "Instrument Catalog",
        "market": get_market_info(),
    }
    ctx.update(extra)
    return ctx


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    q: str | None = Query(default=None),
    xq: str | None = Query(default=None),
):
    from server import templates

    # H93: ağır senkron katalog taraması thread'de — event loop bloklanmasın.
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
    from server import templates

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
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
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
    from server import templates

    if granularity not in ("1d", "1m"):
        raise HTTPException(400, f"unsupported granularity {granularity!r}")
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
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )


@router.post("/index/discover", response_class=JSONResponse)
async def index_discover(force: bool = Form(default=False)):
    """Rebuild ``_tickers.json`` from ``INDEX_ROOT``. Slow; returns a summary."""
    try:
        tickers = await asyncio.to_thread(discover_index_tickers, force=force)
    except FileNotFoundError as e:
        raise HTTPException(404, f"INDEX_ROOT not found: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"count": len(tickers), "sample": tickers[:5]}


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
    from server import templates

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
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    # Re-render the updated row via refresh_row (builds only the target row,
    # avoids a full catalog scan).
    try:
        row = await asyncio.to_thread(refresh_row, source, **kw)
    except Exception as e:
        raise HTTPException(500, str(e))
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )


@router.get("/fragments/row/{source}/{key:path}", response_class=HTMLResponse)
async def row_fragment(request: Request, source: str, key: str):
    from server import templates

    if source == "bybit":
        cat = await asyncio.to_thread(list_catalog, index_limit=1)
        row = next((r for r in cat["bybit"] if r["key"] == key), None)
    elif source == "index":
        # For big catalogs, avoid scanning the whole list — re-query by ticker.
        cat = await asyncio.to_thread(
            list_catalog, index_query=key, index_limit=None
        )
        idx_rows = [r for r in cat["index"] if r["key"] == key]
        row = idx_rows[0] if idx_rows else None
    else:
        raise HTTPException(404, f"unknown source {source!r}")
    if row is None:
        raise HTTPException(404, f"row not found for {source}/{key}")
    return templates.TemplateResponse(
        request,
        "fragments/data/instrument_row.html",
        {"row": row},
    )
