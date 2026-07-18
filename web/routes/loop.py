"""Autonomous loop start/stop endpoints.

Wiki References
---------------
Bkz: [[crash_only_design]]

Loop güvenli olarak durdurulabilir — [[crash_only_design]] fail-fast prensibi.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from loop_runner import run_loop
from state import get_state

router = APIRouter(prefix="/loop")


@router.post("/start", response_class=HTMLResponse)
async def start(request: Request, mode: str = Form("agent")):
    from server import get_bars, templates

    state = get_state()

    # M29: state.running yalnız run_loop THREAD'i içinde True yapılıyordu; check
    # ile thread'in flag'i set etmesi arasındaki pencerede ikinci bir /loop/start
    # de running==False görüp İKİNCİ bir loop thread'i başlatabiliyordu. Kilit
    # altında SENKRON işaretle (running'i hemen True yap) — çift-başlatma yok.
    started = False
    with state.lock:
        if not state.running:
            state.running = True  # senkron guard — thread devralana dek
            state.stop_requested = False
            started = True
    if started:
        t = threading.Thread(
            target=run_loop, args=(state, get_bars(), mode), daemon=True
        )
        t.start()
        state.thread_started = True

    _, _, running, status = state.snapshot()
    return templates.TemplateResponse(
        request,
        "fragments/loop_status.html",
        {"running": running, "status": status, "iter_count": len(state.iterations)},
    )


@router.post("/stop", response_class=HTMLResponse)
async def stop(request: Request):
    from server import templates

    state = get_state()
    if state.running:
        state.stop_requested = True

    _, _, running, status = state.snapshot()
    return templates.TemplateResponse(
        request,
        "fragments/loop_status.html",
        {"running": running, "status": status, "iter_count": len(state.iterations)},
    )
