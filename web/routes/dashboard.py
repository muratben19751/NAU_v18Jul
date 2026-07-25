"""Dashboard: autonomous agent loop overview.

Wiki References
---------------
_(app-specific — outside wiki scope)_

App UI; outside wiki scope.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from composer import load_catalog
from state import get_state

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from server import get_market_info, templates

    state = get_state()
    catalog = load_catalog()
    _, _, running, status = state.snapshot()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "page_title": "Dashboard",
            "market": get_market_info(),
            "running": running,
            "status": status,
            "iter_count": len(state.iterations),
            "has_catalog": bool(catalog),
        },
    )
