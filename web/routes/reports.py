"""Backtest Reports — reports all past runs.

Reads backtest_log.jsonl; shows manual backtest + Strategy Lab + autonomous agent
runs in a single table. Joins with robustness_log.jsonl via spec_id.

Endpoints
---------
GET  /reports               Main page
GET  /reports/export.csv    Download filtered CSV
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from web.viewmodels import fmt_dur, fmt_money, fmt_num, fmt_pct

router = APIRouter(prefix="/reports")

# Log paths come from web.shared (single source of truth; they were duplicated
# with the writer modules). reports.py reads these via its own module globals,
# so tests can still monkeypatch reports.BACKTEST_LOG / .ROBUSTNESS_LOG.
from web.shared import BACKTEST_LOG, ROBUSTNESS_LOG  # noqa: E402

REPORTS_LAYOUT = Path.home() / ".cache" / "nautilus_web_app" / "reports_layout.json"


def _view_state_fields(data: dict) -> dict:
    """view state other than order/hidden: sort + column filters + variant.

    Unknown/wrong-typed fields are silently dropped — even if the file is
    manually corrupted, the page opens with defaults.
    """
    out: dict = {}
    sort = data.get("sort")
    if isinstance(sort, dict) and sort.get("key"):
        out["sort"] = {"key": str(sort["key"]), "asc": bool(sort.get("asc"))}
    filters = data.get("filters")
    if isinstance(filters, dict):
        clean = {
            str(k): str(v).strip()
            for k, v in filters.items()
            if isinstance(v, (str, int, float)) and str(v).strip()
        }
        if clean:
            out["filters"] = clean
    variant = data.get("variant")
    if isinstance(variant, str) and variant:
        out["variant"] = variant
    page_size = data.get("pageSize")
    if isinstance(page_size, (int, float)) and not isinstance(page_size, bool):
        page_size = int(page_size)
        if 0 <= page_size <= 100_000:  # 0 = All
            out["pageSize"] = page_size
    return out


def _load_layout() -> dict:
    """Read saved view state.

    {'order': [...], 'hidden': [...], 'sort': {key, asc},
     'filters': {key: expr}, 'variant': str} — sort/filters/variant optional.
    """
    if not REPORTS_LAYOUT.exists():
        return {}
    try:
        data = json.loads(REPORTS_LAYOUT.read_text())
        if not isinstance(data, dict):
            return {}
        out = {
            "order": list(data.get("order") or []),
            "hidden": list(data.get("hidden") or []),
        }
        out.update(_view_state_fields(data))
        return out
    except Exception:
        return {}


def _save_layout(data: dict) -> None:
    """Write view state to disk atomically (.tmp → replace)."""
    REPORTS_LAYOUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "order": [str(k) for k in (data.get("order") or [])],
        "hidden": [str(k) for k in (data.get("hidden") or [])],
    }
    payload.update(_view_state_fields(data))
    tmp = REPORTS_LAYOUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(REPORTS_LAYOUT)


def _utc_to_local(ts: str) -> str:
    """Convert ISO UTC timestamp to local time string 'YYYY-MM-DD HH:MM:SS'."""
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts[:19].replace("T", " ")


def _runner_label(rationale: str) -> str:
    r = (rationale or "").strip()
    if r.startswith("user-run"):
        return "Human"
    if r.startswith("Strategy Lab"):
        return "Lab"
    if "catalog cycle" in r:
        return "Agent"
    if r.startswith("agent-run"):
        return "Agent"
    return "Unknown"


def _fmt_test_period(start: str, end: str) -> str:
    """Convert bars.start → bars.end difference to human-readable duration (e.g. '6.1y', '14month')."""
    from datetime import datetime as _dt

    if not start or not end:
        return "—"
    try:
        # Log format: "2020-03-25 10:00:00+00:00" or "2020-03-25"
        s = _dt.fromisoformat(str(start).replace(" ", "T", 1)[:25])
        e = _dt.fromisoformat(str(end).replace(" ", "T", 1)[:25])
    except Exception:
        return "—"
    days = (e - s).total_seconds() / 86400
    if days <= 0:
        return "—"
    if days < 1:
        return f"{days * 24:.0f}h"
    if days < 60:
        return f"{days:.0f}d"
    if days < 730:
        return f"{days / 30.44:.1f}month"
    return f"{days / 365.25:.1f}y"


def _fmt_elapsed(sec: float | None) -> str:
    """Backtest wall-clock run time → readable (e.g. '0.8s', '2.4min')."""
    if sec is None:
        return "—"
    try:
        sec = float(sec)
    except Exception:
        return "—"
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{sec / 60:.1f}min"


def _load_robustness_index() -> dict:
    """Robustness record index.

    Primary composite key: ``(spec_id, symbol, interval)`` — different
    symbol/TF runs of the same spec no longer overwrite each other. For legacy
    records (no identity fields) and backward compatibility, single
    ``spec_id`` / ``spec_name`` keys are also kept (last-writer-wins, used only
    as a fallback).
    """
    if not ROBUSTNESS_LOG.exists():
        return {}
    index: dict = {}
    with open(ROBUSTNESS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("spec_id") or ""
            sname = rec.get("spec_name") or ""
            sym = rec.get("symbol") or ""
            iv = rec.get("interval") or ""
            # Later lines in the file are newer — overwrite keeps latest
            if sid and sym and iv:
                index[(sid, sym, iv)] = rec
            if sid:
                index[sid] = rec
            if sname:
                index[sname] = rec
    return index


def _rob_fields(rob: dict | None) -> dict:
    """Formatted fields from a robustness record to add to a reports row."""
    if not rob:
        return {
            "rob_label": "—",
            "rob_oos_sharpe_fmt": "—",
            "rob_oos_pnl_fmt": "—",
            "rob_mc_median_fmt": "—",
            "rob_mc_dd_p95_fmt": "—",
            "rob_wf_pass_fmt": "—",
            # raw for sorting
            "rob_oos_sharpe": None,
            "rob_mc_median": None,
        }

    sp = rob.get("in_out_split") or {}
    mc = rob.get("monte_carlo") or {}
    wf = rob.get("walk_forward") or []

    oos_m = sp.get("oos_metrics") or {}
    oos_sharpe = oos_m.get("sharpe")
    oos_pnl = oos_m.get("pnl")

    mc_median = mc.get("median_final")
    mc_dd_p95 = mc.get("max_dd_p95")

    # Walk-forward pass rate: in how many windows test PnL > 0
    wf_total = len(wf)
    wf_pass = sum(1 for w in wf if (w.get("test_metrics") or {}).get("pnl", 0) > 0)
    if wf_total > 0:
        wf_pass_fmt = f"{wf_pass}/{wf_total}"
    else:
        wf_pass_fmt = "—"

    # Overfitting label → badge class
    label = sp.get("overfitting_label") or "—"

    return {
        "rob_label": label,
        "rob_oos_sharpe_fmt": fmt_num(oos_sharpe, 2),
        "rob_oos_pnl_fmt": fmt_money(oos_pnl, signed=True),
        "rob_mc_median_fmt": fmt_money(mc_median),
        "rob_mc_dd_p95_fmt": fmt_pct(
            mc_dd_p95 / 100 if mc_dd_p95 is not None else None, 2
        ),
        "rob_wf_pass_fmt": wf_pass_fmt,
        # raw for JS sort
        "rob_oos_sharpe": oos_sharpe,
        "rob_mc_median": mc_median,
    }


# H6: parse cache keyed by (mtime_ns, size) — if the log hasn't changed, ~3.6k
# json.loads calls aren't repeated; each visit shouldn't re-pay the full cost.
_PARSE_CACHE: dict[str, tuple[tuple, list]] = {}


def _parsed_log_records() -> list[dict]:
    try:
        st = BACKTEST_LOG.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    cached = _PARSE_CACHE.get("backtest_log")
    if cached and cached[0] == key:
        return cached[1]
    recs: list[dict] = []
    with open(BACKTEST_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    _PARSE_CACHE["backtest_log"] = (key, recs)
    return recs


def _load_rows(
    runner: str = "", symbol: str = "", errors_only: bool = False, capital: str = ""
) -> list[dict]:
    if not BACKTEST_LOG.exists():
        return []

    rob_index = _load_robustness_index()

    rows: list[dict] = []
    if True:
        for rec in _parsed_log_records():
            m = rec.get("metrics") or {}
            bi = rec.get("bars") or {}
            spec = rec.get("spec") or {}
            rationale = rec.get("rationale") or ""
            error = rec.get("error") or ""

            run_symbol = bi.get("symbol") or bi.get("ticker") or ""
            run_runner = _runner_label(rationale)

            if runner and run_runner != runner:
                continue
            if symbol and symbol.upper() not in run_symbol.upper():
                continue
            if errors_only and not error:
                continue
            if capital:
                cash = m.get("starting_cash")
                # exact value match like "10000" (with float tolerance).
                # Non-numeric capital → ignore the filter (instead of
                # eliminating all rows and silently returning an empty table).
                try:
                    cap_val = float(capital)
                except (TypeError, ValueError):
                    cap_val = None
                if cap_val is not None:
                    try:
                        if cash is None or abs(float(cash) - cap_val) > 0.01:
                            continue
                    except (TypeError, ValueError):
                        continue

            pnl = m.get("pnl")
            max_dd = m.get("max_dd")
            sharpe = m.get("sharpe")
            sortino = m.get("sortino")
            win_rate = m.get("win_rate")
            profit_factor = m.get("profit_factor")
            volatility = m.get("volatility")
            commission = m.get("commission_total")
            n_trades = m.get("n_trades") or 0
            avg_dur = m.get("avg_duration_mins")
            starting_cash = m.get("starting_cash")

            spec_id = spec.get("id", "")
            spec_name = spec.get("name", "—")

            # First the composite key (spec+symbol+TF); for legacy records the
            # single spec_id/spec_name fallback (last-writer-wins) is kept.
            bars_bi = rec.get("bars") or {}
            _sym = (
                bars_bi.get("symbol")
                or bars_bi.get("ticker")
                or bars_bi.get("instrument_id")
                or ""
            )
            _iv = bars_bi.get("interval") or bars_bi.get("granularity") or ""
            rob = (
                rob_index.get((spec_id, _sym, _iv))
                or rob_index.get(spec_id)
                or rob_index.get(spec_name)
            )
            # L8: fallback identity-consistency guard — since the plain
            # spec_id/spec_name key is last-writer-wins, ANOTHER symbol/TF's
            # robustness could stick to the row. If the record HAS identity
            # fields AND they conflict with the row's, the fallback is rejected
            # (identity-less legacy records pass unconditionally).
            if rob is not None:
                _r_sym = rob.get("symbol") or ""
                _r_iv = rob.get("interval") or ""
                if (_r_sym and _sym and _r_sym != _sym) or (
                    _r_iv and _iv and _r_iv != _iv
                ):
                    rob = None
            rob_data = _rob_fields(rob)

            row = {
                # identity — ts converted to local time for display
                "ts": _utc_to_local(rec.get("ts", "")),
                # raw UTC ts: /reports/detail finds the log line with this
                "ts_raw": rec.get("ts", ""),
                "spec_id": spec_id,
                "spec_name": spec_name,
                "blocks": spec.get("blocks", []),
                "instrument": rec.get("instrument", "—"),
                "symbol": run_symbol,
                "category": bi.get("category", ""),
                "interval": str(bi.get("interval", "")),
                "runner": run_runner,
                "rationale": rationale,
                "error": error,
                # metrics raw (for sorting)
                "pnl": pnl,
                "max_dd": max_dd,
                "sharpe": sharpe,
                "sortino": sortino,
                "win_rate": win_rate,
                "n_trades": n_trades,
                "starting_cash": starting_cash,
                # metrics formatted
                "pnl_fmt": fmt_money(pnl, signed=True),
                "pnl_pct_fmt": fmt_pct(m.get("pnl_pct"), 3),
                "max_dd_fmt": fmt_pct(max_dd, 2),
                "sharpe_fmt": fmt_num(sharpe, 2),
                "sortino_fmt": fmt_num(sortino, 2),
                "profit_factor_fmt": fmt_num(profit_factor, 2),
                "win_rate_fmt": fmt_pct(win_rate, 2),
                "volatility_fmt": fmt_pct(volatility, 2),
                "commission_fmt": fmt_money(commission),
                "starting_cash_fmt": fmt_money(starting_cash),
                "avg_dur_fmt": fmt_dur(avg_dur),
                "test_period_fmt": _fmt_test_period(
                    bi.get("start", ""), bi.get("end", "")
                ),
                "elapsed_fmt": _fmt_elapsed(rec.get("elapsed_sec")),
                "n_wins": m.get("n_wins") or 0,
                "n_losses": m.get("n_losses") or 0,
            }
            row.update(rob_data)
            rows.append(row)

    rows.reverse()
    return rows


def _all_symbols() -> list[str]:
    # H6: from the parse cache — no extra full scan of the log.
    if not BACKTEST_LOG.exists():
        return []
    symbols: set[str] = set()
    for rec in _parsed_log_records():
        bi = rec.get("bars") or {}
        sym = bi.get("symbol") or bi.get("ticker") or ""
        if sym:
            symbols.add(sym)
    return sorted(symbols)


def _all_capitals() -> list[float]:
    """Distinct starting_cash values in the log (for the filter dropdown).

    H6: from the parse cache — no extra full scan of the log.
    """
    if not BACKTEST_LOG.exists():
        return []
    caps: set[float] = set()
    for rec in _parsed_log_records():
        try:
            c = (rec.get("metrics") or {}).get("starting_cash")
            if c is not None:
                caps.add(float(c))
        except (TypeError, ValueError):
            pass
    return sorted(caps)


def _page_data(
    runner: str, symbol: str, errors_only: bool, capital: str
) -> tuple[list, list, list]:
    """H6: rows + symbols + capitals in a SINGLE synchronous function — run
    via asyncio.to_thread without blocking the event loop. (Old way: 3 full
    log scans on the loop thread inside an async handler → server-wide freeze
    on logs approaching 20MB; every request, including 1s HTMX polls, waited.)
    """
    rows = _load_rows(
        runner=runner, symbol=symbol, errors_only=errors_only, capital=capital
    )
    return rows, _all_symbols(), _all_capitals()


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    from server import get_market_info, templates

    runner_filter = request.query_params.get("runner", "")
    symbol_filter = request.query_params.get("symbol", "")
    errors_only = request.query_params.get("errors_only", "") == "1"
    capital_filter = request.query_params.get("capital", "")

    rows, symbols, capitals = await asyncio.to_thread(
        _page_data, runner_filter, symbol_filter, errors_only, capital_filter
    )

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "active": "reports",
            "page_title": "Reports",
            "market": get_market_info(),
            "rows": rows,
            "total": len(rows),
            "runner_filter": runner_filter,
            "symbol_filter": symbol_filter,
            "errors_only": errors_only,
            "capital_filter": capital_filter,
            "symbols": symbols,
            "capitals": capitals,
            "column_layout": _load_layout(),
        },
    )


_DETAIL_CACHE: dict[str, dict] = {}  # raw ts → render context (cap: 8, FIFO)
_DETAIL_CACHE_MAX = 8


def _find_log_record(ts: str) -> dict | None:
    """Find the line with the raw UTC ts in backtest_log.jsonl (active file)."""
    if not BACKTEST_LOG.exists():
        return None
    with open(BACKTEST_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or f'"{ts}"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts") == ts:
                return rec
    return None


def _detail_error(msg: str) -> HTMLResponse:
    # L7: callers pass raw exception / spec-validation text — printing it
    # unescaped would break the DOM / open injection in LLM-sourced text. Since
    # it's a single choke point, all callers are fixed here at once.
    from markupsafe import escape

    return HTMLResponse(f"<div class='empty-state'>{escape(msg)}</div>")


@router.get("/detail", response_class=HTMLResponse)
async def detail(request: Request, ts: str):
    """Trade detail of a report row: deterministic re-run + chart.

    Using the full spec + data identity (symbol/interval/start/end) in the log
    line, it is replayed exactly in the backtest killable sandbox child; the
    resulting reasoned trade list + price chart is returned as a fragment.
    """
    import asyncio

    from server import templates

    if ts in _DETAIL_CACHE:
        return templates.TemplateResponse(
            request, "fragments/report_detail.html", _DETAIL_CACHE[ts]
        )

    # H6: full log scan in a thread — loop is not blocked.
    rec = await asyncio.to_thread(_find_log_record, ts)
    if rec is None:
        return _detail_error(
            "Record not found — the log may have been rotated (.jsonl.1 archive)."
        )
    if rec.get("error"):
        return _detail_error(f"This run had ended with an error: {rec['error'][:200]}")
    bi = rec.get("bars") or {}
    symbol = bi.get("symbol")
    if not symbol:
        return _detail_error(
            "Chart detail is currently only available for Bybit runs "
            "(External/Index records are not supported)."
        )

    from composer import ComposedStrategySpec

    try:
        spec = ComposedStrategySpec.from_dict(rec.get("spec") or {})
    except Exception as e:
        return _detail_error(f"Spec could not be built from the log record: {e}")
    verr = spec.validate()
    if verr:
        return _detail_error(
            f"Spec can no longer be run: {verr} (may be a deleted custom block)."
        )

    category = bi.get("category", "linear")
    interval = str(bi.get("interval", "60"))

    def _rerun():
        import pandas as pd

        from data import load_bybit_bars
        from sandbox import run_backtest_guarded

        start = pd.Timestamp(bi["start"]).to_pydatetime() if bi.get("start") else None
        end = pd.Timestamp(bi["end"]).to_pydatetime() if bi.get("end") else None
        bars = load_bybit_bars(
            symbol=symbol,
            interval=interval,
            category=category,
            start=start,
            end=end,
        )
        if bars.empty:
            raise RuntimeError("Data is not in the cache — fetch it from the Data page.")
        return run_backtest_guarded(
            spec,
            bars,
            recipe={"symbol": symbol, "interval": interval, "category": category},
            iteration_id=0,
            rationale="reports-detail",
            force_subprocess=True,
        )

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _rerun)
    except Exception as e:
        return _detail_error(f"Re-run failed: {type(e).__name__}: {e}")
    if result.error:
        return _detail_error(f"Re-run error: {result.error}")

    # Fidelity: exactly matches the metrics in the log? (Legacy records fill
    # missing spec fields with defaults — deviation is normal, it is flagged.)
    old_m = rec.get("metrics") or {}
    new_m = result.metrics or {}
    old_n, new_n = old_m.get("n_trades"), new_m.get("n_trades")
    old_pnl, new_pnl = old_m.get("pnl"), new_m.get("pnl")
    fidelity_ok = (
        old_n == new_n
        and old_pnl is not None
        and new_pnl is not None
        and abs(float(old_pnl) - float(new_pnl)) < 0.01
    )

    from web.shared import chart_url as _chart_url

    chart_url = _chart_url(result.bars_info or bi, spec.id)
    # _chart_url automatically increases the TF over a long range (e.g. 6y → D) —
    # let the button highlight show the TF ACTUALLY drawn on the chart
    m = re.search(r"[?&]interval=([^&]+)", chart_url)
    chart_tf = m.group(1) if m else interval

    ctx = {
        "ts": ts,
        "spec_name": rec.get("spec", {}).get("name", "—"),
        "fidelity_ok": fidelity_ok,
        "old_n": old_n,
        "new_n": new_n,
        "old_pnl": old_pnl,
        "new_pnl": new_pnl,
        "trades": result.trades or [],
        "metrics": new_m,
        "chart_url": chart_url,
        "chart_symbol": symbol,
        "chart_category": category,
        "chart_interval": chart_tf,
        "chart_dom_id": "price-chart-" + ts.replace(":", "").replace(".", "")[-12:],
        "last": {
            "spec_name": rec.get("spec", {}).get("name", "—"),
            "params": {"spec_id": spec.id},
            "trades": result.trades or [],
        },
    }
    while len(_DETAIL_CACHE) >= _DETAIL_CACHE_MAX:
        _DETAIL_CACHE.pop(next(iter(_DETAIL_CACHE)), None)
    _DETAIL_CACHE[ts] = ctx
    return templates.TemplateResponse(request, "fragments/report_detail.html", ctx)


@router.get("/layout", response_class=JSONResponse)
async def get_layout():
    """Return the saved column layout (empty dict if none)."""
    return _load_layout()


@router.post("/layout", response_class=JSONResponse)
async def post_layout(request: Request):
    """Persist the column layout (order + hidden)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)
    _save_layout(body)
    return {"ok": True}


@router.get("/export.csv")
async def export_csv(request: Request):
    runner_filter = request.query_params.get("runner", "")
    symbol_filter = request.query_params.get("symbol", "")
    errors_only = request.query_params.get("errors_only", "") == "1"
    capital_filter = request.query_params.get("capital", "")

    # H6: full log scan in a thread — loop is not blocked.
    rows = await asyncio.to_thread(
        _load_rows,
        runner=runner_filter,
        symbol=symbol_filter,
        errors_only=errors_only,
        capital=capital_filter,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Date",
            "Test Period",
            "Strategy",
            "Symbol",
            "Capital",
            "Category",
            "Interval",
            "Runner",
            "PnL (USDT)",
            "PnL (%)",
            "Max DD",
            "Sharpe",
            "Sortino",
            "Profit Factor",
            "Win Rate",
            "N Trades",
            "N Wins",
            "N Losses",
            "Avg Duration",
            "Volatility",
            "Commission",
            "Overfitting",
            "OOS Sharpe",
            "OOS PnL",
            "MC Median",
            "MC DD p95",
            "WF Pass",
            "Error",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["ts"],
                r["test_period_fmt"],
                r["spec_name"],
                r["symbol"],
                r["starting_cash_fmt"],
                r["category"],
                r["interval"],
                r["runner"],
                r["pnl_fmt"],
                r["pnl_pct_fmt"],
                r["max_dd_fmt"],
                r["sharpe_fmt"],
                r["sortino_fmt"],
                r["profit_factor_fmt"],
                r["win_rate_fmt"],
                r["n_trades"],
                r["n_wins"],
                r["n_losses"],
                r["avg_dur_fmt"],
                r["volatility_fmt"],
                r["commission_fmt"],
                r["rob_label"],
                r["rob_oos_sharpe_fmt"],
                r["rob_oos_pnl_fmt"],
                r["rob_mc_median_fmt"],
                r["rob_mc_dd_p95_fmt"],
                r["rob_wf_pass_fmt"],
                r["error"][:80] if r["error"] else "",
            ]
        )

    content = output.getvalue()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=backtest_reports.csv"},
    )
