"""HTMX polling fragments for live UI updates.

Wiki References
---------------
_(app-spesifik — wiki scope dışı)_

Sunum katmanı.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from state import get_state
from web.viewmodels import best_card, iteration_row

router = APIRouter(prefix="/fragments")


@router.get("/iterations", response_class=HTMLResponse)
async def iterations(request: Request):
    from server import templates

    state = get_state()
    iters, _, _, _ = state.snapshot()
    rows = [iteration_row(r) for r in reversed(iters)]
    return templates.TemplateResponse(
        request,
        "fragments/iterations_table.html",
        {"rows": rows},
    )


@router.get("/best", response_class=HTMLResponse)
async def best(request: Request):
    from server import templates

    state = get_state()
    _, b, _, _ = state.snapshot()
    return templates.TemplateResponse(
        request,
        "fragments/best_card.html",
        {"best": best_card(b)},
    )


@router.get("/loop_status", response_class=HTMLResponse)
async def loop_status(request: Request):
    from server import templates

    state = get_state()
    _, _, running, status = state.snapshot()
    return templates.TemplateResponse(
        request,
        "fragments/loop_status.html",
        {"running": running, "status": status, "iter_count": len(state.iterations)},
    )


@router.get("/equity.json")
async def equity_json():
    state = get_state()
    _, b, _, _ = state.snapshot()
    if b is None or not b.equity_curve:
        return JSONResponse({"points": []})
    return JSONResponse({"points": [float(x) for x in b.equity_curve]})
