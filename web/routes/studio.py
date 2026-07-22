"""Unified Strategy Studio — merges the Composer (/strategy) and Backtest
(/backtest) pages into one tabbed page at ``/studio``.

Wiki References
---------------
See: [[strategy_and_actor]], [[backtesting_guide]], [[webapp_module_map]]

The merged Compose + Backtest surface. This module owns only the ``GET /studio``
entry point that renders ``studio.html`` with the UNION of both pages' contexts;
every HTMX action still posts to the existing ``/strategy/*`` and ``/backtest/*``
endpoints (unchanged). The single durable link between the two halves is
``strategy_catalog.json`` keyed by ``spec_id`` — the catalog picker in the
Backtest tab finally makes the Composer→Backtest handoff live (the old
``preferred_spec_id`` deep-link was computed but never rendered).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from composer import BLOCK_CATALOG, load_catalog
from data import BYBIT_ALL_INTERVALS, BYBIT_CATEGORIES, BYBIT_SYMBOLS
from web.shared import session_id

router = APIRouter(prefix="/studio")


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    # Sync handler → FastAPI runs it in a threadpool, so the blocking disk I/O
    # (load_catalog + custom-block listing + read_wiki_page + _recent_runs)
    # doesn't stall the event loop.
    import custom_block_store as cbs
    from server import get_market_info, templates

    # Import backtest-side helpers lazily to avoid import-time coupling; these
    # are the same functions the standalone /backtest page uses.
    from web.routes.backtest import (
        _catalog_index_symbols,
        _last_result_get,
        _recent_runs,
        _result_viewmodel,
    )
    from web.routes.strategy import _drafts, _wiki_html_for

    sid = session_id(request)
    catalog = load_catalog()
    default_type = next(iter(BLOCK_CATALOG.keys()))
    wiki_active, wiki_html = _wiki_html_for(default_type)

    # Autonomous mode: bind the studio cockpit to the newest still-running agent
    # run (System B), so switching to OTONOM after a restart/refresh reconnects
    # to a live loop even if it was started elsewhere (the /agent page or API).
    # Mirrors web.routes.agent_backtest.page's reverse-scan of the store.
    active_run_id = None
    try:
        from web.routes.agent_backtest import _AGENT_LOCK, _AGENT_PROGRESS

        with _AGENT_LOCK:
            for rid, st in reversed(_AGENT_PROGRESS.items()):
                if not st.get("done"):
                    active_run_id = rid
                    break
    except Exception:
        active_run_id = None

    # Backtest tab: session-scoped last result (Faz 3).
    slot = _last_result_get(sid)
    last_row = None
    if slot["r"] is not None:
        last_row = _result_viewmodel(
            slot["r"],
            slot["spec_name"],
            slot.get("narrative", ""),
            dict(slot.get("bars_info", {})),
        )

    ctx = {
        "active": "studio",
        "page_title": "Strategy Studio",
        "market": get_market_info(),
        # ── Autonomous cockpit: newest running agent run (System B), or None ──
        "active_run_id": active_run_id,
        # ── Compose tab context ──
        "block_catalog": BLOCK_CATALOG,
        "default_type": default_type,
        "drafts": _drafts(sid),
        "wiki_active": wiki_active,
        "wiki_html": wiki_html,
        "custom_blocks": cbs.list_custom(),
        "options": {
            "entry_logic": "OR",
            "exit_logic": "OR",
            "order_type": "market",
            "limit_offset_bps": 0.0,
            "use_bracket": False,
            "sl_type": "percent",
            "sl_value": 2.0,
            "tp_type": "off",
            "tp_value": 4.0,
            "atr_period": 14,
            "allow_short": False,
            "trade_size_mode": "fixed_usdt",
            "trade_size_percent": 5.0,
            "trade_size_atr_risk": 1.0,
            "trade_size_usdt": 1000.0,
            "trade_size_btc": 0.1,
            "emulate": False,
            "trend_filter": False,
            "trend_interval": "60",
            "trend_ema_period": 50,
        },
        # ── Shared: the catalog. Compose lists newest-first; Backtest picker
        # iterates it too — pass newest-first (compose_body reverses visually
        # via its own include, backtest_body renders the picker in list order).
        "catalog": list(reversed(catalog)),
        # ── Backtest tab context ──
        "last": last_row,
        "preferred_spec_id": request.query_params.get("spec_id", ""),
        "recent_runs": _recent_runs(6),
        "bybit_symbols": _bybit_symbols(),
        "bybit_categories": BYBIT_CATEGORIES,
        "bybit_intervals": BYBIT_ALL_INTERVALS,
        "index_symbols": _catalog_index_symbols(),
    }
    # Preserve strategy.py's manual render + cookie set (drafts session depends
    # on nautlab_sid; session_id above mints it, we set it if absent).
    html = templates.get_template("studio.html").render(request=request, **ctx)
    resp = HTMLResponse(html)
    if not request.cookies.get("nautlab_sid"):
        resp.set_cookie("nautlab_sid", sid, httponly=True, samesite="lax", max_age=3600)
    return resp


def _bybit_symbols():
    """Symbol list for the Backtest instrument picker — catalog symbols if
    present, else the static BYBIT_SYMBOLS fallback (same as backtest.page)."""
    from data import list_catalog_bybit_symbols

    return list_catalog_bybit_symbols() or [
        {"symbol": s, "category": "linear"} for s in BYBIT_SYMBOLS
    ]
