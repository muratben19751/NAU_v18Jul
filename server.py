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
A second DeepR pass the same day added `_login_rate_limited` (per-IP failed-
attempt counter, 5/60s, in-memory) since `/login` had no brute-force guard
and failures were never logged; marked the `nau_auth` cookie `secure=True`
(it previously lacked the flag); and added `POST /logout` + the
`auth_enabled()` template global (there was no way to end a session short of
rotating `NAU_ACCESS_TOKEN` itself); and `_limit_request_body` (app-wide ASGI
body-size cap, `NAU_MAX_REQUEST_BODY_BYTES`, default 2 MiB) since no route
bounded request size at all. The whole gate (`_require_auth` HTMX-vs-browser
redirect split, `_is_authenticated`/`_auth_cookie_value`, login/logout,
rate-limit, secure cookie) previously had zero test coverage — see
`tests/test_require_auth_middleware.py`, `tests/test_login_rate_limit.py`,
`tests/test_auth_cookie_logout.py`.
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
import logging as _logging
import os as _os
import threading as _threading
import time as _time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from data import load_bybit_bars

_log = _logging.getLogger(__name__)

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


# /login brute-force guard. Single-operator, single-process app — an
# in-memory per-IP failure count (no external limiter dependency) is enough:
# DeepR 2026-08-08 [YÜKSEK] flagged that a Cloudflare-tunnelled, internet-
# facing /login had NO attempt limit and failures were never logged, so a
# script could grind the shared secret unnoticed.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_S = 60.0
_login_failures: dict[str, list[float]] = {}
_login_lock = _threading.Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_rate_limited(ip: str) -> bool:
    """True once `ip` has `_LOGIN_MAX_ATTEMPTS`+ failures in the trailing window."""
    now = _time.monotonic()
    with _login_lock:
        recent = [t for t in _login_failures.get(ip, ()) if now - t < _LOGIN_WINDOW_S]
        _login_failures[ip] = recent
        return len(recent) >= _LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    now = _time.monotonic()
    with _login_lock:
        # Opportunistic prune so long-idle attackers/IPs do not leak memory —
        # cheap at the traffic this gate is meant for (one operator, occasional
        # scans), and only runs on the failure path.
        stale = [
            k
            for k, times in _login_failures.items()
            if not any(now - t < _LOGIN_WINDOW_S for t in times)
        ]
        for k in stale:
            del _login_failures[k]
        _login_failures.setdefault(ip, []).append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


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
# Sidebar only offers a Logout link when the access gate is actually on —
# NAU_ACCESS_TOKEN unset (local dev default) means auth is a no-op, and a
# logout link there would just redirect to a login screen with no gate.
templates.env.globals["auth_enabled"] = lambda: bool(_ACCESS_TOKEN)


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
async def login_submit(request: Request, token: str = Form(...)):
    ip = _client_ip(request)
    if _login_rate_limited(ip):
        _log.warning("login rate-limited: %s", ip)
        return HTMLResponse(
            _LOGIN_PAGE.format(
                error='<p class="err">Çok fazla başarısız deneme. Bir dakika sonra tekrar deneyin.</p>'
            ),
            status_code=429,
        )
    if _ACCESS_TOKEN and _hmac.compare_digest(token, _ACCESS_TOKEN):
        _clear_login_failures(ip)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            _AUTH_COOKIE,
            _auth_cookie_value(_ACCESS_TOKEN),
            httponly=True,
            samesite="lax",
            secure=True,
            max_age=60 * 60 * 24 * 30,
        )
        return resp
    _record_login_failure(ip)
    _log.warning("failed login attempt: %s", ip)
    return HTMLResponse(
        _LOGIN_PAGE.format(error='<p class="err">Yanlış erişim kodu.</p>'),
        status_code=401,
    )


@app.post("/logout")
async def logout_submit():
    """Clear this browser's auth cookie (DeepR 2026-08-08 [ORTA] — there was
    no way to end a session short of rotating NAU_ACCESS_TOKEN itself).

    The cookie is a static sha256(NAU_ACCESS_TOKEN), not a per-session id —
    there is nothing to revoke server-side, only the browser's copy to
    delete. Logging out here does not invalidate any OTHER browser still
    holding the same cookie; only rotating the token does that.
    """
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(_AUTH_COOKIE)
    return resp


# DeepR 2026-08-08 [ORTA]: no route in the app bounded request body size —
# web/routes/reports.py's post_layout() parsed `await request.json()` on
# whatever a client sent, and nothing else was any different. Nothing here
# legitimately needs more than a few KB (no file-upload endpoint exists; the
# largest bodies are strategy/report JSON and short chat text). A single
# ASGI-level cap covers every current and future route without having to
# remember to add a check to each one.
_MAX_REQUEST_BODY_BYTES = int(
    _os.environ.get("NAU_MAX_REQUEST_BODY_BYTES", str(2 * 1024 * 1024))
)


@app.middleware("http")
async def _limit_request_body(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        if declared is not None and declared > _MAX_REQUEST_BODY_BYTES:
            return Response(status_code=413, content="Request body too large")
    # Content-Length can be absent (chunked transfer) or simply wrong — a
    # client is free to declare anything and then stream more. Wrapping
    # receive() enforces the cap on bytes actually read, not just the
    # declared header. Raising mid-stream stops a runaway body from being
    # buffered further (the actual DoS this guards against); the `exceeded`
    # flag is checked again below the route ran, since several routes wrap
    # `await request.json()` in a broad `except Exception` and would
    # otherwise silently turn a 413 into their own generic 400.
    state = {"received": 0, "exceeded": False}
    original_receive = request.receive

    async def _capped_receive():
        message = await original_receive()
        if message["type"] == "http.request":
            state["received"] += len(message.get("body") or b"")
            if state["received"] > _MAX_REQUEST_BODY_BYTES:
                state["exceeded"] = True
                raise HTTPException(413, "Request body too large")
        return message

    request._receive = _capped_receive
    response = await call_next(request)
    if state["exceeded"]:
        return Response(status_code=413, content="Request body too large")
    return response


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
