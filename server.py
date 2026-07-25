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
See: [[nautilus_kernel]], [[event_driven_architecture]]

Loose analog of Nautilus [[nautilus_kernel]] for the WEB app: bootstraps subsystems in `lifespan()`, then routers dispatch requests. Same "compose, then run" shape.
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
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from data import load_bybit_bars

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


def _datetimefmt(unix_ts: int) -> str:
    from datetime import datetime

    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=UTC)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(unix_ts)


templates.env.filters["datetimefmt"] = _datetimefmt

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

app.include_router(dashboard.router)
app.include_router(studio.router)
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
