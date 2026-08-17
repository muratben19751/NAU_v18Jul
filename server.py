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
See: [[nautilus_kernel]], [[event_driven_architecture]], [[nau_deepr_toplu_sertlestirme_2026_08]], [[nau_deepr_ucuncu_tur_2026_08_09]]

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

`_client_ip` (the rate-limit bucketing key) prefers the `CF-Connecting-IP`
header over `request.client.host`: behind the Cloudflare Tunnel this app
actually runs on (see `serve.py`), `request.client.host` is always the
tunnel's own local peer, so the "per-IP" limiter was really one global
bucket for every real caller (DeepR 2026-08-09 [DÜŞÜK]).
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
from urllib.parse import urlsplit as _urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

# `sandbox` BURADA, uygulamanın giriş noktasında ve AÇIKÇA import ediliyor —
# kullanılan bir ad için değil, import anındaki SÜREÇ-GLOBAL yan etkisi için:
# `mp.set_executable(pythonw.exe)` (PM2_HOME ayarlıyken).
#
# Neden burada: `openrouter_backend`'in LLM çağrıları `multiprocessing.Process()`
# ile çocuk açıyor ve `Process()`in `creationflags` kolu YOK; pm2 gibi konsolsuz
# bir ebeveyn altında konsol-alt sistemli bir çocuk donuyor (bir kez yaşandı,
# py-spy ile teşhis edildi: "backtest asılı ama logda satır yok"). Tek koruma
# yukarıdaki set_executable.
#
# Bu satırdan önce koruma yalnız TESADÜFEN geliyordu:
# server → web/routes/loop.py → loop_runner.py → sandbox. `loop_runner` legacy
# ve silinmeye aday; zinciri kıran biri korumayı sildiğini bilemezdi ve hiçbir
# çalışma-zamanı testi yakalayamazdı (PM2_HOME pytest'te tanımsız, arıza CI'da
# hiç üremiyor). Sahipsiz bir koruma, ilgisiz bir temizlikte kaybolur.
import sandbox as _sandbox  # noqa: F401  (import-time side effect, not a name)
from data import load_bybit_bars
from web import templating as _templating
from web.templating import (  # noqa: F401  (geriye dönük yeniden dışa verim)
    BASE_DIR,
    DEFAULT_CATEGORY,
    DEFAULT_INTERVAL,
    DEFAULT_SYMBOL,
    STATIC_DIR,
    TEMPLATES_DIR,
    get_bars,
    get_market_info,
    set_market_context,
    static_version,
    templates,
)

_log = _logging.getLogger(__name__)

# Optional single-operator access gate. If NAU_ACCESS_TOKEN is unset (the
# default for local dev), auth is a no-op and behavior is unchanged. If set,
# every request except /login and /static/* must carry a cookie derived from
# the token (see _require_auth below).
#
# Dosya yedeği bilinçli: bu uygulama pm2 altında koşuyor ve pm2 daemon'u kendi
# ortamını süreç başlatıldığı andan taşıyor. Bir ortam değişkenini kalıcı
# yapmak, daemon'u (yanındaki onlarca uygulamayla birlikte) yeniden başlatmayı
# gerektiriyordu; 2026-08-10'da tam bu yüzden token aylarca boş kaldı ve
# cloudflared tüneli kapısız açık durdu. Aynı desen API anahtarı için zaten
# var (`~/.nautilus_proxy_key`, llm_dispatch), tutarlı olan onu izlemek.
# Öncelik env'de: bir dosyanın sessizce devralmasını istemiyoruz.
_ACCESS_TOKEN_FILE = Path.home() / ".nau_access_token"


def _read_access_token(env: dict | None = None) -> str:
    """Erişim kapısının sırrı: önce ortam değişkeni, sonra home'daki dosya.

    Dosya yedeği YALNIZ dağıtımda (PM2_HOME set) okunur. Kapsamı daraltmak
    zorunlu: dosya her yerde okunsaydı, geliştirme makinesinde token oluşturmak
    tüm route testlerini bir anda 303'e çevirirdi (ölçüldü: 211 test). PM2_HOME,
    `_warn_if_unauthenticated_and_deployed` ve sandbox.py'nin zaten kullandığı
    "gerçekten dağıtıldık" işareti — üçüncü bir işaret icat etmiyoruz.

    Yerel dev/test'te kapı yine env ile açılabilir; sessizce açılmaz.
    """
    env = env if env is not None else _os.environ
    token = env.get("NAU_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if not env.get("PM2_HOME"):
        return ""
    try:
        return _ACCESS_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_ACCESS_TOKEN = _read_access_token()
_AUTH_COOKIE = "nau_auth"


def _warn_if_unauthenticated_and_deployed(token: str, env: dict) -> bool:
    """DeepR 2026-08-09 [ORTA]: an unset token was a SILENT no-op even when
    the app is actually deployed (serve.py under pm2, tunnelled to the
    internet per its own docstring) — an operator who forgot to export the
    token got a fully unauthenticated internet-facing app with no signal
    anywhere. PM2_HOME is the same "are we deployed, not just a local
    dev/test run" marker sandbox.py already uses to decide pythonw spawning;
    local dev (PM2_HOME unset) stays silent, matching the module comment
    above. Returns whether it warned (for tests)."""
    if token or not env.get("PM2_HOME"):
        return False
    _log.warning(
        "NAU_ACCESS_TOKEN is unset while running under pm2 (PM2_HOME is set) "
        "— this usually means the app is tunnelled to the internet with NO "
        "authentication. Set NAU_ACCESS_TOKEN to enable the login gate."
    )
    return True


_warn_if_unauthenticated_and_deployed(_ACCESS_TOKEN, _os.environ)


def _deployed_without_a_token(env: dict | None = None) -> bool:
    """Dağıtımdayız ve kapının sırrı yok — istek servis edilmemeli.

    Uyarı 2026-08-09'da eklendi ve YETMEDİ: uyarı log'a düşerken uygulama
    tünelden kapısız servis etmeye devam ediyordu, yani asıl davranış hiç
    değişmedi. Bir güvenlik kontrolünün "uyar ama yine de yap" hâli, kararı
    log'u okuyacak birinin var olduğu varsayımına bağlar; kimse okumazsa
    koruma yoktur.

    Fail-closed ama SÜRECİ ÖLDÜRMEDEN: pm2 altında import zamanı bir
    `sys.exit` yeniden başlatma döngüsü yaratır ve tünel 502 döndürür — ne
    olduğunu kimse anlamaz. Bunun yerine her istek, ne yapılacağını yazan bir
    503 alır: süreç ayakta, sebep tarayıcıda.

    Kaçış kapısı bilinçli: pm2'yi tünelsiz/yerel kullanan bir kurulum
    `NAU_ALLOW_NO_AUTH=1` diyebilir. Kapıyı açmak artık bir CÜMLE kurmayı
    gerektiriyor; unutmakla aynı şey değil.
    """
    env = env if env is not None else _os.environ
    if _ACCESS_TOKEN or not env.get("PM2_HOME"):
        return False
    return env.get("NAU_ALLOW_NO_AUTH", "").strip().lower() not in ("1", "true", "yes")


_NO_TOKEN_BODY = (
    "503 — bu kurulum kimlik doğrulamasız servis etmeyi reddediyor.\n\n"
    "NAU_ACCESS_TOKEN boş ve uygulama pm2 altında koşuyor (PM2_HOME set), "
    "yani büyük olasılıkla cloudflared tüneliyle internete açık.\n\n"
    "Düzeltmek için ikisinden biri:\n"
    "  1) Sırrı ver:  ~/.nau_access_token dosyasına yaz (ya da NAU_ACCESS_TOKEN "
    "ortam değişkenini set et) ve uygulamayı yeniden başlat.\n"
    "  2) Bilerek kapısız koşuyorsan:  NAU_ALLOW_NO_AUTH=1\n"
)


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
    """Rate-limit bucketing key.

    Prefers CF-Connecting-IP over request.client.host: this app is only
    reachable through a Cloudflare Tunnel (see serve.py's docstring), so
    request.client.host is always the tunnel's own local peer, not the
    real caller -- every request collapses into one bucket and the
    "per-IP" limiter degrades to a single global one (DeepR 2026-08-09
    [DÜŞÜK]). CF-Connecting-IP is set by Cloudflare's edge from the actual
    TLS connection and is not attacker-suppliable as long as the origin
    has no path that bypasses the tunnel. Falls back to
    request.client.host for local/direct-uvicorn runs, where there is no
    Cloudflare edge to set the header at all.
    """
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
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
# Jinja ortamı, statik kökler ve piyasa bağlamı `web/templating.py`'ye taşındı:
# route'lar onları buradan alıyordu ve `server` da route'ları import ettiği için
# ortaya çift yönlü bir bağımlılık çıkıyordu (DeepR 2026-08-11 [ORTA], 54 adet
# fonksiyon-içi `from server import ...`). Adlar burada yeniden dışa veriliyor;
# `from server import templates` yazan eski kod ve testler çalışmaya devam eder.
_DEFAULT_SYMBOL = DEFAULT_SYMBOL
_DEFAULT_CATEGORY = DEFAULT_CATEGORY
_DEFAULT_INTERVAL = DEFAULT_INTERVAL
_context = _templating._context


def _static_version() -> str:
    # İnce sarmalayıcı, yalın takma ad DEĞİL: eski sözleşmede `_static_version`
    # yolu çağrı anında `server.BASE_DIR`'den çözerdi; testler bu global'i
    # monkeypatch'leyerek sahte bir statik ağacı hash'letir. Takma ad
    # (`_static_version = static_version`) fonksiyonu web.templating'in
    # namespace'inde koşturur ve bu dikişi görünmez kılar.
    return static_version(BASE_DIR)


# Sidebar only offers a Logout link when the access gate is actually on —
# NAU_ACCESS_TOKEN unset (local dev default) means auth is a no-op, and a
# logout link there would just redirect to a login screen with no gate.
#
# Bu global burada kayıtlı, `web/templating.py`'de değil: erişim kapısı
# `server`'ın sorumluluğu ve yaprak modülün ondan haberi olmamalı.
templates.env.globals["auth_enabled"] = lambda: bool(_ACCESS_TOKEN)


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
    # let the server come up; veri geldiğinde koşular normale döner.
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
    set_market_context(bars=bars)
    if bars.empty:
        import warnings

        _why = (
            f"startup fetch error ({type(_startup_err).__name__})"
            if _startup_err is not None
            else "Bybit unreachable or cache empty"
        )
        warnings.warn(
            f"Startup: could not load bars for {_DEFAULT_SYMBOL}/{_DEFAULT_INTERVAL} — "
            f"{_why}. Server is starting anyway; backtest yüzeyleri veri "
            "gelene kadar hata bildirecek.",
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
            # DeepR 2026-08-11 [ORTA]: "lax" → "strict". Lax, ÜST-SEVİYE bir
            # çapraz-site GEZİNMESİNDE çerezi yine gönderir; bu uygulamada
            # yazma yollarının bir kısmı düz form POST'u olabildiği ve yanıtlar
            # HTML döndüğü için o kapı açık kalıyordu. Strict'te çerez yalnız
            # aynı-site isteklerde gider. Bedeli tek-operatörlük bir uygulamada
            # ihmal edilebilir: yer imi ve adres çubuğuna yazılan URL Strict
            # çerezi GÖNDERİR; sadece başka bir sitedeki bağlantıya tıklayarak
            # gelen ilk istek oturumsuz görünür, tazeleme yeter.
            samesite="strict",
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
    # ÖNCE bu: /login ve /static/* dahil hiçbir şey servis edilmiyor. Kapısız
    # bir dağıtımda "giriş sayfası açık kalsın" diye bir istisna, girilecek bir
    # sır olmadığı için yalnızca kapıyı açık tutmanın kibar adı olurdu.
    if _deployed_without_a_token():
        return PlainTextResponse(_NO_TOKEN_BODY, status_code=503)
    if not _ACCESS_TOKEN or path == "/login" or path.startswith("/static/"):
        return await call_next(request)
    if _is_authenticated(request):
        return await call_next(request)
    if request.headers.get("HX-Request") == "true":
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------------------
# CSRF — durum değiştiren isteklerde köken (origin) doğrulaması
#
# DeepR 2026-08-11 [ORTA]: uygulamada hiçbir CSRF savunması yoktu. Tek dolaylı
# engel `nau_auth` çerezinin `samesite="lax"` olmasıydı ve o da işe yaramıyordu:
# NAU_ACCESS_TOKEN boşken (uzun süre öyleydi) çerez hiç gerekmiyor, dolayısıyla
# SameSite hiçbir şeyi korumuyor. Operatörün tarayıcısında açılan kötücül bir
# sayfa `POST /agent/run` (para harcatan LLM turu), `/agent/stop/{id}`,
# `/strategy/blocks/save-custom`, `/studio/{id}/deploy` çağırabiliyordu;
# 127.0.0.1'e bağlı olmak bunu ENGELLEMEZ, çünkü isteği atan kurbanın kendi
# tarayıcısı.
#
# NEDEN TOKEN DEĞİL DE ORIGIN: "çerezle eşleşen token + hx-headers" deseni bu
# uygulamada sessizce kırılırdı. Yazma yollarının hepsi htmx değil —
# `web/templates/reports.html`'de ham bir `fetch('/reports/layout', {method:
# 'POST'})` var ve `htmx.ajax("POST", …)` çağrılarının bir kısmı `source`
# elemanı vermiyor; ikisi de `<body hx-headers>`'ı devralmaz. Yani token
# eklemek en az beş canlı yolu, testlerin göremeyeceği biçimde bozardı.
# `Origin` başlığını ise TARAYICI koyar: fetch, XHR, htmx ve düz form POST'u —
# hepsinde, istisnasız. Savunma tam olarak korunması gereken yüzeyle örtüşüyor.
#
# EŞLEŞTİRME NEDEN SADECE HOST ÜZERİNDEN: uygulama cloudflared tünelinin
# arkasında düz http dinliyor, tarayıcı ise https görüyor — şema karşılaştırmak
# her POST'u 403 yapardı. Host karşılaştırması ters-vekil arkasındaki standart
# yaklaşım. Tünel Host'u yeniden yazarsa `NAU_ALLOWED_ORIGINS` ile açıkça
# tanımlanır (reddedilen her istek zaten iki değeri de log'luyor).
# ---------------------------------------------------------------------------
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# /login: kapıdan geçmemiş bir tarayıcının tek yazma yolu; kendi rate-limiter'ı
# var ve "kurbanı saldırganın hesabına girdirmek" tek-sır modelinde anlamsız.
_CSRF_EXEMPT_PATHS = frozenset({"/login"})


def _allowed_origin_hosts(request: Request) -> set[str]:
    """Bu istek için meşru sayılan köken host'ları (hepsi küçük harf)."""
    hosts = {
        h.strip().lower()
        for h in (
            request.headers.get("host"),
            request.headers.get("x-forwarded-host"),
        )
        if h and h.strip()
    }
    for raw in _os.environ.get("NAU_ALLOWED_ORIGINS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        # Hem "https://host" hem çıplak "host" yazımını kabul et.
        parsed = _urlsplit(raw if "//" in raw else "//" + raw)
        if parsed.netloc:
            hosts.add(parsed.netloc.lower())
    return hosts


def _csrf_origin_ok(request: Request) -> bool | None:
    """``True`` aynı köken, ``False`` çapraz köken, ``None`` "tarayıcı değil".

    ``None`` durumu bilinçli: `Origin` DA `Referer` DA yoksa istek bir
    tarayıcıdan gelmiyordur (curl, pytest'in TestClient'ı, operatörün kendi
    script'i). CSRF tanımı gereği bir tarayıcı-şaşırtma saldırısıdır; tarayıcı
    yoksa saldırı da yoktur. Modern tarayıcılar GET/HEAD dışındaki her istekte
    `Origin` gönderir — referrer-policy onu bastırırsa bile `null` olarak
    gönderir, o da eşleşmeyeceği için ``False`` döner. Yani bu boşluk bir
    tarayıcı tarafından ZORLANAMAZ. Yine de sadece tarayıcıdan erişilen bir
    kurulumda kapatılabilsin diye `NAU_CSRF_REQUIRE_ORIGIN=1` bayrağı var.
    """
    allowed = _allowed_origin_hosts(request)
    origin = request.headers.get("origin")
    if origin:
        return _urlsplit(origin).netloc.lower() in allowed
    referer = request.headers.get("referer")
    if referer:
        return _urlsplit(referer).netloc.lower() in allowed
    return None


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):
    if (
        request.method in _CSRF_SAFE_METHODS
        or request.url.path in _CSRF_EXEMPT_PATHS
        or request.url.path.startswith("/static/")
    ):
        return await call_next(request)
    verdict = _csrf_origin_ok(request)
    if verdict is None and _os.environ.get("NAU_CSRF_REQUIRE_ORIGIN") == "1":
        verdict = False
    if verdict is False:
        _log.warning(
            "CSRF: cross-origin %s %s refused (origin=%r referer=%r host=%r)",
            request.method,
            request.url.path,
            request.headers.get("origin"),
            request.headers.get("referer"),
            request.headers.get("host"),
        )
        # Gövde HTML değil: htmx bunu DOM'a basacak olsa bile zararsız kalsın.
        return Response(
            status_code=403,
            content="CSRF: cross-origin request refused",
            media_type="text/plain; charset=utf-8",
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Temel güvenlik başlıkları. `frame-ancestors 'none'` + X-Frame-Options aynı
# bulgunun clickjacking ayağını kapatıyor: başlıksızken kötücül bir sayfa
# uygulamayı görünmez bir iframe'e alıp aynı işlemleri TIKLAMA üzerinden
# tetikleyebiliyordu (origin kontrolü burada yardım etmez — tıklayan gerçekten
# kullanıcının kendisi ve istek gerçekten aynı kökenli).
#
# CSP'nin script-src'i 'unsafe-inline' içeriyor ÇÜNKÜ uygulama satır-içi
# script'lerle dolu (base.html'in toast/tearsheet bloğu, her sayfanın kendi
# <script>'i) ve htmx `allowScriptTags` ile swap edilen parçaların script'ini
# çalıştırıyor. Bunu dürüstçe söylemek gerekir: buradaki CSP satır-içi XSS'i
# durdurmaz — onu #1'deki kaçış katmanı durduruyor. CSP'nin işi KÖKEN KİLİDİ:
# artık hiçbir uzak script yüklenemez (htmx/Chart.js yerelleştirildiği için
# buna gerek de kalmadı) ve bir XSS başarılı olsa bile veriyi başka bir
# origin'e sızdıramaz (`connect-src 'self'`). 'unsafe-eval' YOK: şablonlarda
# `hx-on`/`hx-vals js:`/`hx-trigger [expr]` kullanımı yok (tarandı), yani
# htmx'in Function() yoluna hiç girilmiyor.
# ---------------------------------------------------------------------------
_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    )
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


from web.routes import (
    agent_backtest as agent_route,
)
from web.routes import (  # noqa: E402  (late import: routers import from server, circular)
    backtest,
    dashboard,
    lab,
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
