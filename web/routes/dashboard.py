"""Dashboard: uygulamanın giriş sayfası — durum özeti ve yüzeylere kapı.

Eskiden legacy Loop koşucusunun kokpitiydi (iterasyon listesi, "Best So Far",
equity grafiği — hepsi `state.AppState`'ten). Loop 2026-08-17'de emekliye
ayrıldı; sayfa KALDI çünkü burası `/`. Artık durum tutmuyor: kataloğun ve veri
kaynağının o anki hâlini gösterir, gerisini asıl yüzeylere bırakır.

Wiki References
---------------
_(app-specific — outside wiki scope)_
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from composer import load_catalog
from web.templating import get_market_info, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    catalog = load_catalog()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "page_title": "Dashboard",
            "market": get_market_info(),
            "catalog_count": len(catalog or []),
        },
    )
