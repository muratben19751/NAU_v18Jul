"""FastAPI entrypoint for Nautilus Lab web UI.

Run (dev — auto-reload):
    uvicorn server:app --host 127.0.0.1 --port 8000 --reload \
        --reload-exclude "$PWD/nautilus_wiki" --reload-exclude "$PWD/.claude" \
        --reload-exclude "$PWD/tests"

    `--reload` izlenen ağaçtaki her `*.py` değişiminde sunucuyu yeniden başlatır.
    Strateji ÜRETİMİ (`POST /backtest/describe`) ~15-20 sn süren bir worker
    thread'de çalışır ve ilerleme durumu BELLEKTE tutulur (`_GEN_PROGRESS`);
    bu sırada izlenen bir `.py` değişirse sunucu yeniden başlar, worker + durum
    uçar ve sağ-üstteki üretim paneli aniden kaybolur. `--reload-exclude`'a
    MUTLAK (absolute) DİZİN YOLU verilmeli: uvicorn dışlamayı yalnızca yol
    var olan bir dizinse `path.parents` ile recursive uygular ve watchfiles
    filtreye MUTLAK yol geçirir — göreli `nautilus_wiki` ya da `nautilus_wiki/*`
    glob'u eşleşmez (deneysel olarak doğrulandı). Bu sayede üretimle alakasız
    ağaçlar (wiki, skill'ler, testler) watch dışına alınır ve kesintiler azalır.
    Kesinti yine olursa panel artık sessizce kaybolmaz — "üretim yarıda kesildi,
    sunucu yeniden başladı" mesajı gösterir ([[webapp_module_map]]).

    Prod / kesintisiz üretim için `--reload` OLMADAN çalıştırın.

Wiki References
---------------
See: [[nautilus_kernel]], [[event_driven_architecture]], [[nau_deepr_toplu_sertlestirme_2026_08]]

Loose analog of Nautilus [[nautilus_kernel]] for the WEB app: bootstraps subsystems in `lifespan()`, then routers dispatch requests. Same "compose, then run" shape.

`_require_auth` (single-operator shared-secret auth via `NAU_ACCESS_TOKEN`) and
the `nl2br` Jinja filter (XSS-safe chat-bubble rendering) were added in the
2026-08-08 DeepR hardening pass — see [[nau_deepr_toplu_sertlestirme_2026_08]].
"""

from __future__ import annotations

import sys as _sys

# Windows consoles default stdout/stderr to cp1252, which crashes on the
# Turkish text and arrow/·/… glyphs used throughout the app's progress logs
# (UnicodeEncodeError: 'charmap' codec can't encode ...). Force UTF-8 so every
# print()/log across the process (server + backtest worker threads) is safe.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import hashlib as _hashlib
import hmac as _hmac
import os as _os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from data import load_bybit_bars

# Optional single-operator access gate. If NAU_ACCESS_TOKEN is unset (the
# default for local dev), auth is a no-op and behavior is unchanged. If set,
# every request except /login and /static/* must carry a cookie derived from
# the token (see _require_auth below).
_ACCESS_TOKEN = _os.environ.get("NAU_ACCESS_TOKEN", "").strip()
_AUTH_COOKIE = "nau_auth"


def _auth_cookie_value(token: str) -> str:
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_authenticated(request: Request) -> bool:
    if not _ACCESS_TOKEN:
        return True
    got = request.cookies.get(_AUTH_COOKIE, "")
    return _hmac.compare_digest(got, _auth_cookie_value(_ACCESS_TOKEN))


# Default instrument shown in the topbar.
_DEFAULT_SYMBOL = "BTCUSDT"
_DEFAULT_CATEGORY = "linear"
_DEFAULT_INTERVAL = "1"  # 1-minute bars

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR = BASE_DIR / "web" / "static"


def _static_version() -> str:
    """Cache-busting hash based on chart.js + app.css + app.js content."""
    try:
        h = _hashlib.md5()
        for name in ("chart.js", "app.css", "app.js"):
            p = BASE_DIR / "web" / "static" / name
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()[:8]
    except Exception:
        return "0"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["static_version"] = _static_version()


def _loop_running() -> bool:
    """Live loop status for the sidebar Engine card + Dashboard nav dot."""
    try:
        from state import get_state

        _, _, running, _ = get_state().snapshot()
        return bool(running)
    except Exception:
        return False


templates.env.globals["loop_running"] = _loop_running


def _engine_model_label() -> str:
    """Sidebar ENGINE card's model line. Was a hardcoded string ('Claude Fable 5')
    regardless of which model AUTO/SIMPLE/PRO actually ran with — this resolves
    the same way the AUTO cockpit's own model slot does (agent.model_label),
    so switching models (or the credit-exhaustion fallback kicking in) is
    reflected sitewide instead of only where llm_badge() happened to be wired."""
    try:
        from agent import model_label

        return model_label()
    except Exception:
        return "Claude"


templates.env.globals["engine_model_label"] = _engine_model_label


def _first_studio_strategy_id() -> str | None:
    """Nav link target for 'Strategy Builder' — the most recently updated
    strategy, so a fresh install (no seed_studio.py run) doesn't 404 on a
    hardcoded demo id."""
    try:
        from strategy_studio.store import StrategyStore

        meta = StrategyStore().list_meta()
        return meta[0].strategy_id if meta else None
    except Exception:
        return None


templates.env.globals["first_studio_strategy_id"] = _first_studio_strategy_id


def _datetimefmt(unix_ts: int) -> str:
    from datetime import datetime

    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=UTC)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(unix_ts)


templates.env.filters["datetimefmt"] = _datetimefmt


def _nl2br(value) -> Markup:
    return Markup(str(escape(value)).replace("\n", "<br>"))


templates.env.filters["nl2br"] = _nl2br

_context: dict = {"bars": None, "market": None}


def get_bars():
    return _context["bars"]


def get_market_info() -> dict:
    return _context["market"] or {
        "symbol": _DEFAULT_SYMBOL,
        "venue": _DEFAULT_CATEGORY.upper(),
        "bars": 0,
        "start": "—",
        "end": "—",
        "last_price": 0.0,
        "spark": [],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    # Fail fast if the installed nautilus_trader wheel drifted from the pin.
    from constants import assert_nautilus_version

    print(f"[startup] nautilus_trader {assert_nautilus_version()}", flush=True)

    loop = asyncio.get_event_loop()
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    # Run blocking I/O (Bybit HTTP + parquet) in a thread so the event loop is
    # not blocked during startup.
    # M124: On an offline/Bybit-unreachable start, a ConnectionError from
    # load_bybit_bars → lifespan → FastAPI startup would take the whole thing
    # down (even with a full cache on disk, the tail-fetch blows up with a
    # connection error). Swallow the exception, continue with an empty df —
    # let the server come up, and let the loop runner run once data arrives.
    try:
        bars = await loop.run_in_executor(
            None,
            lambda: load_bybit_bars(
                symbol=_DEFAULT_SYMBOL,
                interval=_DEFAULT_INTERVAL,
                category=_DEFAULT_CATEGORY,
                start=start,
                end=end,
            ),
        )
    except Exception as _e:
        import pandas as _pd

        bars = _pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        _startup_err = _e
    else:
        _startup_err = None
    _context["bars"] = bars
    if bars.empty:
        import warnings

        _why = (
            f"startup fetch error ({type(_startup_err).__name__})"
            if _startup_err is not None
            else "Bybit unreachable or cache empty"
        )
        warnings.warn(
            f"Startup: could not load bars for {_DEFAULT_SYMBOL}/{_DEFAULT_INTERVAL} — "
            f"{_why}. Server is starting anyway; the loop runner will report "
            "errors until data arrives.",
            RuntimeWarning,
            stacklevel=2,
        )
    _context["market"] = {
        "symbol": f"{_DEFAULT_SYMBOL[:3]}/{_DEFAULT_SYMBOL[3:]} · BYBIT",
        "venue": _DEFAULT_CATEGORY.upper(),
        "bars": len(bars),
        "start": str(bars.index[0].date()) if not bars.empty else "—",
        "end": str(bars.index[-1].date()) if not bars.empty else "—",
        "last_price": float(bars["close"].iloc[-1]) if not bars.empty else 0.0,
        # Topbar sparkline: last ~48 closes (indicative)
        "spark": [round(float(x), 2) for x in bars["close"].iloc[-48:]]
        if not bars.empty
        else [],
    }
    yield


app = FastAPI(title="Nautilus Lab", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Nautilus Lab — Giriş</title>
<style>body{{background:#0b0f14;color:#e5e7eb;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
form{{background:#111827;padding:2rem;border-radius:8px;min-width:280px}}
input{{width:100%;padding:.6rem;margin:.5rem 0 1rem;border-radius:4px;border:1px solid #374151;
background:#1f2937;color:#e5e7eb;box-sizing:border-box}}
button{{width:100%;padding:.6rem;border-radius:4px;border:none;background:#2563eb;color:#fff;
cursor:pointer}}
p.err{{color:#f87171}}</style></head><body>
<form method="post" action="/login">
<h2>Nautilus Lab</h2>
{error}
<input type="password" name="token" placeholder="Erişim kodu" autofocus>
<button type="submit">Giriş</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_form():
    return HTMLResponse(_LOGIN_PAGE.format(error=""))


@app.post("/login")
async def login_submit(token: str = Form(...)):
    if _ACCESS_TOKEN and _hmac.compare_digest(token, _ACCESS_TOKEN):
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            _AUTH_COOKIE,
            _auth_cookie_value(_ACCESS_TOKEN),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return resp
    return HTMLResponse(
        _LOGIN_PAGE.format(error='<p class="err">Yanlış erişim kodu.</p>'),
        status_code=401,
    )


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    if not _ACCESS_TOKEN or path == "/login" or path.startswith("/static/"):
        return await call_next(request)
    if _is_authenticated(request):
        return await call_next(request)
    if request.headers.get("HX-Request") == "true":
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse(url="/login", status_code=303)


from web.routes import (
    agent_backtest as agent_route,
)
from web.routes import (  # noqa: E402  (late import: routers import from server, circular)
    backtest,
    dashboard,
    fragments,
    lab,
    loop,
    reports,
    strategy,
    studio,
    wiki,
)
from web.routes import (
    chart as chart_route,
)
from web.routes import (
    data as data_route,
)
from web.routes import (
    robustness as robustness_route,
)
from web.routes import (
    sessions as sessions_route,
)
from web.routes import (
    strategy_studio as strategy_studio_route,
)
from web.routes import (
    tearsheet as tearsheet_route,
)
from web.routes import (
    tokens as tokens_route,
)

app.include_router(dashboard.router)
app.include_router(studio.router)
# Strategy Studio builder: /studio/{strategy_id} — the bare /studio above is the
# Composer+Backtest page, so the two coexist without shadowing each other.
app.include_router(strategy_studio_route.router)
app.include_router(strategy.router)
app.include_router(backtest.router)
app.include_router(loop.router)
app.include_router(fragments.router)
app.include_router(wiki.router)
app.include_router(data_route.router)
app.include_router(lab.router)
app.include_router(chart_route.router)
app.include_router(robustness_route.router)
app.include_router(reports.router)
app.include_router(agent_route.router)
app.include_router(sessions_route.router)
app.include_router(tokens_route.router)
# GET /tearsheet — the overlay every backtest listing links to (read-only).
app.include_router(tearsheet_route.router)
