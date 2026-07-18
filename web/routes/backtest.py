"""Single-run backtest page — with real-time step progress via polling.

Wiki References
---------------
Bkz: [[backtesting_guide]], [[environment_contexts]]

[[environment_contexts]]'ın Backtest bacağı.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from composer import BLOCK_CATALOG, load_catalog
from data import (
    BYBIT_ALL_INTERVALS,
    BYBIT_CATEGORIES,
    BYBIT_SYMBOLS,
    discover_index_tickers,
    external_instrument_object,
    list_external_instruments,
    load_external_bars,
    load_index_bars,
)
from web.viewmodels import iteration_row
from wiki_helper import read_wiki_page

try:
    import markdown as _md

    def render_md(txt: str) -> str:
        return _md.markdown(txt, extensions=["fenced_code", "tables"])
except Exception:  # pragma: no cover

    def render_md(txt: str) -> str:
        return f"<pre>{txt}</pre>"


router = APIRouter(prefix="/backtest")

# Leaf shared module — keeps the route→shared dependency arrow one-directional.
from web.shared import BACKTEST_LOG, ProgressStore  # noqa: E402
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402

_LAST_RESULT: dict[str, Optional] = {
    "r": None,
    "spec_name": None,
    "narrative": "",
    "bars_info": {},
}
_LAST_RESULT_LOCK = threading.Lock()

# Progress store: run_id → {steps, done, result, error, spec_name}
# ProgressStore holds the dict + lock + capped done-first eviction; the
# _RUN_PROGRESS / _RUN_PROGRESS_LOCK aliases keep every existing direct-access
# site (worker thread ↔ async handler) unchanged.
_RUN_STORE = ProgressStore(50)
_RUN_PROGRESS = _RUN_STORE.raw()
_RUN_PROGRESS_LOCK = _RUN_STORE.lock

# Açıklamadan-strateji üretimi (Claude → custom Python blok). Ayrı store: üretim
# backtest'ten ÖNCE gelen, LLM-bağımlı ve yavaş bir faz; bittiğinde fragment
# mevcut /backtest/run'a zincirlenir (280 satırlık run worker'ı değişmez).
_GEN_STORE = ProgressStore(20)
_GEN_PROGRESS = _GEN_STORE.raw()
_GEN_LOCK = _GEN_STORE.lock

_GEN_PHASES = [
    "Koşullar ayrıştırılıyor",
    "Bloklar yazılıyor (Claude)",
    "Bloklar kaydediliyor",
    "Strateji derleniyor",
]
_GEN_MAX_BLOCKS = 5  # runaway breakdown'a karşı (5 koşul = 5 LLM çağrısı)

# Çoklu-TF taraması: aynı stratejiyi birden çok bar aralığında AYRI koşup
# karşılaştır (robustness'taki multi-symbol taramasının TF versiyonu). Ayrı
# store; her interval sırayla _worker'da koşar, panel canlı doldurulur.
_SWEEP_STORE = ProgressStore(20)
_SWEEP_PROGRESS = _SWEEP_STORE.raw()
_SWEEP_LOCK = _SWEEP_STORE.lock

# Perf: a blank date range used to default to the ENTIRE cache (1M+ 1m bars →
# the backtest blows past the sandbox wall). When the user gives NO explicit
# start/end, bound the run to the most recent N bars so the default completes;
# an explicit range is always honored in full. Interval-agnostic (bar count).
_DEFAULT_MAX_BARS = 100_000

# BACKTEST_LOG, _rotate_if_large, _sanitize_floats, _chart_url and _log_backtest
# now live in web.shared (imported above as re-export aliases) — single source
# of truth; they were duplicated / cross-imported across the route modules.


def _recent_runs(limit: int = 6) -> list[dict]:
    """Son N backtest koşusunu log'dan oku (Run History paneli için)."""
    if not BACKTEST_LOG.exists():
        return []
    out: list[dict] = []
    try:
        # Read only the last 32 KB to avoid loading the full file into memory
        # as the log grows over thousands of runs.
        with open(BACKTEST_LOG, "rb") as fb:
            fb.seek(0, 2)
            size = fb.tell()
            fb.seek(max(0, size - 32768))
            tail = fb.read().decode("utf-8", errors="replace")
        lines = tail.splitlines()
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            spec = rec.get("spec") or {}
            m = rec.get("metrics") or {}
            ts = rec.get("ts", "")
            hhmm = ts[11:16] if len(ts) >= 16 else ""
            out.append(
                {
                    "time": hhmm,
                    "name": spec.get("name", "?"),
                    "pnl": m.get("pnl"),
                }
            )
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


def _generate_narrative(last_row: dict) -> str:
    """Short Turkish narrative about the completed backtest. Falls back to template."""
    try:
        from agent import MODEL, _get_client

        m = last_row
        prompt = (
            f"Bir backtest sonucunu 2-3 cümleyle Türkçe özetle:\n"
            f"Strateji: {m.get('strategy', '?')}\n"
            f"PnL: {m.get('pnl_fmt', '?')} ({m.get('pnl_pct_fmt', '?')})\n"
            f"Trade: {m.get('n_trades', 0)} · Wins: {m.get('n_wins', 0)} · Losses: {m.get('n_losses', 0)}\n"
            f"Win Rate: {m.get('win_rate_fmt', '?')} · Sharpe: {m.get('sharpe_fmt', '?')} · Sortino: {m.get('sortino_fmt', '?')}\n"
            f"Max Drawdown: {m.get('max_dd_fmt', '?')} · Avg Süre: {m.get('avg_dur_fmt', '?')}\n"
            f"Kısa, anlaşılır, trader diline uygun. Başında 'Bu strateji' ile başla."
        )
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        pass
    # Fallback template
    pnl_dir = "kazandı" if (last_row.get("pnl") or 0) >= 0 else "kaybetti"
    return (
        f"Bu strateji {last_row.get('n_trades', 0)} işlem açtı ve "
        f"{last_row.get('pnl_fmt', '?')} {pnl_dir}. "
        f"Win rate %{(last_row.get('win_rate') or 0) * 100:.1f}, "
        f"Sortino {last_row.get('sortino_fmt', '—')}, "
        f"maks çekilme {last_row.get('max_dd_fmt', '—')}."
    )


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    # Sync handler → FastAPI runs it in a threadpool, so its blocking disk I/O
    # (load_catalog + read_wiki_page + _recent_runs) doesn't stall the event
    # loop. (reports/data pages already offload via asyncio.to_thread.)
    from server import get_market_info, templates

    catalog = load_catalog()
    last_row = None
    with _LAST_RESULT_LOCK:
        r = _LAST_RESULT["r"]
        spec_name = _LAST_RESULT["spec_name"]
        narrative = _LAST_RESULT.get("narrative", "")
        bi = dict(_LAST_RESULT.get("bars_info", {}))
    if r is not None:
        last_row = iteration_row(r)
        last_row["rationale"] = r.rationale
        last_row["equity_curve"] = r.equity_curve
        last_row["equity_dates"] = r.equity_dates
        last_row["spec_name"] = spec_name
        last_row["narrative"] = narrative
        last_row["bars_info"] = bi  # robustness paneli için gerekli (#1)
        if bi.get("symbol"):
            _sid = (last_row.get("params") or {}).get("spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

    wiki_html = render_md(read_wiki_page("wiki/concepts/order_flow_pipeline.md"))
    preferred_spec_id = request.query_params.get("spec_id", "")
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "active": "backtest",
            "page_title": "Backtest",
            "market": get_market_info(),
            "catalog": catalog,
            "block_catalog": BLOCK_CATALOG,
            "last": last_row,
            "wiki_html": wiki_html,
            "preferred_spec_id": preferred_spec_id,
            "recent_runs": _recent_runs(6),
            # Ana panel: sembol datalist (yaz-bul), kategori seçimi, çoklu-TF checkbox'ları.
            "bybit_symbols": BYBIT_SYMBOLS,
            "bybit_categories": BYBIT_CATEGORIES,
            "bybit_intervals": BYBIT_ALL_INTERVALS,
        },
    )


@router.get("/tickers", response_class=HTMLResponse)
async def tickers(request: Request):
    try:
        ts = discover_index_tickers()
    except Exception as e:
        return HTMLResponse(
            f"<option value=''>ticker discovery failed: {type(e).__name__}: {e}</option>"
        )
    if not ts:
        return HTMLResponse("<option value=''>no tickers found</option>")
    return HTMLResponse("".join(f'<option value="{t}">{t}</option>' for t in ts))


@router.get("/external_instruments", response_class=HTMLResponse)
async def external_instruments(request: Request):
    """<option> list for the External data-source picker. Each option carries
    its available timeframes in data-grans so the UI can narrow the select."""
    try:
        rows = list_external_instruments()
    except Exception as e:
        return HTMLResponse(
            f"<option value=''>external catalog scan failed: {type(e).__name__}: {e}</option>"
        )
    if not rows:
        return HTMLResponse("<option value=''>no external instruments found</option>")
    return HTMLResponse(
        "".join(
            f'<option value="{r["instrument_id"]}" data-grans="{",".join(r["granularities"])}">'
            f"{r['instrument_id']}</option>"
            for r in rows
        )
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    spec_id: str = Form(...),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("60"),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form(""),
    granularity: str = Form("1d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ext_instrument: str = Form(""),
    ext_granularity: str = Form("1-DAY"),
    ext_start: str = Form(""),
    ext_end: str = Form(""),
):
    """Return a progress panel immediately, run the backtest in a daemon thread."""
    from server import templates

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Spec not found.</div>", status_code=404
        )

    # Capture all form params for the worker (no I/O in this handler)
    run_id = uuid.uuid4().hex[:8]
    # Evict to stay within cap (done-first, else oldest — see ProgressStore).
    _RUN_STORE.create_evicting(
        run_id,
        {
            "steps": [],
            "done": False,
            "result": None,
            "error": None,
            "spec_name": spec.name,
            "bars_info": {},
            "narrative": "",
        },
    )

    def _progress(msg: str) -> None:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        with _RUN_PROGRESS_LOCK:
            state = _RUN_PROGRESS.get(run_id)
            if state is not None:
                state["steps"].append({"ts": ts, "msg": msg})

    # All heavy work — parquet read + Bar construction + engine.run() — in thread
    def _set_error(msg: str) -> None:
        with _RUN_PROGRESS_LOCK:
            if run_id in _RUN_PROGRESS:
                _RUN_PROGRESS[run_id]["error"] = msg

    def _worker() -> None:
        import time as _time
        from datetime import timedelta

        import pandas as _pd

        from data import _bybit_cache_path, load_bybit_bars

        _t_start = _time.perf_counter()
        try:
            _progress(f"Başlatılıyor · {spec.name} · {instrument_kind}")

            if instrument_kind == "Bybit":
                cache_path = _bybit_cache_path(category, symbol, interval)
                # Her zaman cache'deki gerçek aralığı kullan — tarih girilmezse
                # cache başı/sonu sabit kalır, her çalıştırmada sonuç aynı olur.
                if cache_path.exists():
                    df_check = _pd.read_parquet(cache_path)
                    cache_start = (
                        df_check.index[0].to_pydatetime().replace(tzinfo=UTC)
                        if not df_check.empty
                        else None
                    )
                    cache_end = (
                        df_check.index[-1].to_pydatetime().replace(tzinfo=UTC)
                        if not df_check.empty
                        else None
                    )
                else:
                    cache_start = cache_end = None

                if bybit_start:
                    start_dt = datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                else:
                    start_dt = cache_start or datetime.now(UTC) - timedelta(days=7)

                if bybit_end:
                    end_dt = datetime.fromisoformat(bybit_end).replace(
                        hour=23, minute=59, second=59, tzinfo=UTC
                    )
                else:
                    end_dt = cache_end or datetime.now(UTC)

                _progress(f"Veri okunuyor · {symbol}/{category}/{interval}…")
                bars = load_bybit_bars(
                    symbol=symbol,
                    interval=interval,
                    category=category,
                    start=start_dt,
                    end=end_dt,
                )
                if bars.empty:
                    _set_error(
                        f"No cached bars for {symbol}/{category}/{interval}. "
                        "Fetch them first from the Data catalog."
                    )
                    return
                # Perf: cap the DEFAULT (blank-range) run to recent bars so it
                # completes; explicit dates are honored in full (see L88).
                if not bybit_start and not bybit_end and len(bars) > _DEFAULT_MAX_BARS:
                    _progress(
                        f"Varsayılan aralık son {_DEFAULT_MAX_BARS:,} bara "
                        "sınırlandı (tam geçmiş için tarih aralığı girin)"
                    )
                    bars = bars.iloc[-_DEFAULT_MAX_BARS:]
                # Infer base currency from symbol (ETHUSDT → ETH, SOLUSDT → SOL).
                base_guess = "BTC"
                for suffix in ("USDT", "USDC", "USD"):
                    if symbol.endswith(suffix):
                        base_guess = symbol[: -len(suffix)] or "BTC"
                        break
            if instrument_kind == "Bybit":
                # BacktestEngine yolu — per-trade detay (işlem listesi + grafik
                # markerları) için gerekli. BacktestNode özet-only olduğundan
                # trade-level görselleştirmeyi destekleyemiyor (v2rc1 kısıtı).
                bars_info = {
                    "symbol": symbol,
                    "category": category,
                    "interval": interval,
                    "n_bars": len(bars),
                    "start": str(bars.index[0]),
                    "end": str(bars.index[-1]),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                _progress(
                    f"BacktestEngine · {symbol}/{category}/{interval}m · {len(bars):,} bar"
                )
                # Route through the sandbox: builtin-only specs run in-process
                # (zero overhead); specs with custom blocks run in a killable
                # child so runaway user code can't hang the server.
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    spec,
                    bars,
                    recipe={
                        "symbol": symbol,
                        "base": base_guess,
                        "category": category,
                        "interval": interval,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=f"user-run · {symbol} {category} {interval}m",
                    # builtin spec'ler de child'da: Nautilus backtest GIL'i
                    # tutar; in-process koşarsa event loop donar (agent bug'ı).
                    force_subprocess=True,
                )
            elif instrument_kind == "External":
                if not ext_instrument:
                    _set_error("Instrument required for External catalog.")
                    return
                # Tarihler opsiyonel — boş bırakılırsa katalogdaki tüm aralık.
                try:
                    start_dt = (
                        datetime.fromisoformat(ext_start).replace(tzinfo=UTC)
                        if ext_start
                        else None
                    )
                    end_dt = (
                        datetime.fromisoformat(ext_end).replace(
                            hour=23, minute=59, second=59, tzinfo=UTC
                        )
                        if ext_end
                        else None
                    )
                except ValueError:
                    _set_error("ext_start and ext_end must be YYYY-MM-DD.")
                    return
                _progress(f"Veri okunuyor · {ext_instrument}/{ext_granularity}…")
                bars = load_external_bars(
                    ext_instrument, ext_granularity, start=start_dt, end=end_dt
                )
                if bars.empty:
                    _set_error(
                        f"No bars for {ext_instrument} {ext_granularity} in the "
                        "selected date range."
                    )
                    return
                instrument = external_instrument_object(ext_instrument)
                if instrument is None:
                    _set_error(
                        f"Instrument definition for {ext_instrument} not found in "
                        "the external catalog."
                    )
                    return
                rationale = f"user-run · External {ext_instrument} {ext_granularity}"
                run_spec = spec
                if float(spec.trade_size) < 1 and int(instrument.size_precision) == 0:
                    # Shared catalog spec'ini mutate etme — sadece bu run için klonla (#39)
                    import copy as _copy

                    run_spec = _copy.copy(spec)
                    run_spec.trade_size = 1.0
                    rationale += " · trade_size clamped to 1"
                # bars_info deliberately has no "symbol" key (Index convention):
                # the Bybit chart URL and robustness panel key on it and would
                # otherwise fetch Bybit klines for a non-Bybit instrument.
                bars_info = {
                    "ticker": ext_instrument,
                    "granularity": ext_granularity,
                    "n_bars": len(bars),
                    # L34: günlük+ granülaritede saat bileşeni gösterilmez —
                    # açılış-zamanı indeksi 'kapanış − takvim aralığı' nominal
                    # değeridir (00:00 NY ≈ 04:00 UTC), seans saati DEĞİL.
                    "start": str(bars.index[0].date())
                    if ext_granularity in ("1-DAY", "1-WEEK")
                    else str(bars.index[0]),
                    "end": str(bars.index[-1].date())
                    if ext_granularity in ("1-DAY", "1-WEEK")
                    else str(bars.index[-1]),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                _progress(
                    f"BacktestEngine · {ext_instrument}/{ext_granularity} · {len(bars):,} bar"
                )
                # Sandbox yolu: builtin-only spec'ler in-process (sıfır ek yük),
                # custom bloklu spec'ler kill edilebilir child'da koşar — Bybit
                # dalıyla aynı güvence (child instrument'ı recipe'den kurar).
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "external",
                        "instrument_id": ext_instrument,
                        "granularity": ext_granularity,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=rationale,
                    force_subprocess=True,  # GIL izolasyonu (bkz. Bybit dalı)
                )
            else:
                if not ticker:
                    _set_error("Ticker required for Index instruments.")
                    return
                try:
                    start_d, end_d = (
                        date.fromisoformat(start_date),
                        date.fromisoformat(end_date),
                    )
                except ValueError:
                    _set_error("start_date and end_date must be YYYY-MM-DD.")
                    return
                _progress(f"Veri okunuyor · {ticker}/{granularity}…")
                bars = load_index_bars(ticker, start_d, end_d, granularity)
                if bars.empty:
                    _set_error(f"No bars for {ticker}.")
                    return
                rationale = f"user-run · Index {ticker} {granularity} {start_d}→{end_d}"
                run_spec = spec
                if float(spec.trade_size) < 1:
                    # Shared catalog spec'ini mutate etme — sadece bu run için klonla (#39)
                    import copy as _copy

                    run_spec = _copy.copy(spec)
                    run_spec.trade_size = 1.0
                    rationale += " · trade_size clamped to 1"
                bars_info = {
                    "ticker": ticker,
                    "granularity": granularity,
                    "start": str(start_d),
                    "end": str(end_d),
                    "n_bars": len(bars),
                    "first_close": float(bars.iloc[0]["close"]),
                    "last_close": float(bars.iloc[-1]["close"]),
                }
                # Index yolu da sandbox'ta: daha önce ham run_composed_backtest
                # bu daemon thread'de GIL tutuyordu (donma riski). Child,
                # instrument/bar_type'ı index recipe'sinden yeniden kurar.
                from sandbox import run_backtest_guarded

                result = run_backtest_guarded(
                    run_spec,
                    bars,
                    recipe={
                        "source": "index",
                        "ticker": ticker,
                        "granularity": granularity,
                    },
                    iteration_id=0,
                    progress_fn=_progress,
                    rationale=rationale,
                    force_subprocess=True,
                )

            # Store result first so UI can display it regardless of log I/O outcome.
            narrative = ""
            if result.error is None:
                try:
                    nrow = iteration_row(result)
                    nrow["spec_name"] = spec.name
                    narrative = _generate_narrative(nrow)
                except Exception:
                    narrative = ""

            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:  # evict edilmişse yazma (#7)
                    _RUN_PROGRESS[run_id]["result"] = result
                    _RUN_PROGRESS[run_id]["bars_info"] = bars_info
                    _RUN_PROGRESS[run_id]["narrative"] = narrative
            with _LAST_RESULT_LOCK:  # torn read önle (#8)
                _LAST_RESULT["r"] = result
                _LAST_RESULT["spec_name"] = spec.name
                _LAST_RESULT["bars_info"] = bars_info
                _LAST_RESULT["narrative"] = narrative  # yeni sonuçla eşle (#23)

            try:
                _log_backtest(
                    run_spec if "run_spec" in locals() else spec,
                    result,
                    instrument_kind,
                    bars_info,
                    elapsed_sec=_time.perf_counter() - _t_start,
                )
            except Exception:
                pass  # log I/O failure must not hide the result already stored above
        except Exception as e:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _RUN_PROGRESS_LOCK:
                if run_id in _RUN_PROGRESS:
                    _RUN_PROGRESS[run_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()

    return templates.TemplateResponse(
        request,
        "fragments/backtest_progress.html",
        {"run_id": run_id, "steps": [], "done": False, "error": None},
    )


# ── Açıklamadan strateji üret → /backtest/run'a zincirle ────────────────────
# Kullanıcı doğal dille strateji tarif eder; Claude entry+exit için YENİ Python
# blokları yazar (codegate doğrulama + smoke), spec kataloğa yazılır ve fragment
# mevcut /backtest/run'ı aynı enstrüman parametreleriyle tetikler. Böylece
# enstrüman seçimi (Bybit/Index/External) ve backtest yolu tek kaynakta kalır.


def _gen_state_view(gen_id: str) -> dict | None:
    """Kilit altında kopya — canlı dict'i template'e vermek torn read olurdu.

    ``chain_vals``: üretim bittiğinde /backtest/run'a POST edilecek tam form
    değerleri (enstrüman parametreleri + yeni spec_id). None ise zincir yok.
    """
    with _GEN_LOCK:
        raw = _GEN_PROGRESS.get(gen_id)
        if raw is None:
            return None
        chain = None
        if raw["done"] and raw["spec_id"] and not raw["error"]:
            chain = dict(raw["run_params"], spec_id=raw["spec_id"])
        return {
            "phases": [dict(p) for p in raw["phases"]],
            "done": raw["done"],
            "error": raw["error"],
            "spec_id": raw["spec_id"],
            "spec_name": raw["spec_name"],
            "chain_vals": chain,
        }


def _normalize_intervals(codes: list[str]) -> list[str]:
    """Yalnız desteklenen Bybit interval kodları, form sırasını koru, tekilleştir."""
    valid = {code for code, _label in BYBIT_ALL_INTERVALS}
    seen: set[str] = set()
    return [c for c in codes if c in valid and not (c in seen or seen.add(c))]


def _gen_phase(gen_id: str, idx: int, detail: str = "", done: bool = False) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    with _GEN_LOCK:
        st = _GEN_PROGRESS.get(gen_id)
        if st is None:
            return
        for i, p in enumerate(st["phases"]):
            if i < idx or (i == idx and done):
                p["status"] = "done"
            elif i == idx:
                p["status"] = "running"
                p["detail"] = detail
                p["ts"] = ts


@router.post("/describe", response_class=HTMLResponse)
async def describe(
    request: Request,
    description: str = Form(""),
    instrument_kind: str = Form("Bybit"),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    interval: str = Form("60"),
    intervals: list[str] = Form(default=[]),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
    ticker: str = Form(""),
    granularity: str = Form("1d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ext_instrument: str = Form(""),
    ext_granularity: str = Form("1-DAY"),
    ext_start: str = Form(""),
    ext_end: str = Form(""),
):
    """Açıklamadan custom blok üret; bitince fragment backtest'i tetikler.

    ``intervals`` (checkbox'lar) çoklu-TF seçimidir: 2+ ise üretim sonunda
    /backtest/sweep'e (karşılaştırma tablosu), 1 ise /backtest/run'a (tam sonuç)
    zincirlenir. Boşsa tekil ``interval`` alanına düşer (eski davranış)."""
    from server import templates

    desc = (description or "").strip()
    if len(desc) < 10:
        return HTMLResponse(
            "<div class='empty-state'>Lütfen stratejiyi biraz daha ayrıntılı "
            "tarif edin (en az 10 karakter) — Claude bu metinden Python blok "
            "yazacak.</div>",
            status_code=400,
        )

    # Çoklu-TF: checkbox'lar → normalize; boşsa tekil interval'e düş; o da yoksa 1h.
    norm_intervals = (
        _normalize_intervals(intervals) or _normalize_intervals([interval]) or ["60"]
    )

    gen_id = uuid.uuid4().hex[:8]
    # Zincirlenecek backtest parametreleri — spec_id üretim sonunda eklenir.
    # ``intervals``/``intervals_csv``: sweep zincirinin çoklu-TF listesi (csv,
    # hx-vals dizi kodlamasına güvenmeyen sağlam yol). ``interval``: tek-TF /run.
    run_params = {
        "instrument_kind": instrument_kind,
        "symbol": symbol,
        "category": category,
        "interval": norm_intervals[0],
        "intervals": norm_intervals,
        "intervals_csv": ",".join(norm_intervals),
        "bybit_start": bybit_start,
        "bybit_end": bybit_end,
        "ticker": ticker,
        "granularity": granularity,
        "start_date": start_date,
        "end_date": end_date,
        "ext_instrument": ext_instrument,
        "ext_granularity": ext_granularity,
        "ext_start": ext_start,
        "ext_end": ext_end,
    }
    _GEN_STORE.create_evicting(
        gen_id,
        {
            "phases": [
                {"label": p, "status": "pending", "detail": "", "ts": ""}
                for p in _GEN_PHASES
            ],
            "done": False,
            "error": None,
            "spec_id": "",
            "spec_name": "",
            "run_params": run_params,
        },
    )

    def _worker() -> None:
        from agent import (
            GeneratedCodeError,
            propose_condition_breakdown,
            propose_custom_block,
        )
        from composer import (
            ComposedStrategySpec,
            SignalBlock,
            append_to_catalog,
            new_spec_id,
            register_custom_from_disk,
        )
        from custom_block_store import is_valid_name, save_custom
        from web.routes.lab import _atr_stop_fallback

        try:
            # ── Faz 0: tarifi AYRI koşullara böl (her biri ayrı, düzenlenebilir
            # blok olacak). Başarısızsa tek entry + tek exit'e düş (eski davranış). ──
            _gen_phase(gen_id, 0, "Claude tarifi ayrı koşullara bölüyor…")
            entry_logic, exit_logic = "OR", "OR"
            try:
                bd = propose_condition_breakdown(desc)
                label = bd["label"]
                entry_logic, exit_logic = bd["entry_logic"], bd["exit_logic"]
                conditions = bd["conditions"][:_GEN_MAX_BLOCKS]
                _gen_phase(
                    gen_id,
                    0,
                    f"✓ {len(conditions)} koşul · entry={entry_logic} / exit={exit_logic}",
                    done=True,
                )
            except Exception as e:  # LLM/parse/şema — tek-blok yoluna düş
                label = desc[:40].strip() or "Tarif edilen strateji"
                conditions = [
                    {"role": "entry", "label": label, "desc": desc},
                    {"role": "exit", "label": label, "desc": desc},
                ]
                _gen_phase(
                    gen_id, 0, f"tek koşula düşüldü ({type(e).__name__})", done=True
                )

            # ── Faz 1: her koşul için AYRI custom Python bloğu yaz ──
            _gen_phase(gen_id, 1, "Claude blokları yazıyor…")
            made: list[tuple[str, dict, str]] = []  # (name, block, role)
            counters = {"entry": 0, "exit": 0}
            # DİKKAT: "entry"[0]=="exit"[0]=="e" → role[0] KULLANMA (ad çakışır).
            _role_tag = {"entry": "e", "exit": "x"}
            for cond in conditions:
                role = cond["role"]
                counters[role] += 1
                name = f"desc_{_role_tag[role]}{counters[role]}_{gen_id}"  # desc_e1_.., desc_x1_..
                _gen_phase(gen_id, 1, f"{cond['label']} ({role})…")
                try:
                    blk = propose_custom_block(cond["label"], cond["desc"], role)
                except GeneratedCodeError:
                    if role == "exit":
                        blk = _atr_stop_fallback()  # exit üretilemedi → ATR stop
                    else:
                        continue  # entry üretilemedi → o koşulu atla
                made.append((name, blk, role))

            if not any(r == "entry" for _, _, r in made):
                raise RuntimeError(
                    "Hiç entry bloğu üretilemedi — tarifi somutlaştırın "
                    "(hangi indikatör, hangi eşik, ne zaman gir)."
                )
            if not any(r == "exit" for _, _, r in made):
                made.append((f"desc_xf_{gen_id}", _atr_stop_fallback(), "exit"))
            n_e = sum(1 for _, _, r in made if r == "entry")
            n_x = sum(1 for _, _, r in made if r == "exit")
            _gen_phase(gen_id, 1, f"✓ {n_e} entry + {n_x} exit blok", done=True)

            # ── Faz 2: kaydet + register (Composer'da tek tek görünür/düzenlenir) ──
            _gen_phase(gen_id, 2, "Bloklar diske yazılıyor…")
            for name, blk, _role in made:
                if not is_valid_name(name):
                    raise RuntimeError(f"Geçersiz blok adı: {name}")
                save_custom(name, blk["meta"], blk["code"], prompt=desc)
                register_custom_from_disk(name)
            _gen_phase(gen_id, 2, f"{len(made)} blok Composer'da görünür", done=True)

            # ── Faz 3: spec derle (blok-seviyesi OR/AND) + kataloğa yaz ──
            _gen_phase(gen_id, 3, "Strateji derleniyor…")

            def _params(blk: dict) -> dict:
                raw = blk["meta"].get("params") or {}
                return {
                    k: (v.get("default") if isinstance(v, dict) else v)
                    for k, v in raw.items()
                }

            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=label,
                description=desc,
                blocks=[
                    SignalBlock(type=name, role=role, params=_params(blk))
                    for name, blk, role in made
                ],
                trade_size=0.01,
                allow_short=False,
                entry_logic=entry_logic,
                exit_logic=exit_logic,
            )
            err = spec.validate()
            if err:
                raise RuntimeError(f"Spec hatası: {err}")
            append_to_catalog(spec)  # M14: kilitli append
            _gen_phase(
                gen_id, 3, f"✓ {spec.name} · {entry_logic}/{exit_logic}", done=True
            )

            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["spec_id"] = spec.id
                    _GEN_PROGRESS[gen_id]["spec_name"] = spec.name
        except Exception as e:
            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _GEN_LOCK:
                if gen_id in _GEN_PROGRESS:
                    _GEN_PROGRESS[gen_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    state = _gen_state_view(gen_id)
    return templates.TemplateResponse(
        request,
        "fragments/describe_progress.html",
        {"gen_id": gen_id, "state": state, "done": False},
    )


@router.get("/describe/progress/{gen_id}", response_class=HTMLResponse)
async def describe_progress(request: Request, gen_id: str):
    from server import templates

    state = _gen_state_view(gen_id)
    if state is None:
        return HTMLResponse(
            "<div class='empty-state'>Üretim kaydı bulunamadı (sunucu yeniden "
            "başlatılmış olabilir).</div>"
        )
    return templates.TemplateResponse(
        request,
        "fragments/describe_progress.html",
        {"gen_id": gen_id, "state": state, "done": state["done"]},
    )


# ── Çoklu-TF taraması: aynı strateji, birden çok bar aralığı ────────────────
# Her interval için Bybit bar'ları yüklenip run_backtest_guarded ile AYRI koşulur;
# sonuçlar tek karşılaştırma tablosunda toplanır. Bar-yükleme (load_bybit_bars) ve
# koşum (run_backtest_guarded) mevcut primitiflerdir — 280 satırlık /run worker'ı
# çoğaltılmaz.

# Tablo sütunlarına metrik çıkar (NAU: sharpe = per-trade). None güvenli.
_SWEEP_METRIC_KEYS = (
    "pnl_pct",
    "sharpe_per_trade",
    "max_dd_pct",
    "n_trades",
    "win_rate",
    "profit_factor",
)


def _sweep_row_metrics(metrics: dict) -> dict:
    m = metrics or {}
    return {k: m.get(k) for k in _SWEEP_METRIC_KEYS}


def _sweep_state_view(sweep_id: str) -> dict | None:
    with _SWEEP_LOCK:
        raw = _SWEEP_PROGRESS.get(sweep_id)
        if raw is None:
            return None
        return {
            "spec_name": raw["spec_name"],
            "symbol": raw["symbol"],
            "category": raw["category"],
            "done": raw["done"],
            "error": raw["error"],
            "rows": [dict(r) for r in raw["rows"]],
        }


@router.post("/sweep", response_class=HTMLResponse)
async def sweep(
    request: Request,
    spec_id: str = Form(...),
    symbol: str = Form("BTCUSDT"),
    category: str = Form("linear"),
    intervals: list[str] = Form(default=[]),
    intervals_csv: str = Form(""),
    bybit_start: str = Form(""),
    bybit_end: str = Form(""),
):
    """Aynı stratejiyi seçilen TF'lerin her birinde koş → karşılaştırma tablosu."""
    from server import templates

    catalog = load_catalog()
    spec = next((s for s in catalog if s.id == spec_id), None)
    if spec is None:
        return HTMLResponse(
            "<div class='empty-state'>Strateji bulunamadı.</div>", status_code=404
        )
    # describe→sweep zinciri intervals'ı csv olarak taşır (hx-vals dizi kodlamasına
    # güvenmeden). Doğrudan form POST'u (standalone/testler) ``intervals`` listesini
    # verir; o varsa öncelik onda.
    raw = list(intervals)
    if not raw and intervals_csv:
        raw = [x.strip() for x in intervals_csv.split(",") if x.strip()]
    picked = _normalize_intervals(raw)
    if len(picked) < 2:
        return HTMLResponse(
            "<div class='empty-state'>En az 2 zaman dilimi seçin "
            "(karşılaştırma için).</div>",
            status_code=400,
        )

    label_of = dict(BYBIT_ALL_INTERVALS)
    sweep_id = uuid.uuid4().hex[:8]
    _SWEEP_STORE.create_evicting(
        sweep_id,
        {
            "spec_name": spec.name,
            "symbol": symbol,
            "category": category,
            "done": False,
            "error": None,
            "rows": [
                {
                    "interval": code,
                    "label": label_of.get(code, code),
                    "status": "pending",
                    "error": "",
                    "metrics": _sweep_row_metrics({}),
                }
                for code in picked
            ],
        },
    )

    def _row(code: str, **upd) -> None:
        with _SWEEP_LOCK:
            st = _SWEEP_PROGRESS.get(sweep_id)
            if st is None:
                return
            for r in st["rows"]:
                if r["interval"] == code:
                    r.update(upd)
                    return

    def _worker() -> None:
        from concurrent.futures import ThreadPoolExecutor
        from datetime import timedelta

        import pandas as _pd

        from data import _base_ccy, _bybit_cache_path, load_bybit_bars
        from parallel_exec import get_worker_count, parallel_enabled
        from sandbox import run_backtest_guarded

        base = _base_ccy(symbol)

        def _run_one(code: str) -> None:
            _row(code, status="running")
            try:
                cp = _bybit_cache_path(category, symbol, code)
                if not cp.exists():
                    _row(code, status="error", error="cache yok — Data ekranından çek")
                    return
                # Tarih boşsa cache'in tam aralığı (deterministik).
                if bybit_start:
                    s_dt = datetime.fromisoformat(bybit_start).replace(tzinfo=UTC)
                else:
                    s_dt = None
                if bybit_end:
                    e_dt = datetime.fromisoformat(bybit_end).replace(
                        hour=23, minute=59, second=59, tzinfo=UTC
                    )
                else:
                    e_dt = None
                if s_dt is None or e_dt is None:
                    _df = _pd.read_parquet(cp)
                    if not _df.empty:
                        s_dt = s_dt or _df.index[0].to_pydatetime().replace(tzinfo=UTC)
                        e_dt = e_dt or _df.index[-1].to_pydatetime().replace(tzinfo=UTC)
                    else:
                        s_dt = s_dt or datetime.now(UTC) - timedelta(days=7)
                        e_dt = e_dt or datetime.now(UTC)
                bars = load_bybit_bars(
                    symbol=symbol,
                    interval=code,
                    category=category,
                    start=s_dt,
                    end=e_dt,
                )
                if bars.empty:
                    _row(code, status="error", error="bar yok")
                    return
                # Perf: bound the DEFAULT (blank-range) sweep run to recent bars;
                # explicit dates honored in full (see L88).
                if not bybit_start and not bybit_end and len(bars) > _DEFAULT_MAX_BARS:
                    bars = bars.iloc[-_DEFAULT_MAX_BARS:]
                result = run_backtest_guarded(
                    spec,
                    bars,
                    recipe={
                        "symbol": symbol,
                        "base": base,
                        "category": category,
                        "interval": code,
                    },
                    iteration_id=0,
                    rationale=f"tf-sweep · {symbol} {category} {code}",
                    force_subprocess=True,
                )
                if result.error:
                    _row(code, status="error", error=result.error[:120])
                else:
                    _row(
                        code,
                        status="done",
                        metrics=_sweep_row_metrics(result.metrics),
                        n_bars=len(bars),
                    )
            except Exception as e:
                _row(code, status="error", error=f"{type(e).__name__}: {e}"[:120])

        try:
            # Intervals are INDEPENDENT full backtests on different bars, so run
            # them concurrently — each force_subprocess child runs in parallel
            # while its supervisor thread just blocks. Bounded + kill-switchable
            # (NAUTILUS_PARALLEL=0 → sequential). Per-row errors handled inside.
            workers = min(len(picked), get_worker_count()) if parallel_enabled() else 1
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                list(ex.map(_run_one, picked))
        except Exception as e:
            with _SWEEP_LOCK:
                if sweep_id in _SWEEP_PROGRESS:
                    _SWEEP_PROGRESS[sweep_id]["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _SWEEP_LOCK:
                if sweep_id in _SWEEP_PROGRESS:
                    _SWEEP_PROGRESS[sweep_id]["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    return templates.TemplateResponse(
        request,
        "fragments/sweep_progress.html",
        {"sweep_id": sweep_id, "state": _sweep_state_view(sweep_id), "done": False},
    )


@router.get("/sweep/progress/{sweep_id}", response_class=HTMLResponse)
async def sweep_progress(request: Request, sweep_id: str):
    from server import templates

    state = _sweep_state_view(sweep_id)
    if state is None:
        return HTMLResponse(
            "<div class='empty-state'>Tarama kaydı bulunamadı (sunucu yeniden "
            "başlatılmış olabilir).</div>"
        )
    return templates.TemplateResponse(
        request,
        "fragments/sweep_progress.html",
        {"sweep_id": sweep_id, "state": state, "done": state["done"]},
    )


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    from server import templates

    # Snapshot state under lock to avoid torn reads from the worker thread.
    with _RUN_PROGRESS_LOCK:
        raw = _RUN_PROGRESS.get(run_id)
        if raw is None:
            return HTMLResponse("<div class='empty-state'>Unknown run ID.</div>")
        state = {
            "done": raw["done"],
            "result": raw["result"],
            "error": raw["error"],
            "spec_name": raw["spec_name"],
            "steps": list(raw["steps"]),
            "bars_info": raw.get("bars_info", {}),
            "narrative": raw.get("narrative", ""),
        }

    if state["done"] and state["result"] is not None:
        result = state["result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["spec_name"]
        last_row["steps"] = state["steps"]
        last_row["narrative"] = state["narrative"]  # worker'da üretildi (#9)
        bi = state.get("bars_info", {})
        last_row["bars_info"] = bi  # robustness paneli için (#1)
        if bi.get("symbol"):
            _sid = (last_row.get("params") or {}).get("spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

        with _RUN_PROGRESS_LOCK:
            _RUN_PROGRESS.pop(run_id, None)

        return templates.TemplateResponse(
            request,
            "fragments/backtest_result.html",
            {"last": last_row},
        )

    if state["done"] and state["error"]:
        with _RUN_PROGRESS_LOCK:
            _RUN_PROGRESS.pop(run_id, None)
        return HTMLResponse(
            f"<div class='panel' style='border-color:rgba(239,68,68,0.5)'>"
            f"<div class='panel-body'><span class='badge exit'>✗ HATA</span>"
            f"<pre class='diagram mt-3'>{state['error']}</pre></div></div>"
        )

    return templates.TemplateResponse(
        request,
        "fragments/backtest_progress.html",
        {"run_id": run_id, "steps": state["steps"], "done": False, "error": None},
    )
