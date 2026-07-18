"""Autonomous Backtest Agent — research loop.

Pipeline:
  0. Load data (full range from cache)
  1. Generate strategy (Claude → ComposedStrategySpec)
  2. N × (backtest → Claude refinement)
  3. Ranking (Sharpe + PnL + WinRate score)
  4. Robustness scan (in order: IS/OOS + WFO + MC)
  5. Save the winner (catalog + log)

Endpoints:
    GET  /agent               Page
    POST /agent/run           Start pipeline (returns immediately)
    GET  /agent/progress/{id} Status polling (HTMX every 1s)
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/agent")

from web.shared import ProgressStore  # noqa: E402
from web.shared import chart_url as _chart_url  # noqa: E402
from web.shared import log_backtest as _log_backtest  # noqa: E402
from web.shared import log_robustness as _log_robustness  # noqa: E402

# ProgressStore holds dict + lock + capped eviction; aliases keep the many
# existing direct-access sites (worker ↔ pollers ↔ session log) unchanged.
_AGENT_STORE = ProgressStore(50)
_AGENT_PROGRESS = _AGENT_STORE.raw()
_AGENT_LOCK = _AGENT_STORE.lock
# Minimum number of trades to be considered statistically reliable. Runs below
# this are eliminated with -inf in _score (cannot be a winner). The value is
# aligned with the NAU_ev backtest optimizer's JUNK_MIN_TRADES=20 threshold (see
# NAU wiki backtest-optimizer.md: "trade < 20 → eliminated + not stored").
# L28: adjustable via AGENT_MIN_TRADES env var; default 20 → behavior unchanged.
_MIN_TRADES = int(os.environ.get("AGENT_MIN_TRADES", "20"))

# L2: Monte Carlo median drawdown limit (%). Both the _robustness_passed
# comparison and the explanation text shown to the user derive from this SINGLE
# constant.
_MC_DD_LIMIT = -25.0

# L32: Sealed holdout — the last N days of data are completely withheld from the
# iteration + robustness phases; tested exactly once only AFTER the winner is
# declared. The result is NOT BOUND to the decision, shown for information only.
OOS_HOLDOUT_DAYS = 60

# M22 extra circuit breaker: threshold of consecutive winnerless rounds
# (continuous mode). Counter resets when a winner appears. 0 = off; default 25.
_WINLESS_ROUND_LIMIT = int(os.environ.get("AGENT_WINLESS_ROUND_LIMIT", "25"))

# L38: model → ($/MTok input, output, cache_read, cache_write). Selected by
# agent.MODEL; unknown model falls back to Sonnet rates. If the backend is
# claude-cli / OAuth (subscription), there is NO per-token billing → cost None
# (UI hides it).
_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5": (10.0, 50.0, 1.00, 12.50),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
}
_DEFAULT_PRICING_MODEL = "claude-sonnet-4-6"

# L26: lightweight best-effort SQLite index of winner + robustness moments.
_AGENT_INDEX_DB = Path.home() / ".cache" / "nautilus_web_app" / "agent_index.db"

# When this module runs inside a sandbox child process (robustness offloaded to
# a subprocess so it can't freeze the web server), progress steps are relayed to
# the parent via this queue instead of the child's own _AGENT_PROGRESS.
_IPC_Q = None

# ── Session Logger ────────────────────────────────────────────────────────────
SESSION_LOG_DIR = Path.home() / ".cache" / "nautilus_web_app" / "agent_sessions"
_SESSION_LOG_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOG_META: threading.Lock = threading.Lock()  # guards _SESSION_LOG_LOCKS


def _json_safe(obj):
    """Reduces NaN/Inf floats to None (recursive) — prevents json.dumps from
    producing non-standard ``NaN``/``Infinity`` literals (part of L33).
    Small helper specific to this file; independent of the one in reports.
    """
    if isinstance(obj, float):  # np.float64 is also a float subclass
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _session_log(run_id: str, event: str, **kwargs) -> None:
    """Append a JSON event line to agent_sessions/{run_id}.jsonl.
    Thread-safe per run_id. Silently ignores all errors so logging never
    breaks the agent worker.
    """
    try:
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _SESSION_LOG_META:
            if run_id not in _SESSION_LOG_LOCKS:
                _SESSION_LOG_LOCKS[run_id] = threading.Lock()
            lock = _SESSION_LOG_LOCKS[run_id]
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "run_id": run_id,
            **kwargs,
        }
        line = json.dumps(_json_safe(record), default=str) + "\n"
        with lock:
            with (SESSION_LOG_DIR / f"{run_id}.jsonl").open("a") as _f:
                _f.write(line)
    except Exception:
        pass


def _cleanup_all_agent_blocks() -> None:
    """Legacy hook kept for compatibility; agent blocks are now persistent."""
    return None


_PHASES = [
    "Loading data",
    "Generating strategy",
    "Backtest loop",
    "Ranking",
    "Robustness scan",
    "Completed",
]


# ── Progress helpers ──────────────────────────────────────────────────────────


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _set_phase(run_id: str, idx: int, detail: str = "") -> None:
    t = _ts()
    label = _PHASES[idx] if 0 <= idx < len(_PHASES) else str(idx)
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if not s:
            return
        for i, p in enumerate(s["phases"]):
            if i < idx:
                p["status"] = "done"
            elif i == idx:
                p["status"] = "running"
                p["detail"] = detail
                p["ts"] = t
            else:
                p["status"] = "pending"
                p["detail"] = ""
    _session_log(
        run_id,
        "phase_change",
        phase_idx=idx,
        phase_label=label,
        status="running",
        detail=detail,
    )


def _done_phase(run_id: str, idx: int, detail: str = "") -> None:
    t = _ts()
    label = _PHASES[idx] if 0 <= idx < len(_PHASES) else str(idx)
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if not s:
            return
        p = s["phases"][idx]
        p["status"] = "done"
        p["detail"] = detail
        p["ts"] = t
    _session_log(
        run_id,
        "phase_change",
        phase_idx=idx,
        phase_label=label,
        status="done",
        detail=detail,
    )


def _add_step(run_id: str, msg: str) -> None:
    # In a sandbox child, relay the step to the parent (tag matches _run_in_child).
    if _IPC_Q is not None:
        try:
            _IPC_Q.put(("progress", msg))
        except Exception:
            pass
        return
    t = _ts()
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["steps"].append({"ts": t, "msg": msg})
            # Cap to prevent unbounded memory growth in continuous mode.
            if len(s["steps"]) > 500:
                s["steps"] = s["steps"][-500:]
    _session_log(run_id, "step", ts=t, msg=msg)


# ── Timeline (Gantt) span track ───────────────────────────────────────────────
# Every meaningful operation (data loading, LLM call, backtest #i, robustness
# candidate and its sub-phases) is recorded as a "span"; the SVG timeline on the
# /agent screen and the /sessions replay are fed from this track. Epoch floats
# are carried inside the payload — never via the `ts=` kwarg (which would
# overwrite _session_log's ISO ts).

_TL_MAX_SPANS = 400  # continuous-mode memory ceiling (same spirit as 500-step cap)
_TL_LANES = ("data", "llm", "backtest", "robustness")


def _tl_begin(
    run_id: str,
    lane: str,
    key: str,
    label: str,
    *,
    sub: bool = False,
    round_num: int = 1,
    **meta,
) -> None:
    """Open a span in the timeline. No-op inside a sandbox child (_IPC_Q set) —
    child sub-phases are derived on the parent side from step markers."""
    if _IPC_Q is not None:
        return
    t0 = datetime.now(UTC).timestamp()
    span = {
        "key": key,
        "lane": lane,
        "label": label,
        "t0": t0,
        "t1": None,
        "status": "running",
        "sub": sub,
        "round": round_num,
        "meta": dict(meta),
    }
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            tl = s.setdefault("timeline", [])
            tl.append(span)
            if len(tl) > _TL_MAX_SPANS:
                # Drop the oldest CLOSED spans; open (running) ones are kept.
                closed_idx = next(
                    (i for i, sp in enumerate(tl) if sp["t1"] is not None), None
                )
                if closed_idx is not None:
                    tl.pop(closed_idx)
                else:
                    tl.pop(0)
    _session_log(
        run_id,
        "timeline",
        op="begin",
        key=key,
        lane=lane,
        label=label,
        t0=t0,
        sub=sub,
        round=round_num,
        meta=dict(meta),
    )


def _tl_end(run_id: str, key: str, *, status: str = "ok", **meta) -> None:
    """Close the most recent open span with `key`. Unknown/closed key → no-op."""
    if _IPC_Q is not None:
        return
    t1 = datetime.now(UTC).timestamp()
    found = False
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            for sp in reversed(s.get("timeline") or []):
                if sp["key"] == key and sp["t1"] is None:
                    sp["t1"] = t1
                    sp["status"] = status
                    if meta:
                        sp["meta"].update(meta)
                    found = True
                    break
    if found:
        _session_log(
            run_id, "timeline", op="end", key=key, t1=t1, status=status, meta=dict(meta)
        )


def _tl_close_open(run_id: str, *, status: str = "fail") -> None:
    """Close all spans left open (error / stop / end-of-round cleanup)."""
    if _IPC_Q is not None:
        return
    t1 = datetime.now(UTC).timestamp()
    closed: list[str] = []
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            for sp in s.get("timeline") or []:
                if sp["t1"] is None:
                    sp["t1"] = t1
                    sp["status"] = status
                    closed.append(sp["key"])
    for key in closed:
        _session_log(
            run_id, "timeline", op="end", key=key, t1=t1, status=status, meta={}
        )


# Robustness sub-phase markers — the FIXED leading glyphs of the _add_step
# messages in _run_full_robustness. Do not change the glyphs: _make_rob_progress
# parses them to open/close sub-spans.
_TL_ROB_MARKERS = {
    "🌐": ("ms", "Multi-Symbol"),
    "📊": ("isoos", "IS/OOS"),
    "📈": ("wfo", "Walk-Forward"),
    "🎲": ("mc", "Monte Carlo"),
}


def _make_rob_progress(run_id: str, cand_idx: int, round_num: int):
    """Step + sub-span bridge for the robustness child's progress relay.

    The returned function forwards each message to _add_step; on the 4 fixed
    phase glyphs (🌐 📊 📈 🎲) it opens a sub-span (closing the previous one with
    ok); a result line starting with "  →" closes the active sub with ok, and
    "  ⚠ Monte Carlo skipped" closes mc with warn.
    """
    state = {"open": None}  # active sub-span key

    def _close(status: str = "ok") -> None:
        if state["open"]:
            _tl_end(run_id, state["open"], status=status)
            state["open"] = None

    def progress(msg: str) -> None:
        _add_step(run_id, msg)
        head = msg.lstrip()[:1]
        if head in _TL_ROB_MARKERS:
            _close("ok")
            slug, label = _TL_ROB_MARKERS[head]
            key = f"rob-r{round_num}-c{cand_idx}-{slug}"
            _tl_begin(
                run_id,
                "robustness",
                key,
                label,
                sub=True,
                round_num=round_num,
            )
            state["open"] = key
        elif msg.startswith("  →"):
            _close("ok")
        elif msg.startswith("  ⚠ Monte Carlo skipped"):
            _close("warn")

    progress.close_open = _close  # called at candidate close
    return progress


def _add_tokens(run_id: str, usage: dict | None) -> None:
    """Accumulate token counters from the Claude API usage dict."""
    if not usage:
        return
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["tokens_in"] = s.get("tokens_in", 0) + (usage.get("input_tokens") or 0)
            s["tokens_out"] = s.get("tokens_out", 0) + (usage.get("output_tokens") or 0)
            s["tokens_cache_read"] = s.get("tokens_cache_read", 0) + (
                usage.get("cache_read_input_tokens") or 0
            )
            s["tokens_cache_write"] = s.get("tokens_cache_write", 0) + (
                usage.get("cache_creation_input_tokens") or 0
            )


def _set_robustness_scan(run_id: str, current: int, total: int) -> None:
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s is not None:
            s["rob_scan_current"] = current
            s["rob_scan_total"] = total


# ── Core helpers ──────────────────────────────────────────────────────────────


def _proposal_to_spec(proposal: dict):
    """Convert propose_composed_strategy output to ComposedStrategySpec."""
    from composer import ComposedStrategySpec, SignalBlock, new_spec_id

    opts = proposal.get("strategy_options") or {}
    blocks = [
        SignalBlock(type=b["type"], role=b["role"], params=b.get("params", {}))
        for b in proposal.get("blocks", [])
    ]
    return ComposedStrategySpec(
        id=new_spec_id(),
        name=proposal.get("name", "Agent Strategy"),
        description=proposal.get("description", ""),
        blocks=blocks,
        trade_size=float(opts.get("trade_size", 0.01)),
        entry_logic=opts.get("entry_logic", "OR"),
        exit_logic=opts.get("exit_logic", "OR"),
        order_type="market",  # agent always uses market — limit rarely fills on backtests
        limit_offset_bps=0.0,
        use_bracket=bool(opts.get("use_bracket", False)),
        sl_type=opts.get("sl_type", "percent"),
        sl_value=float(opts.get("sl_value", 2.0)),
        tp_type=opts.get("tp_type", "off"),
        tp_value=float(opts.get("tp_value", 4.0)),
        atr_period=int(opts.get("atr_period", 14)),
        allow_short=bool(opts.get("allow_short", False)),
        trade_size_mode=opts.get("trade_size_mode", "fixed"),
        trade_size_percent=float(opts.get("trade_size_percent", 5.0)),
        trade_size_atr_risk=float(opts.get("trade_size_atr_risk", 1.0)),
        trade_size_usdt=float(opts.get("trade_size_usdt", 1000.0)),
    )


_STARTING_CASH: float | None = None
_PNL_FALLBACK_WARNED = False


def _starting_cash() -> float:
    """Lazily fetch the STARTING_CASH constant (keep the module import light).

    Contract: try ``app_constants`` first (a parallel refactor is creating this
    module); on ImportError fall back to the existing ``backtest`` source.
    """
    global _STARTING_CASH
    if _STARTING_CASH is None:
        try:
            from app_constants import STARTING_CASH
        except ImportError:
            from backtest import STARTING_CASH
        _STARTING_CASH = float(STARTING_CASH)
    return _STARTING_CASH


def _score(result) -> float:
    """NAU composite ranking score (H9/M30/M32):

        calmar = clamp(pnl_pct / max(|max_dd|, 0.01), -10, 10)
        base   = 0.7 × calmar + 0.3 × clamp(sharpe_per_trade, -10, 10)
        score  = base × n_trades / (n_trades + 20)      ← confidence multiplier

    NAU parity: the sharpe term is PER-TRADE sharpe ((mean/std)×√n, NAU
    backtest.py:89) — NOT the annualized 252-day Sharpe. NAU's fold_quality
    composite also reads m['sharpe'] on a per-trade basis; here the annualized
    'sharpe' is a separate field, so sharpe_per_trade is used explicitly here.

    - pnl_pct and max_dd are in the FRACTION convention (0.1 = 10%; max_dd < 0 is
      healthy).
    - The old WinRate×0.2 term and the fixed 0.1 confidence bonus are REMOVED:
      WR info is already embedded in PnL/Calmar, and the fixed bonus was
      distorting the ranking.
    - The overtrading log-penalty is DELIBERATELY kept: if n_trades > 2000,
      -0.3×log10(n/2000) is added to the score (suppresses commission-heavy
      noise strategies; also necessary because the confidence multiplier
      saturates to 1 at large n).

    JUNK elimination → -inf (aligned with the NAU backtest optimizer):
    error OR < _MIN_TRADES trades OR degenerate drawdown. The NAU rule is
    ``trade < 20 OR max_drawdown <= 0 → eliminated``. In this project max_dd is
    in the negative convention (healthy = <0); 0/NaN/missing = no real drawdown =
    data/computation degeneration, so ``max_dd >= 0`` is the mirror of NAU's
    ``<= 0``.
    """
    global _PNL_FALLBACK_WARNED
    if result.error:
        return float("-inf")
    m = result.metrics
    if not m or (m.get("n_trades") or 0) < _MIN_TRADES:
        return float("-inf")
    max_dd = m.get("max_dd")
    # NAU: max_drawdown <= 0 → junk (degenerate). M1: NaN is also degenerate.
    if max_dd is None or math.isnan(max_dd) or max_dd >= 0:
        return float("-inf")
    n_trades = m.get("n_trades") or 0
    # NAU parity: the composite's 0.3 term is PER-TRADE sharpe. Annualized
    # 'sharpe' (252-day) is on a different scale; NAU uses per-trade
    # ((mean/std)×√n).
    sharpe = m.get("sharpe_per_trade")
    if sharpe is None:
        sharpe = m.get("sharpe") or 0.0  # backward-compat (old metrics dicts)
    # Use explicit None check to avoid treating pnl_pct=0.0 (break-even) as missing
    _pnl_pct = m.get("pnl_pct")
    if _pnl_pct is not None:
        pnl_pct = _pnl_pct
    else:
        # M12+L1: pnl is absolute USDT — divide by STARTING_CASH to convert to
        # fraction (scale-correct fallback). Warn once: a missing pnl_pct
        # indicates a regression in the metrics producer.
        pnl_pct = (m.get("pnl") or 0.0) / _starting_cash()
        if not _PNL_FALLBACK_WARNED:
            _PNL_FALLBACK_WARNED = True
            logging.warning(
                "_score: no pnl_pct in metrics — used pnl/STARTING_CASH "
                "fallback (logged once)"
            )
    if math.isnan(sharpe) or math.isinf(sharpe):
        sharpe = 0.0
    if math.isnan(pnl_pct) or math.isinf(pnl_pct):
        pnl_pct = 0.0
    calmar = pnl_pct / max(abs(max_dd), 0.01)
    calmar = max(-10.0, min(10.0, calmar))
    base = 0.7 * calmar + 0.3 * max(-10.0, min(10.0, sharpe))
    # Confidence multiplier: continuously reduces the score of low-trade results
    # (n=20 → ×0.5, n=180 → ×0.9); replaces the old stepwise 0.1 bonus.
    score = base * (n_trades / (n_trades + 20))
    # Documented exception: overtrading log penalty (see docstring).
    if n_trades > 2000:
        score += -0.3 * math.log10(n_trades / 2000)
    return score


def _rank_results(results: list[tuple]) -> list[tuple]:
    """Sort (spec, IterationResult) pairs by composite score descending."""
    return sorted(results, key=lambda x: _score(x[1]), reverse=True)


# Liquid peer basket for multi-symbol robustness on external (US equity) runs.
# Real instrument ids from the catalog — SPY/IWM are on the ARCA venue, not NASDAQ.
EXTERNAL_PEER_BASKET = [
    "SPY.ARCA",
    "QQQ.NASDAQ",
    "IWM.ARCA",
    "AAPL.NASDAQ",
    "MSFT.NASDAQ",
]


def _clamp_spec_trade_size(spec):
    """Equity (size_precision=0) requires integer shares — the fractional crypto
    trade_size the agent produces (0.01) rounds to 0 shares and yields 0 trades.
    Since spec is a fresh object, in-place mutation is safe; the robustness phase
    uses the same object too, so fixing it at a single point covers every consumer.
    """
    if float(spec.trade_size) < 1:
        spec.trade_size = 1.0
    return spec


def _robustness_passed(
    rob: dict, strict: bool = True, run_id: str | None = None
) -> bool:
    """Robustness decision — with an evaluated-criteria counter (H4+L18).

    The 4 criteria (IS/OOS, WFO, Multi-Symbol, Monte Carlo) are each classified
    one by one as ``evaluated | failed | skipped``. The old version silently
    counted missing/faulty sections as "passed"; now:

    - strict:  at least 3 criteria must be ACTUALLY evaluated and none may be
      failed.
    - relaxed: at least 2 criteria must be evaluated, none failed
      ('⚠' labels accepted in both modes — not counted as failed).

    For each skipped criterion (if run_id is given) a warning
    '⚠ <criterion> could not be evaluated: <reason>' is added to the step log.
    """
    if not rob or rob.get("error"):
        return False

    def _skip(name: str, why: str) -> None:
        if run_id:
            _add_step(run_id, f"⚠ {name} could not be evaluated: {why}")

    evaluated = 0
    failed = 0

    # 1) IS/OOS overfitting criterion
    split = rob.get("split") or {}
    split_label = split.get("overfitting_label", "")
    if split.get("error") or not split_label:
        _skip("IS/OOS", str(split.get("error") or "no label"))
    else:
        evaluated += 1
        # The "robust" and "caution" labels are accepted; a '✗' or the
        # 'yetersiz' (insufficient) marker → failed. NOTE: the label substrings
        # are produced by backtest_robustness.py (out of scope) and matched
        # verbatim here — do not translate the "yetersiz" literal below.
        if "✗" in split_label or "yetersiz" in split_label:
            failed += 1

    # 2) Walk-Forward: ≥50% valid windows with positive test PnL.
    # Windows with <3 test trades are statistically unreliable → invalid.
    wfo = rob.get("wfo_windows") or []
    valid_windows = [
        w
        for w in wfo
        if (w.get("test_n_trades") or w.get("test_metrics", {}).get("n_trades", 0) or 0)
        >= 3
    ]
    if not wfo or not valid_windows:
        _skip(
            "Walk-Forward",
            "no windows" if not wfo else "no valid window with ≥3 trades",
        )
    else:
        evaluated += 1
        # M28/M594: NAU dispersion-penalized OOS Sharpe (mean − 0.5·std). The
        # manual suite produces this in wfo_aggregate; the agent path
        # (_run_full_robustness) does not call wfo_aggregate, so the field was
        # empty and this branch was dead code — compute it INLINE from
        # wfo_windows here. Otherwise fall back to the positive-window ratio.
        pen = rob.get("oos_sharpe_penalized")
        if pen is None:
            _sh = [
                float((w.get("test_metrics") or {}).get("sharpe"))
                for w in valid_windows
                if (w.get("test_metrics") or {}).get("sharpe") is not None
                and math.isfinite(
                    (w.get("test_metrics") or {}).get("sharpe", float("nan"))
                )
            ]
            if len(_sh) >= 2:
                _m = sum(_sh) / len(_sh)
                _var = sum((s - _m) ** 2 for s in _sh) / len(_sh)
                pen = _m - 0.5 * (_var**0.5)
        try:
            pen = float(pen) if pen is not None else None
            if pen is not None and not math.isfinite(pen):
                pen = None
        except (TypeError, ValueError):
            pen = None
        if pen is not None:
            wfo_failed = pen <= 0
        else:
            positive = sum(
                1
                for w in valid_windows
                if (w.get("test_metrics") or {}).get("pnl", 0) > 0
            )
            wfo_failed = positive / len(valid_windows) < 0.5
        if wfo_failed:
            failed += 1

    # 3) Multi-symbol generalizability
    ms = rob.get("multi_symbol") or {}
    ms_label = ms.get("generalization_label", "")
    # 'yetersiz' substring is produced by backtest_robustness.py; kept verbatim.
    if not ms or not ms_label or ("yetersiz" in ms_label and "✗" not in ms_label):
        _skip("Multi-Symbol", "no section" if not ms else "insufficient data")
    else:
        evaluated += 1
        if "✗" in ms_label:  # symbol-specific strategy is not accepted
            failed += 1

    # 4) Monte Carlo: median drawdown check
    mc = rob.get("mc") or {}
    if not mc or mc.get("error"):
        _skip("Monte Carlo", str(mc.get("error") or "no section"))
    else:
        evaluated += 1
        dd_p50 = mc.get("max_dd_p50")
        if dd_p50 is not None and dd_p50 < _MC_DD_LIMIT:
            failed += 1

    if failed:
        return False
    min_required = 3 if strict else 2
    if evaluated < min_required:
        if run_id:
            _add_step(
                run_id,
                f"⚠ Only {evaluated}/4 criteria could be evaluated "
                f"(need ≥{min_required}) — candidate cannot pass",
            )
        return False
    return True


def _ms_score_factor(rob: dict | None) -> float:
    """M26+M31: effective-score multiplier ∈ [0.15, 1.0] from multi-symbol pass_rate.

    x = pass_rate - 0.5 (if pass_rate missing, x=0 → neutral 0.575);
    factor = 0.15 + (clamp(x, -0.5, 0.5) + 0.5) × 0.85.
    """
    ms = (rob or {}).get("multi_symbol") or {}
    pr = ms.get("pass_rate")
    # M653: on insufficient data run_multi_symbol returns pass_rate=0.0 + the
    # label '— (yetersiz veri)' — this is NOT a real 0, it means COULD NOT BE
    # EVALUATED. Feeding 0.0 into the factor computation applied the minimum
    # 0.15 penalty instead of neutral (0.575): a candidate whose MS was never
    # measured got clipped by 85% as if it lost on all symbols, losing the
    # among-passers selection. Treat insufficient-data as neutral.
    # ('yetersiz' substring is produced by backtest_robustness.py; kept verbatim.)
    ms_label = ms.get("generalization_label", "") or ""
    insufficient = ("yetersiz" in ms_label) or ms.get("n_valid", 1) == 0
    try:
        if pr is None or insufficient:
            x = 0.0  # neutral → factor 0.575
        else:
            x = float(pr) - 0.5
        if not math.isfinite(x):
            x = 0.0
    except (TypeError, ValueError):
        x = 0.0
    x = max(-0.5, min(0.5, x))
    return 0.15 + (x + 0.5) * 0.85


def _split_holdout(df, min_bars: int = 200):
    """L32: seal off the last OOS_HOLDOUT_DAYS days as a sealed slice.

    Returns ``(trimmed_df, holdout_df | None)``. If the remaining (trimmed) data
    falls below ``min_bars``, the holdout is SKIPPED and the full df is returned
    unchanged.
    """
    import pandas as pd

    from backtest_robustness import WF_EMBARGO_DAYS

    if df is None or len(df) == 0:
        return df, None
    cutoff = df.index[-1] - pd.Timedelta(days=OOS_HOLDOUT_DAYS)
    # M674: NAU purge gap (WF_EMBARGO_DAYS) between train and holdout — so
    # patterns/lookback learned around the cutoff don't leak into the first days
    # of the holdout (NAU fit_end = oos_start − WF_EMBARGO_DAYS).
    train_end = cutoff - pd.Timedelta(days=WF_EMBARGO_DAYS)
    trimmed = df[df.index < train_end]
    if len(trimmed) < min_bars:
        return df, None
    return trimmed, df[df.index >= cutoff]


_INDEX_DB_WARNED = False


def _index_insert(
    run_id: str,
    round_num: int,
    spec_name: str,
    spec_id: str,
    score,
    passed: bool,
    symbol: str,
    interval: str,
) -> None:
    """L26: best-effort SQLite index at winner + robustness moments.

    An error never breaks the worker: the first error is logged once, later ones
    are swallowed.
    """
    global _INDEX_DB_WARNED
    try:
        import sqlite3

        sc = None
        if score is not None:
            try:
                f = float(score)
                sc = f if math.isfinite(f) else None
            except (TypeError, ValueError):
                sc = None
        _AGENT_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(_AGENT_INDEX_DB), timeout=5.0)
        try:
            with con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS results ("
                    "run_id TEXT, round INTEGER, ts TEXT, spec_name TEXT, "
                    "spec_id TEXT, score REAL, passed INT, symbol TEXT, "
                    "interval TEXT)"
                )
                con.execute(
                    "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(round_num),
                        datetime.now(UTC).isoformat(),
                        spec_name,
                        spec_id,
                        sc,
                        int(bool(passed)),
                        symbol,
                        interval,
                    ),
                )
        finally:
            con.close()
    except Exception:
        if not _INDEX_DB_WARNED:
            _INDEX_DB_WARNED = True
            logging.warning("could not write agent_index.db (logged once)")


def _llm_cost_usd(ti: int, to: int, tcr: int, tcw: int) -> tuple[str, float | None]:
    """L38: returns (pricing_model, estimated cost USD).

    The model is read from agent.MODEL; the price from _MODEL_PRICING (unknown
    model → Sonnet rates). If the backend is claude-cli / OAuth subscription,
    there is NO per-token billing → cost None (UI hides the cost line). Backend
    detection mirrors agent._build_client: NAUTILUS_LLM_BACKEND=api → API;
    =claude-cli → CLI; auto → API if ANTHROPIC_API_KEY (or ~/.nautilus_proxy_key)
    exists, otherwise CLI.
    """
    try:
        from agent import MODEL as model
    except Exception:
        model = _DEFAULT_PRICING_MODEL
    pi, po, pcr, pcw = _MODEL_PRICING.get(model, _MODEL_PRICING[_DEFAULT_PRICING_MODEL])
    backend = os.environ.get("NAUTILUS_LLM_BACKEND", "auto").strip().lower()
    if backend == "claude-cli":
        return model, None
    if backend != "api":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
        if not has_key:
            try:
                has_key = (Path.home() / ".nautilus_proxy_key").exists()
            except Exception:
                has_key = False
        if not has_key:  # auto → no key → claude CLI (subscription)
            return model, None
    return model, (ti * pi + to * po + tcr * pcr + tcw * pcw) / 1_000_000


def _run_full_robustness(
    run_id: str,
    spec,
    bars_df,
    instrument,
    bar_type,
    venue,
    trades: list,
    symbol: str = "BTCUSDT",
    interval: str = "1",
    category: str = "linear",
    source: str = "bybit",
) -> dict:
    """Run robustness in the order Multi-Symbol → IS/OOS → WFO → MC.

    ``source="external"``: ``symbol`` is an external catalog instrument id
    (e.g. "QQQ.NASDAQ"), ``interval`` is the catalog DSL ("1-DAY"); the
    multi-symbol universe is chosen from EXTERNAL_PEER_BASKET.

    When NAUTILUS_PARALLEL=1 (default), independent backtest units
    (multi-symbol, IS/OOS pair, WFO window×candidate) are distributed to a
    process pool; if the pool can't be set up or a stage blows up in the pool,
    that stage is re-run on the untouched sequential path. NAUTILUS_PARALLEL=0 →
    fully sequential (old behavior).
    """
    import shutil as _shutil

    from backtest import STARTING_CASH
    from backtest_robustness import (
        run_insample_oos_split,
        run_monte_carlo,
        run_multi_symbol,
        run_walk_forward,
    )

    pf = lambda m: _add_step(run_id, m)  # noqa: E731

    # ── Parallel pool (optional) ──────────────────────────────────────────────
    pool = None
    run_many = None
    snapshot_path = None
    try:
        from parallel_exec import (
            BacktestPool,
            get_worker_count,
            make_snapshot,
            parallel_enabled,
        )

        if parallel_enabled():
            # Warm the trend-filter cache BEFORE fan-out: on a cold cache multiple
            # workers race to write the same parquet (data.py to_parquet).
            if getattr(spec, "trend_filter", False):
                try:
                    if source == "external":
                        from data import load_external_bars

                        load_external_bars(
                            symbol, getattr(spec, "trend_interval", "1-DAY")
                        )
                    else:
                        from data import load_bybit_bars

                        load_bybit_bars(
                            symbol=symbol,
                            interval=getattr(spec, "trend_interval", "60"),
                            category=category,
                            start=bars_df.index[0].to_pydatetime(),
                            end=bars_df.index[-1].to_pydatetime(),
                        )
                except Exception as warm_exc:
                    _add_step(run_id, f"  ⚠ Could not warm trend cache: {warm_exc}")
            snapshot_path = make_snapshot(bars_df)
            pool_recipe = (
                {"source": "external", "instrument_id": symbol, "granularity": interval}
                if source == "external"
                else {"symbol": symbol, "interval": interval, "category": category}
            )
            pool = BacktestPool(
                snapshot_path,
                pool_recipe,
                max_workers=get_worker_count(),
            )
            run_many = pool.run_units
            _add_step(
                run_id,
                f"⚡ Parallel mode: {pool.max_workers} worker processes "
                "(can be disabled with NAUTILUS_PARALLEL=0)",
            )
    except Exception as pool_exc:
        _add_step(run_id, f"⚠ Could not set up parallel pool ({pool_exc}) — sequential mode")
        run_many = None

    def _stage(label, fn, /, *args, **kwargs):
        """Run a robustness stage with the pool (if any); on a pool error re-run
        the same stage on the sequential path. The sequential path is always up."""
        if run_many is not None:
            try:
                return fn(*args, run_many=run_many, **kwargs)
            except Exception as par_exc:
                _add_step(
                    run_id,
                    f"  ⚠ {label} parallel stage failed "
                    f"({type(par_exc).__name__}) — re-running sequentially",
                )
        return fn(*args, **kwargs)

    try:
        # 1) Multi-Symbol — cheapest test, eliminates fast (saves IS/OOS and WFO time up front)
        if source == "external":
            from data import _external_bar_dir

            # First filter peers that HAVE data at this granularity, THEN clip to
            # 3 (scoring is already tolerant). The reverse order used to never try
            # the 4th/5th peer if the first 3 peers had no data.
            other_symbols = [
                p
                for p in EXTERNAL_PEER_BASKET
                if p != symbol and _external_bar_dir(p, interval) is not None
            ][:3]
            # 365 calendar days ≈ 252 equity bars — too few for the _MIN_TRADES threshold; use 730.
            ms_days = 730
        else:
            other_symbols = [
                s for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if s != symbol
            ]
            ms_days = 365  # 180→365: more trades → statistical reliability
        _add_step(
            run_id,
            f"🌐 Multi-Symbol — the strategy is also being tested on {', '.join(other_symbols)}. "
            "Is it generalizable or specific only to this symbol?",
        )
        ms = _stage(
            "Multi-Symbol",
            run_multi_symbol,
            spec,
            primary_symbol=symbol,
            symbols=other_symbols,
            interval=interval,
            category=category,
            days=ms_days,
            progress_fn=pf,
            source=source,
        )
        _add_step(
            run_id,
            f"  → positive on {ms.get('symbols_positive', 0)}/{ms.get('symbols_valid', 0)} symbols · "
            f"{ms.get('generalization_label', '?')}",
        )

        # 2) IS/OOS Split
        _add_step(
            run_id,
            "📊 IS/OOS Split — 70% of data for training, 30% real OOS test. "
            "The OOS/IS Sharpe ratio measures overfitting (≥0.7 = robust).",
        )
        split = _stage(
            "IS/OOS",
            run_insample_oos_split,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            split_pct=0.7,
            progress_fn=pf,
        )
        sp = split or {}
        is_m = sp.get("in_sample_metrics") or {}
        oos_m = sp.get("oos_metrics") or {}
        _add_step(
            run_id,
            f"  IS result: PnL={is_m.get('pnl', 0):+.2f} · "
            f"Sharpe={is_m.get('sharpe', float('nan')):.2f} · "
            f"{is_m.get('n_trades', 0)} trade | "
            f"OOS result: PnL={oos_m.get('pnl', 0):+.2f} · "
            f"Sharpe={oos_m.get('sharpe', float('nan')):.2f} · "
            f"{oos_m.get('n_trades', 0)} trade",
        )
        _add_step(run_id, f"  → Overfitting score: {sp.get('overfitting_label', '?')}")

        # 3) Walk-Forward
        _add_step(
            run_id,
            "📈 Walk-Forward — rolling-window OOS test. Each window has 6 months training + 2 months test. "
            "≥50% of windows must have positive PnL.",
        )
        wfo = _stage(
            "Walk-Forward",
            run_walk_forward,
            spec,
            bars_df,
            instrument,
            bar_type,
            venue,
            train_months=6,
            test_months=2,
            step_months=3,  # 2→3: ~24 windows instead of 35, 30% faster
            progress_fn=pf,
        )
        if wfo:
            pos = sum(1 for w in wfo if (w.get("test_metrics") or {}).get("pnl", 0) > 0)
            avg_pnl = sum(
                (w.get("test_metrics") or {}).get("pnl", 0) for w in wfo
            ) / len(wfo)
            _add_step(
                run_id,
                f"  → {pos}/{len(wfo)} windows positive · average test PnL={avg_pnl:+.2f} USDT",
            )

        # 4) Monte Carlo (already vectorized numpy — no pool needed)
        mc: dict = {"error": "No trade data."}
        if trades:
            _add_step(
                run_id,
                f"🎲 Monte Carlo — shuffles the sequence of {len(trades)} trades {300} times to "
                f"measure the luck factor. Median DD < {_MC_DD_LIMIT:.0f}% is risky.",
            )
            mc = run_monte_carlo(
                trades,
                n_sims=300,
                starting_cash=STARTING_CASH,
                progress_fn=pf,
            )
            if not mc.get("error"):
                _add_step(
                    run_id,
                    f"  → Median final: ${mc.get('median_final', 0):,.0f} · "
                    f"p5 scenario: ${mc.get('p5_final', 0):,.0f} · "
                    f"Median max DD: {mc.get('max_dd_p50', 0):.1f}%",
                )
        else:
            _add_step(
                run_id, "  ⚠ Monte Carlo skipped — no trades were opened in the backtest"
            )

        return {"split": split, "wfo_windows": wfo, "mc": mc, "multi_symbol": ms}
    finally:
        if pool is not None:
            pool.shutdown()
        if snapshot_path is not None:
            _shutil.rmtree(Path(snapshot_path).parent, ignore_errors=True)


# ── Worker ────────────────────────────────────────────────────────────────────


def _agent_worker(
    run_id: str,
    hint: str,
    symbol: str,
    category: str,
    intervals: list[str],
    n_iterations: int,
    strict_mode: bool,
    trend_filter: bool = False,
    trend_interval: str = "60",
    continuous_mode: bool = False,
    web_research: bool = False,
    source: str = "bybit",
    instrument_id: str = "",
    max_hours: float = 0.0,
    max_total_tokens: int = 0,
) -> None:
    import pandas as pd

    from agent import propose_composed_strategy
    from composer import load_catalog
    from data import _bybit_cache_path
    from sandbox import run_backtest_guarded, run_robustness_guarded

    is_external = source == "external"
    # Market context passed to the LLM — None on Bybit (existing prompt preserved byte-for-byte).
    market = (
        f"US equity {instrument_id} ({'/'.join(intervals)} bars, USD cash account)"
        if is_external
        else None
    )

    def _recipe(iv: str) -> dict:
        """String recipe from which the sandbox/robustness child rebuilds the instrument."""
        if is_external:
            return {
                "source": "external",
                "instrument_id": instrument_id,
                "granularity": iv,
            }
        return {"symbol": symbol, "interval": iv, "category": category}

    # Log the session start
    _session_log(
        run_id,
        "session_start",
        hint=hint,
        symbol=symbol,
        category=category,
        intervals=intervals,
        n_iterations=n_iterations,
        strict_mode=strict_mode,
        trend_filter=trend_filter,
        trend_interval=trend_interval,
        continuous_mode=continuous_mode,
        web_research=web_research,
        source=source,
        instrument_id=instrument_id,
        max_hours=max_hours,
        max_total_tokens=max_total_tokens,
    )

    run_number = 0
    # 0 = UNLIMITED (user preference): continuous mode stops only via the stop
    # button OR the circuit breaker. If a safe ceiling is wanted, any positive
    # number suffices.
    _MAX_CONTINUOUS_ROUNDS = 0
    # Circuit breaker: the same error text N consecutive rounds → stop. In
    # unlimited mode this is the ONLY automatic safety net (the 886f439b session
    # kept retrying a persistent "Cache too little data" error in a useless loop
    # — this cuts that off).
    _CONSEC_ERR_LIMIT = 3
    _last_err_str: str | None = None
    _consec_err = 0
    # M22: optional budget ceilings (0 = unlimited) + winnerless-round breaker.
    _worker_t0 = time.monotonic()
    _winless_rounds = 0

    def _winless_bump() -> bool:
        """Increment the winnerless-round counter; returns True if the limit is exceeded.

        M22: previously it only incremented in the 'no eligible candidate' branch;
        the 'candidates exist but none passed robustness' branch skipped the
        counter, leaving an infinite-loop risk. Now both winnerless branches call
        this.
        """
        nonlocal _winless_rounds
        _winless_rounds += 1
        return bool(_WINLESS_ROUND_LIMIT and _winless_rounds >= _WINLESS_ROUND_LIMIT)

    def _winless_stop() -> None:
        _add_step(
            run_id,
            f"⏹ {_WINLESS_ROUND_LIMIT} consecutive winnerless rounds — "
            "circuit breaker stopping continuous mode.",
        )
        _tl_close_open(run_id, status="warn")
        with _AGENT_LOCK:
            if run_id in _AGENT_PROGRESS:
                _AGENT_PROGRESS[run_id]["done"] = True
                _AGENT_PROGRESS[run_id]["continuous_finished"] = True
        _session_log(
            run_id,
            "session_end",
            round=run_number,
            outcome="winless_limit",
            total_rounds=run_number,
        )

    while True:
        run_number += 1
        if (
            continuous_mode
            and _MAX_CONTINUOUS_ROUNDS
            and run_number > _MAX_CONTINUOUS_ROUNDS
        ):
            _add_step(
                run_id,
                f"Continuous mode: maximum of {_MAX_CONTINUOUS_ROUNDS} rounds reached, stopping.",
            )
            break

        # M22: budget check at round start — if the time/token ceiling is exceeded, finish gracefully.
        _elapsed_h = (time.monotonic() - _worker_t0) / 3600.0
        with _AGENT_LOCK:
            _bs = _AGENT_PROGRESS.get(run_id) or {}
            _tok_total = sum(
                (_bs.get(k, 0) or 0)
                for k in (
                    "tokens_in",
                    "tokens_out",
                    "tokens_cache_read",
                    "tokens_cache_write",
                )
            )
        _budget_reason = None
        if max_hours and max_hours > 0 and _elapsed_h >= max_hours:
            _budget_reason = f"time ceiling ({max_hours:g} hours) reached"
        elif (
            max_total_tokens and max_total_tokens > 0 and _tok_total >= max_total_tokens
        ):
            _budget_reason = f"token ceiling ({max_total_tokens:,}) exceeded"
        if _budget_reason:
            _add_step(
                run_id,
                f"⏹ Budget: {_budget_reason} — ending the run gracefully.",
            )
            _tl_close_open(run_id, status="warn")
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["done"] = True
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="budget",
                reason=_budget_reason,
                total_rounds=run_number - 1,
            )
            return
        if continuous_mode and run_number > 1:
            with _AGENT_LOCK:
                s = _AGENT_PROGRESS.get(run_id)
                if s is None or s.get("stop_requested"):
                    break
                # Reset for next round
                s["done"] = False
                s["error"] = None
                s["winner_result"] = None
                s["winner_spec_name"] = ""
                s["winner_spec_id"] = ""
                s["winner_rob"] = None
                s["winner_holdout"] = None
                s["winner_narrative"] = None  # M2625: new round → fresh narrative
                s["rob_scan_log"] = []
                s["backtest_results"] = []
                for p in s["phases"]:
                    p["status"] = "pending"
                    p["detail"] = ""
            # Timeline: the previous round's open spans are closed with warn; spans
            # are NOT DELETED (they remain with the round tag) — the live view
            # filters to the active round.
            _tl_close_open(run_id, status="warn")
            _add_step(run_id, f"━━━ Continuous mode: round {run_number} starting ━━━")

        try:
            # ── Phase 0: Data ────────────────────────────────────────────────────
            # Multi-TF: lazy-load cache per TF. Pre-load the first TF here.
            # L32: tf_cache holds the TRIMMED data (excluding the sealed holdout);
            # the holdout slice is stored in holdout_cache and used ONLY in a
            # single validation run after the winner is declared.
            tf_cache: dict[str, pd.DataFrame] = {}  # interval → trimmed_df
            holdout_cache: dict[str, pd.DataFrame | None] = {}  # interval → sealed df

            def _load_tf(iv: str) -> pd.DataFrame:
                if iv in tf_cache:
                    return tf_cache[iv]
                # Timeline: cache-miss load is tracked as a "data" span.
                _tl_key = f"data-{iv}-r{run_number}"
                _tl_begin(
                    run_id,
                    "data",
                    _tl_key,
                    f"Data: {instrument_id if is_external else symbol} {iv}",
                    round_num=run_number,
                )
                try:
                    df = _load_tf_uncached(iv)
                except Exception:
                    _tl_end(run_id, _tl_key, status="fail")
                    raise
                # L32: sealed holdout — the last OOS_HOLDOUT_DAYS days are withheld
                # from the iteration + robustness phases. Skip if remaining data < 200 bars.
                trimmed, hold_df = _split_holdout(df)
                if hold_df is not None:
                    df = trimmed
                    tf_cache[iv] = df
                    _add_step(
                        run_id,
                        f"🔒 Sealed holdout separated ({iv}): the last "
                        f"{OOS_HOLDOUT_DAYS} days ({len(hold_df):,} bars) will be "
                        f"withheld until a winner is declared — {len(df):,} bars remaining",
                    )
                else:
                    tf_cache[iv] = df
                    _add_step(
                        run_id,
                        f"⚠ insufficient data for holdout ({iv}) — sealed OOS skipped",
                    )
                holdout_cache[iv] = hold_df
                _tl_end(run_id, _tl_key, status="ok", n_bars=len(df))
                return df

            def _load_tf_uncached(iv: str) -> pd.DataFrame:
                from datetime import timedelta

                from data import load_bybit_bars

                if is_external:
                    from data import load_external_bars

                    _add_step(
                        run_id,
                        f"Loading catalog data ({instrument_id}, {iv})…",
                    )
                    df = load_external_bars(instrument_id, iv)  # full catalog range
                    if len(df) < 100:
                        raise RuntimeError(
                            f"Insufficient data ({len(df)} bars, {instrument_id} {iv})."
                        )
                    # 1-MINUTE guard: ~23 years >2M bars — crop to the last 2 years
                    # to keep the engine responsive (mirror of the Bybit 1m guard).
                    if iv == "1-MINUTE" and len(df) > 1_000_000:
                        cutoff = df.index[-1] - pd.Timedelta(days=730)
                        df = df[df.index >= cutoff]
                        _add_step(
                            run_id, f"1-MINUTE cropped to the last 2 years ({len(df):,} bars)"
                        )
                    tf_cache[iv] = df
                    return df

                cache_path = _bybit_cache_path(category, symbol, iv)
                # Widest available range per TF. 1m is bounded to ~2y (and cropped
                # below) so the engine stays responsive; coarser TFs pull Bybit's
                # full history (bar counts are small). load_bybit_bars now backfills
                # older history when `start` predates the cache, so a narrow cache
                # (e.g. the 7-day startup fetch) is widened here on first run and
                # served from cache afterwards.
                lookback_days = {"1": 730, "5": 1460, "15": 2200}.get(iv, 2200)
                end_dt = datetime.now(UTC)
                start_dt = end_dt - timedelta(days=lookback_days)
                _add_step(
                    run_id,
                    f"Loading widest range ({iv}, ~{lookback_days}d)…",
                )
                try:
                    df = load_bybit_bars(
                        symbol=symbol,
                        interval=iv,
                        category=category,
                        start=start_dt,
                        end=end_dt,
                    )
                except Exception as fetch_exc:
                    # Network hiccup — fall back to whatever is already cached.
                    if not cache_path.exists():
                        raise RuntimeError(
                            f"Could not load {symbol}/{category}/{iv} data: {fetch_exc}"
                        ) from fetch_exc
                    _add_step(
                        run_id, f"Fetch error ({iv}), falling back to cache: {fetch_exc}"
                    )
                    df = pd.read_parquet(cache_path)

                if len(df) < 100:
                    raise RuntimeError(
                        f"Insufficient data ({len(df)} bars, {iv}). "
                        "Fetch it from the Data page."
                    )
                # 1m guard: cap at last 2 years so backtests stay responsive.
                if iv == "1" and len(df) > 1_000_000:
                    cutoff = df.index[-1] - pd.Timedelta(days=730)
                    df = df[df.index >= cutoff]
                    _add_step(run_id, f"1m cropped to the last 2 years ({len(df):,} bars)")
                tf_cache[iv] = df
                return df

            if is_external:
                # Narrow down to the timeframes the instrument actually has.
                from data import _external_bar_dir

                avail = [
                    iv
                    for iv in intervals
                    if _external_bar_dir(instrument_id, iv) is not None
                ]
                skipped = [iv for iv in intervals if iv not in avail]
                if skipped:
                    _add_step(
                        run_id,
                        f"⚠ missing TF skipped for {instrument_id}: {', '.join(skipped)}",
                    )
                if not avail:
                    raise RuntimeError(
                        f"No catalog data for {instrument_id} in the "
                        "selected timeframes"
                    )
                intervals = avail

            first_iv = intervals[0]
            _set_phase(
                run_id,
                0,
                (
                    f"Reading catalog: {instrument_id}/{first_iv}"
                    if is_external
                    else f"Reading cache: {symbol}/{category}/{first_iv}"
                )
                + (f" + {len(intervals) - 1} more TF" if len(intervals) > 1 else ""),
            )
            first_df = _load_tf(first_iv)
            date_start = first_df.index[0].date()
            date_end = first_df.index[-1].date()
            _done_phase(
                run_id,
                0,
                f"✓ {len(first_df):,} bar · {date_start} → {date_end}"
                + (
                    f" · Multi-TF: {', '.join(intervals)}" if len(intervals) > 1 else ""
                ),
            )

            if is_external:
                # Index convention: NO "symbol" key — Bybit-specific chart and
                # robustness OOB panels are triggered via bars_info["symbol"], so
                # they safely stay disabled on external runs.
                bars_info = {
                    "ticker": instrument_id,
                    "granularity": first_iv,
                    "n_bars": len(first_df),
                    "start": str(date_start),
                    "end": str(date_end),
                }
            else:
                bars_info = {
                    "symbol": symbol,
                    "category": category,
                    "interval": first_iv,
                    "n_bars": len(first_df),
                    "start": str(date_start),
                    "end": str(date_end),
                }

            # ── Phase 1: Initial strategy ────────────────────────────────────────
            _set_phase(run_id, 1, "Claude is generating a strategy…")

            catalog = load_catalog()
            dummy_history: list = []

            if web_research:
                _add_step(run_id, "🌐 Running web research…")
            _tl_begin(
                run_id,
                "llm",
                f"llm-propose-r{run_number}",
                "Initial strategy (Claude)",
                round_num=run_number,
            )
            proposal, _usage1 = propose_composed_strategy(
                dummy_history,
                catalog,
                hint=hint,
                web_research=web_research,
                market=market,
            )
            _add_tokens(run_id, _usage1)
            _session_log(
                run_id,
                "strategy_proposed",
                iteration=0,
                round=run_number,
                spec=proposal,
                source="builtin",
                usage=_usage1,
            )

            spec = _proposal_to_spec(proposal)
            _tl_end(run_id, f"llm-propose-r{run_number}", status="ok", name=spec.name)
            if is_external:
                _clamp_spec_trade_size(spec)
            spec.trend_filter = trend_filter
            spec.trend_interval = trend_interval
            _done_phase(
                run_id,
                1,
                f"✓ {spec.name}" + (" · trend filter ON" if trend_filter else ""),
            )

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name

            # ── Phase 2: Backtest loop ───────────────────────────────────────────
            # Backtests run in killable child processes (sandbox); the child
            # rebuilds the instrument/bar_type from the recipe, so the worker
            # thread never touches Nautilus objects (nor the GIL) directly.
            _set_phase(run_id, 2, f"0/{n_iterations} completed")

            history: list = []
            results: list[tuple] = []  # (spec, result, iter_iv)
            used_concepts: list[str] = []  # accumulate custom block labels

            for i in range(n_iterations):
                # Check stop signal between iterations in continuous mode.
                # _add_step OUTSIDE THE LOCK: it also acquires _AGENT_LOCK;
                # calling it from inside the block was a re-entrant deadlock (the
                # 2026-07-14 live incident that froze the whole server).
                with _AGENT_LOCK:
                    s = _AGENT_PROGRESS.get(run_id)
                    stop_hit = bool(s is not None and s.get("stop_requested"))
                if stop_hit:
                    _add_step(run_id, "  ⏹ Stop signal received — breaking the loop")
                    break

                # Round-robin TF selection
                iter_iv = intervals[i % len(intervals)]
                iter_df = _load_tf(iter_iv)
                if is_external:
                    iter_bars_info = {
                        "ticker": instrument_id,
                        "granularity": iter_iv,
                        "n_bars": len(iter_df),
                        "start": str(iter_df.index[0].date()),
                        "end": str(iter_df.index[-1].date()),
                    }
                    tf_label = f" [{iter_iv}]"
                else:
                    iter_bars_info = {
                        "symbol": symbol,
                        "category": category,
                        "interval": iter_iv,
                        "n_bars": len(iter_df),
                        "start": str(iter_df.index[0].date()),
                        "end": str(iter_df.index[-1].date()),
                    }
                    tf_label = f" [{iter_iv}m]" if iter_iv != "D" else " [1D]"

                _add_step(
                    run_id,
                    f"[{i + 1}/{n_iterations}] Backtest{tf_label}: {spec.name}…",
                )
                run_label = instrument_id if is_external else symbol
                _tl_begin(
                    run_id,
                    "backtest",
                    f"bt-r{run_number}-i{i + 1}",
                    f"Backtest {i + 1}/{n_iterations} · {spec.name} [{iter_iv}]",
                    round_num=run_number,
                    iter=i + 1,
                    name=spec.name,
                )
                # Run in a killable child process: a Nautilus backtest holds the
                # GIL for its whole run and would otherwise freeze the async web
                # server's event loop. The timeout also kills a hung backtest.
                _bt_t0 = time.perf_counter()
                r = run_backtest_guarded(
                    spec,
                    iter_df,
                    _recipe(iter_iv),
                    iteration_id=i,
                    rationale=f"agent-run · iter {i + 1}/{n_iterations} · {run_label} {iter_iv}",
                    progress_fn=lambda m, _i=i: _add_step(run_id, f"  └ {m}"),
                    timeout_s=150.0,
                    force_subprocess=True,
                )
                _bt_elapsed = time.perf_counter() - _bt_t0
                # Add the block type list to the strategy field → Claude sees this info
                block_types_str = "+".join(b.type for b in spec.blocks)
                r.strategy = f"composed:{spec.name} [{block_types_str}]"
                history.append(r)
                results.append((spec, r, iter_iv))
                try:
                    _log_backtest(
                        spec,
                        r,
                        "External" if is_external else "Bybit",
                        iter_bars_info,
                        elapsed_sec=_bt_elapsed,
                    )
                except Exception as log_exc:
                    _add_step(run_id, f"  ⚠ Could not write log: {log_exc}")

                sc = _score(r)
                # Add to state for the live backtest table
                m_bt = r.metrics or {}
                with _AGENT_LOCK:
                    s = _AGENT_PROGRESS.get(run_id)
                    if s is not None:
                        s["backtest_results"].append(
                            {
                                "iter": i + 1,
                                "round": run_number,
                                "interval": iter_iv,
                                "spec_name": spec.name,
                                "score": round(sc, 4) if sc > float("-inf") else None,
                                "pnl": m_bt.get("pnl"),
                                "pnl_pct": m_bt.get("pnl_pct"),
                                "sharpe": m_bt.get("sharpe"),
                                "max_dd": m_bt.get("max_dd"),
                                "win_rate": m_bt.get("win_rate"),
                                "n_trades": m_bt.get("n_trades", 0),
                                "avg_dur": m_bt.get("avg_duration_mins"),
                                "error": r.error,
                            }
                        )
                # Session log: backtest result (including equity_curve)
                _session_log(
                    run_id,
                    "backtest_result",
                    iteration=i,
                    round=run_number,
                    interval=iter_iv,
                    spec_name=spec.name,
                    spec_id=spec.id,
                    spec_blocks=[
                        {"type": b.type, "role": b.role, "params": b.params}
                        for b in spec.blocks
                    ],
                    score=round(sc, 4),
                    metrics=r.metrics,
                    equity_curve=list(r.equity_curve) if r.equity_curve else [],
                    equity_dates=list(r.equity_dates) if r.equity_dates else [],
                    n_trades=len(r.trades) if r.trades else 0,
                    bars_info=iter_bars_info,
                    error=r.error,
                )
                if r.error:
                    _add_step(run_id, f"  ✗ Error: {r.error[:80]}")
                    _tl_end(run_id, f"bt-r{run_number}-i{i + 1}", status="fail")
                else:
                    m = r.metrics or {}
                    _add_step(
                        run_id,
                        f"  ✓ PnL={m.get('pnl', 0):+.2f} · "
                        f"Sharpe={m.get('sharpe', float('nan')):.2f} · "
                        f"Trades={m.get('n_trades', 0)} · Score={sc:.3f}",
                    )
                    _tl_end(
                        run_id,
                        f"bt-r{run_number}-i{i + 1}",
                        status="warn" if (m.get("n_trades", 0) or 0) == 0 else "ok",
                        pnl=m.get("pnl"),
                        score=round(sc, 4) if sc > float("-inf") else None,
                    )

                _set_phase(run_id, 2, f"{i + 1}/{n_iterations} completed")

                if i < n_iterations - 1:
                    next_i = i + 1
                    use_custom = next_i % 2 == 1
                    _add_step(
                        run_id,
                        f"[{next_i + 1}/{n_iterations}] "
                        + (
                            "Generating custom block…"
                            if use_custom
                            else "Claude is generating a new strategy…"
                        ),
                    )
                    _tl_begin(
                        run_id,
                        "llm",
                        f"llm-refine-r{run_number}-i{next_i + 1}",
                        (
                            "Custom block generation"
                            if use_custom
                            else "Claude new strategy"
                        ),
                        round_num=run_number,
                        iter=next_i + 1,
                    )
                    try:
                        if use_custom:
                            custom_spec = _generate_custom_spec(
                                run_id,
                                next_i,
                                hint,
                                history,
                                used_concepts=used_concepts,
                                round_num=run_number,
                                market=market,
                            )
                            if custom_spec is not None:
                                spec = custom_spec
                                # Accumulate the generated block labels
                                for b in spec.blocks:
                                    used_concepts.append(b.type)
                                _add_step(run_id, f"  → {spec.name} (custom)")
                            else:
                                proposal, _u = propose_composed_strategy(
                                    history, load_catalog(), hint=hint, market=market
                                )
                                _add_tokens(run_id, _u)
                                spec = _proposal_to_spec(proposal)
                                _session_log(
                                    run_id,
                                    "strategy_proposed",
                                    iteration=next_i,
                                    round=run_number,
                                    spec=proposal,
                                    source="builtin_fallback",
                                    usage=_u,
                                )
                                _add_step(run_id, f"  → {spec.name} (fallback builtin)")
                        else:
                            proposal, _u = propose_composed_strategy(
                                history, load_catalog(), hint=hint, market=market
                            )
                            _add_tokens(run_id, _u)
                            spec = _proposal_to_spec(proposal)
                            _session_log(
                                run_id,
                                "strategy_proposed",
                                iteration=next_i,
                                round=run_number,
                                spec=proposal,
                                source="builtin",
                                usage=_u,
                            )
                            _add_step(run_id, f"  → {spec.name}")
                        # H9: trend_filter/trend_interval was only applied to the
                        # first (phase 1) spec; EVERY spec generated in refine
                        # kept the composer default (trend_filter=False), making
                        # inter-iteration comparison inconsistent and saving the
                        # winner without a filter. Apply it to every spec.
                        spec.trend_filter = trend_filter
                        spec.trend_interval = trend_interval
                        if is_external:
                            _clamp_spec_trade_size(spec)
                        with _AGENT_LOCK:
                            if run_id in _AGENT_PROGRESS:
                                _AGENT_PROGRESS[run_id]["strategy_name"] = spec.name
                        _tl_end(
                            run_id,
                            f"llm-refine-r{run_number}-i{next_i + 1}",
                            status="ok",
                            name=spec.name,
                        )
                    except Exception as e:
                        _add_step(
                            run_id,
                            f"  ⚠ Could not get proposal: {e} — continuing with previous strategy",
                        )
                        _tl_end(
                            run_id,
                            f"llm-refine-r{run_number}-i{next_i + 1}",
                            status="warn",
                        )

            _done_phase(run_id, 2, f"✓ {n_iterations} iterations completed")

            # ── Phase 3: Ranking ─────────────────────────────────────────────────
            _set_phase(run_id, 3, "Ranking results…")
            _tl_begin(
                run_id,
                "backtest",
                f"rank-r{run_number}",
                "Ranking",
                round_num=run_number,
            )
            ranked = _rank_results(results)
            eligible = [(s, r, iv) for s, r, iv in ranked if _score(r) > float("-inf")]

            _add_step(
                run_id,
                f"{len(eligible)}/{len(results)} results qualify (≥{_MIN_TRADES} trades, no error)",
            )
            for rank_i, (s, r, iv) in enumerate(ranked[:5]):
                sc = _score(r)
                m = r.metrics or {}
                _add_step(
                    run_id,
                    f"  #{rank_i + 1} {s.name} [{iv}] · score={sc:.3f} · "
                    f"PnL={m.get('pnl', 0):+.2f} · "
                    f"Sharpe={m.get('sharpe', float('nan')):.2f}",
                )

            if not eligible:
                _tl_end(run_id, f"rank-r{run_number}", status="warn")
                _done_phase(run_id, 3, "⚠ No eligible result — all iterations failed")
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    return
                _consec_err = 0  # round ended without exception — error streak broken
                _last_err_str = None
                if _winless_bump():
                    _winless_stop()
                    return
                continue
            _tl_end(run_id, f"rank-r{run_number}", status="ok")
            _done_phase(run_id, 3, f"✓ {len(eligible)} candidates ranked")

            # ── Phase 4: Robustness scan ─────────────────────────────────────────
            _set_phase(run_id, 4, f"0/{len(eligible)} trying")
            _set_robustness_scan(run_id, 0, len(eligible))

            winner_spec = None
            winner_result = None
            winner_rob = None
            winner_iv = None
            rob_scan_log: list[dict] = []
            # M26+M31: instead of "first passer wins", collect at most the first 3
            # candidates that pass; the winner is chosen by the effective score
            # weighted by the multi-symbol pass_rate factor. If the list ends
            # before 3 passers are found, decide with what's on hand.
            _MAX_PASSERS = 3
            passers: list[dict] = []

            for rank_i, (cand_spec, cand_result, cand_iv) in enumerate(eligible):
                # The stop signal is also checked INSIDE the robustness scan:
                # otherwise, even if the iteration loop is cut by 'stop', the flow
                # would enter the full robustness scan that takes minutes per
                # candidate.
                with _AGENT_LOCK:
                    _rs = _AGENT_PROGRESS.get(run_id)
                    _rob_stop = bool(_rs is not None and _rs.get("stop_requested"))
                if _rob_stop:
                    _add_step(
                        run_id,
                        "  ⏹ Stop signal — breaking the robustness scan",
                    )
                    break
                _set_robustness_scan(run_id, rank_i + 1, len(eligible))
                _add_step(
                    run_id,
                    f"[{rank_i + 1}/{len(eligible)}] Robustness: {cand_spec.name} [{cand_iv}]",
                )
                _set_phase(
                    run_id,
                    4,
                    f"{rank_i + 1}/{len(eligible)} trying: {cand_spec.name}",
                )

                cand_df = _load_tf(cand_iv)

                _rob_key = f"rob-r{run_number}-c{rank_i + 1}"
                _tl_begin(
                    run_id,
                    "robustness",
                    _rob_key,
                    f"Robustness {rank_i + 1}/{len(eligible)} · {cand_spec.name}",
                    round_num=run_number,
                    name=cand_spec.name,
                )
                _rob_progress = _make_rob_progress(run_id, rank_i + 1, run_number)
                try:
                    # Isolated in a killable child so the suite's many backtests
                    # can't freeze the web server's event loop.
                    rob = run_robustness_guarded(
                        cand_spec,
                        cand_df,
                        _recipe(cand_iv),
                        cand_result.trades,
                        symbol=instrument_id if is_external else symbol,
                        interval=cand_iv,
                        progress_fn=_rob_progress,
                    )
                except Exception as rob_exc:
                    _rob_progress.close_open("fail")
                    _tl_end(run_id, _rob_key, status="fail")
                    _add_step(run_id, f"  ⚠ Robustness error: {rob_exc} — skipping")
                    rob_scan_log.append(
                        {
                            "rank": rank_i + 1,
                            "name": cand_spec.name,
                            "score": round(_score(cand_result), 3),
                            "passed": False,
                            "overfitting_label": f"error: {type(rob_exc).__name__}",
                            "mc_dd_p50": None,
                            "wf_pass": "—",
                            "ms_label": "—",
                        }
                    )
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["rob_scan_log"] = list(rob_scan_log)
                    continue

                passed = _robustness_passed(rob, strict=strict_mode, run_id=run_id)

                split_label = (rob.get("split") or {}).get("overfitting_label", "?")
                mc_dd = (rob.get("mc") or {}).get("max_dd_p50", None)
                wfo = rob.get("wfo_windows") or []
                wf_pos = sum(
                    1 for w in wfo if (w.get("test_metrics") or {}).get("pnl", 0) > 0
                )
                wf_str = f"{wf_pos}/{len(wfo)}" if wfo else "—"
                ms_label = (rob.get("multi_symbol") or {}).get(
                    "generalization_label", "—"
                )

                # Session log: full robustness result (including equity curves + multi_symbol)
                _session_log(
                    run_id,
                    "robustness_result",
                    round=run_number,
                    rank=rank_i + 1,
                    spec_name=cand_spec.name,
                    spec_id=cand_spec.id,
                    score=round(_score(cand_result), 4),
                    passed=passed,
                    overfitting_label=split_label,
                    wf_pass=wf_str,
                    ms_label=ms_label,
                    split=rob.get("split"),
                    wfo_windows=rob.get("wfo_windows"),
                    mc=rob.get("mc"),
                    multi_symbol=rob.get("multi_symbol"),
                )
                # L26: lightweight SQLite index (best-effort, errors swallowed)
                _index_insert(
                    run_id,
                    run_number,
                    cand_spec.name,
                    cand_spec.id,
                    _score(cand_result),
                    passed,
                    instrument_id if is_external else symbol,
                    cand_iv,
                )

                rob_scan_log.append(
                    {
                        "rank": rank_i + 1,
                        "name": cand_spec.name,
                        "score": round(_score(cand_result), 3),
                        "passed": passed,
                        "overfitting_label": split_label,
                        "mc_dd_p50": round(mc_dd, 1) if mc_dd is not None else None,
                        "wf_pass": wf_str,
                        "ms_label": ms_label,
                    }
                )
                # Flush partial results after each candidate so data is preserved
                # even if an exception aborts the loop later.
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["rob_scan_log"] = list(rob_scan_log)

                _rob_progress.close_open("ok")
                if passed:
                    _tl_end(run_id, _rob_key, status="ok", name=cand_spec.name)
                    # M26+M31: a passing candidate enters the pool; effective
                    # score = _score × multi-symbol pass_rate factor (0.15…1.0).
                    raw_score = _score(cand_result)
                    factor = _ms_score_factor(rob)
                    # Sign-safe MS penalty: for a positive score, exactly equals
                    # raw*factor (raw - (1-factor)*raw); for a negative score, it
                    # prevents a small factor from making the score LESS negative
                    # and inverting the ranking (the penalty always pulls the
                    # score DOWN).
                    effective = raw_score - (1.0 - factor) * abs(raw_score)
                    passers.append(
                        {
                            "spec": cand_spec,
                            "result": cand_result,
                            "rob": rob,
                            "iv": cand_iv,
                            "score": raw_score,
                            "factor": factor,
                            "effective": effective,
                        }
                    )
                    _add_step(
                        run_id,
                        f"  ✅ ALL TESTS PASSED! "
                        f"IS/OOS: {split_label} · WFO: {wf_str} · Multi-symbol: {ms_label}",
                    )
                    _add_step(
                        run_id,
                        f"  ⚖ Effective score: {raw_score:.3f} × MS-factor "
                        f"{factor:.3f} = {effective:.3f} "
                        f"({len(passers)}/{_MAX_PASSERS} passing candidates)",
                    )
                    if len(passers) >= _MAX_PASSERS:
                        _add_step(
                            run_id,
                            f"  {_MAX_PASSERS} passing candidates collected — scan ending",
                        )
                        break
                else:
                    _tl_end(run_id, _rob_key, status="warn", name=cand_spec.name)
                    _add_step(
                        run_id,
                        (
                            f"  ❌ Failed — IS/OOS: {split_label} · "
                            f"WFO: {wf_str} · MC median DD: {mc_dd:.1f}% · Multi-symbol: {ms_label}"
                            if mc_dd is not None
                            else f"  ❌ Failed — IS/OOS: {split_label} · "
                            f"WFO: {wf_str} · Multi-symbol: {ms_label}"
                        ),
                    )

            if passers:
                # The passing candidate with the highest effective score wins.
                best = max(passers, key=lambda p: p["effective"])
                winner_spec = best["spec"]
                winner_result = best["result"]
                winner_rob = best["rob"]
                winner_iv = best["iv"]
                if len(passers) > 1:
                    _add_step(
                        run_id,
                        "🏁 Selection among passers: "
                        + " · ".join(
                            f"{p['spec'].name}={p['effective']:.3f}"
                            f" ({p['score']:.3f}×{p['factor']:.2f})"
                            for p in passers
                        ),
                    )
                _add_step(
                    run_id,
                    f"🏆 Winner: {winner_spec.name} "
                    f"(effective score {best['effective']:.3f})",
                )
                # Update bars_info with the winner's actual TF — not only the TF
                # key but also n_bars/start/end are rebuilt from the winner's df
                # (in multi-TF the first TF's range was shifting the chart URL
                # window and the winner session-log to the wrong range).
                bars_info["granularity" if is_external else "interval"] = winner_iv
                _win_df = _load_tf(winner_iv)
                bars_info["n_bars"] = len(_win_df)
                bars_info["start"] = str(_win_df.index[0].date())
                bars_info["end"] = str(_win_df.index[-1].date())

            if winner_spec is None:
                _done_phase(run_id, 4, f"✗ None of the {len(eligible)} candidates passed")
                if not continuous_mode:
                    with _AGENT_LOCK:
                        if run_id in _AGENT_PROGRESS:
                            _AGENT_PROGRESS[run_id]["done"] = True
                    _session_log(
                        run_id,
                        "session_end",
                        round=run_number,
                        outcome="no_winner",
                        total_rounds=run_number,
                    )
                    return
                _consec_err = 0  # round ended without exception — error streak broken
                _last_err_str = None
                # M22: 'candidates exist but none passed robustness' is also a
                # winnerless round — the counter must increment here too (the most
                # common infinite-loop scenario).
                if _winless_bump():
                    _winless_stop()
                    return
                _add_step(run_id, "Continuous mode: new round starting…")
                continue

            _winless_rounds = 0  # M22: winner found — winnerless streak broken
            _done_phase(run_id, 4, f"✓ Winner: {winner_spec.name}")

            # ── Phase 5: Save ────────────────────────────────────────────────────
            _set_phase(run_id, 5, "Saving to catalog…")
            _tl_begin(
                run_id,
                "data",
                f"save-r{run_number}",
                "Saving winner",
                round_num=run_number,
                name=winner_spec.name,
            )
            # M14: locked append_to_catalog instead of lockless load→append→save —
            # so the winner isn't lost due to a concurrent lab/strategy save.
            from composer import append_to_catalog

            append_to_catalog(winner_spec)
            _add_step(run_id, f"✓ {winner_spec.name} → strategy_catalog.json")

            # H4/H8: the winner may be on a different TF (winner_iv); cand_iv is
            # the TF of the LAST scanned candidate left over from the loop. The
            # robustness log and the sealed holdout must use the winner's OWN TF —
            # otherwise the log identity is overwritten and the holdout runs with
            # the wrong slice/recipe.
            _log_robustness(
                winner_spec.id,
                winner_spec.name,
                winner_rob,
                symbol=instrument_id if is_external else symbol,
                category=category,
                interval=winner_iv,
            )
            _add_step(run_id, "✓ Robustness result → robustness_log.jsonl")
            _tl_end(run_id, f"save-r{run_number}", status="ok")
            _done_phase(run_id, 5, f"✓ {winner_spec.name} saved")

            # ── L32: sealed holdout — the winner is run ONCE on the last
            # OOS_HOLDOUT_DAYS-day slice that NEVER entered selection. The result
            # is only reported (an unbiased forward-looking estimate +
            # selection-bias detector); it is not bound to any decision.
            winner_holdout = None
            _hold_df = holdout_cache.get(winner_iv)  # H4: the winner's TF
            if _hold_df is not None and not _hold_df.empty:
                try:
                    _hold_res = run_backtest_guarded(
                        winner_spec,
                        _hold_df,
                        _recipe(winner_iv),
                        iteration_id=999,
                        rationale="sealed holdout (L32)",
                        timeout_s=150.0,
                        force_subprocess=True,
                    )
                    _hm = _hold_res.metrics or {}
                    if _hold_res.error is None and _hm:
                        winner_holdout = {
                            "sharpe": _hm.get("sharpe"),
                            "pnl_pct": _hm.get("pnl_pct"),
                            "n_trades": _hm.get("n_trades"),
                            "days": OOS_HOLDOUT_DAYS,
                        }
                        _add_step(
                            run_id,
                            f"🔒 Sealed OOS ({OOS_HOLDOUT_DAYS}d): "
                            f"Sharpe {_hm.get('sharpe', 0):.2f} · "
                            f"PnL {100 * (_hm.get('pnl_pct') or 0):.1f}% · "
                            f"{_hm.get('n_trades', 0)} trades (not bound to decision)",
                        )
                        _session_log(
                            run_id,
                            "holdout_result",
                            round=run_number,
                            spec_id=winner_spec.id,
                            **winner_holdout,
                        )
                    else:
                        _add_step(
                            run_id,
                            f"⚠ Sealed OOS run returned an error: {_hold_res.error}",
                        )
                except Exception as _hold_err:
                    _add_step(run_id, f"⚠ Could not run sealed OOS: {_hold_err}")

            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    # Update bars_info with the winner's TF — for the chart URL
                    winner_result.bars_info = bars_info
                    _AGENT_PROGRESS[run_id]["winner_result"] = winner_result
                    _AGENT_PROGRESS[run_id]["winner_spec_name"] = winner_spec.name
                    _AGENT_PROGRESS[run_id]["winner_spec_id"] = winner_spec.id
                    _AGENT_PROGRESS[run_id]["winner_rob"] = winner_rob
                    _AGENT_PROGRESS[run_id]["winner_holdout"] = winner_holdout
                    _AGENT_PROGRESS[run_id]["done"] = (
                        True  # always set so polling shows result
                    )

            # Session log: winner
            _session_log(
                run_id,
                "winner",
                round=run_number,
                spec_name=winner_spec.name,
                spec_id=winner_spec.id,
                score=round(_score(winner_result), 4),
                metrics=winner_result.metrics,
                equity_curve=list(winner_result.equity_curve)
                if winner_result.equity_curve
                else [],
                bars_info=bars_info,
            )
            _consec_err = 0  # round finished successfully — error streak broken
            _last_err_str = None

            if not continuous_mode:
                # Terminal event: all other exit paths (no_winner/error/stopped)
                # write session_end; let the winner path write it too, so external
                # watchers and the /sessions summary can see the session ended.
                _session_log(
                    run_id,
                    "session_end",
                    round=run_number,
                    outcome="winner",
                    total_rounds=run_number,
                )
                return

            # In continuous mode: briefly expose the result then continue
            import time as _time

            _time.sleep(3)  # give polling a chance to render the result
            _add_step(
                run_id, f"Continuous mode: round {run_number} completed, continuing…"
            )

        except Exception as e:
            _tl_close_open(run_id, status="fail")
            err_str = f"{type(e).__name__}: {e}"
            with _AGENT_LOCK:
                if run_id in _AGENT_PROGRESS:
                    _AGENT_PROGRESS[run_id]["error"] = err_str
                    if not continuous_mode:
                        _AGENT_PROGRESS[run_id]["done"] = True
            _session_log(
                run_id,
                "session_end",
                round=run_number,
                outcome="error",
                error=err_str,
            )
            if not continuous_mode:
                break
            # Circuit breaker: the same error _CONSEC_ERR_LIMIT consecutive rounds
            # → persistent problem (missing data, config) — retrying is pointless, stop.
            _consec_err = _consec_err + 1 if err_str == _last_err_str else 1
            _last_err_str = err_str
            if _consec_err >= _CONSEC_ERR_LIMIT:
                _add_step(
                    run_id,
                    f"⏹ {_CONSEC_ERR_LIMIT} consecutive identical errors — stopping "
                    f"continuous mode: {err_str[:100]}",
                )
                with _AGENT_LOCK:
                    if run_id in _AGENT_PROGRESS:
                        _AGENT_PROGRESS[run_id]["done"] = True
                break
            _add_step(run_id, f"⚠ Round {run_number} error: {e} — restarting…")
        finally:
            # Token snapshot + session_end at the end of each round
            with _AGENT_LOCK:
                s = _AGENT_PROGRESS.get(run_id) or {}
            _ti = s.get("tokens_in", 0) or 0
            _to = s.get("tokens_out", 0) or 0
            _tcr = s.get("tokens_cache_read", 0) or 0
            _tcw = s.get("tokens_cache_write", 0) or 0
            # L38: model-based rates; on subscription CLI the cost is None.
            _model, _cost_usd = _llm_cost_usd(_ti, _to, _tcr, _tcw)
            _session_log(
                run_id,
                "token_snapshot",
                round=run_number,
                total_input=_ti,
                total_output=_to,
                cache_read=_tcr,
                cache_write=_tcw,
                pricing_model=_model,
                cost_usd=round(_cost_usd, 6) if _cost_usd is not None else None,
                cost_eur=round(_cost_usd * 0.91, 6) if _cost_usd is not None else None,
            )

    # Continuous mode exited (stop/budget/winless/error — all breaks land here).
    _tl_close_open(run_id, status="warn")
    with _AGENT_LOCK:
        if run_id in _AGENT_PROGRESS:
            _AGENT_PROGRESS[run_id]["done"] = True
            # Permanent end: the progress route stops polling when it sees this
            # (a separate flag to distinguish it from the inter-round done=True window).
            _AGENT_PROGRESS[run_id]["continuous_finished"] = True
    _session_log(run_id, "session_end", outcome="stopped", total_rounds=run_number)


def _pick_best_exit_from_history(history: list) -> str | None:
    """Return the exit block type of the highest-scoring strategy in history."""
    _EXIT_PRIORITY = [
        "momentum",
        "macd_cross",
        "rsi_threshold",
        "bollinger_break",
        "ema_cross",
    ]
    if not history:
        return None
    best_score = max((_score(r) for r in history), default=float("-inf"))
    if best_score > 0:
        for r in sorted(history, key=lambda x: _score(x), reverse=True)[:3]:
            name = r.strategy.lower()
            for et in _EXIT_PRIORITY:
                if et.replace("_", "") in name.replace("_", "").replace(" ", ""):
                    return et
        return _EXIT_PRIORITY[0]
    return None


def _generate_custom_spec(
    run_id: str,
    iter_idx: int,
    hint: str,
    history: list,
    used_concepts: list | None = None,
    round_num: int = 1,
    market: str | None = None,
):
    """Odd iterations: ask Claude for a novel idea, generate custom entry+exit blocks,
    register them, and return a ComposedStrategySpec built from those blocks.
    Returns None if block generation fails (caller falls back to builtin).

    ``market`` — optional market context (external US equity runs); passed to the
    idea generator, and if None the crypto phrasing is preserved.
    """
    from agent import (
        GeneratedCodeError,
        _propose_agent_strategy_idea,
        propose_custom_block,
    )
    from composer import (
        ComposedStrategySpec,
        SignalBlock,
        new_spec_id,
        register_custom_from_disk,
    )
    from custom_block_store import save_custom

    try:
        idea = _propose_agent_strategy_idea(
            hint, history, used_concepts=used_concepts, market=market
        )
        # M1583: the tokens from the idea + custom-block LLM calls should be
        # added to the counters — previously only the builtin proposal was
        # counted; since custom generation is the most token-heavy call, the cost
        # and the max_total_tokens budget breaker were seriously undercounting.
        _add_tokens(run_id, idea.get("usage"))
        entry_label = idea.get("entry_label", "Agent Entry")
        exit_label = idea.get("exit_label", "Agent Exit")

        entry_name = f"agnt_e_{run_id}_{iter_idx}"
        exit_name = f"agnt_x_{run_id}_{iter_idx}"

        _add_step(run_id, f"  ⚙ Generating custom entry block: {entry_label}…")
        entry_block = propose_custom_block(entry_label, idea["entry_desc"], "entry")
        _add_tokens(run_id, entry_block.get("usage"))
        entry_block["name"] = entry_name
        save_custom(entry_name, entry_block["meta"], entry_block["code"], prompt=hint)
        register_custom_from_disk(entry_name)
        _add_step(run_id, f"  ✓ Entry block saved: {entry_name}")
        # Session log + copy the code under {run_id}_blocks/
        _session_log(
            run_id,
            "custom_block_generated",
            iteration=iter_idx,
            round=round_num,
            name=entry_name,
            role="entry",
            label=entry_label,
            meta=entry_block.get("meta"),
            code=entry_block.get("code", ""),
        )
        try:
            blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
            blocks_dir.mkdir(parents=True, exist_ok=True)
            (blocks_dir / f"{entry_name}.py").write_text(entry_block.get("code", ""))
        except Exception:
            pass

        # With 50% probability use the best builtin exit from history (instead of custom)
        # This combines a proven exit mechanism with new entry ideas
        best_builtin_exit = _pick_best_exit_from_history(history)
        use_builtin_exit = (
            best_builtin_exit is not None and __import__("random").random() < 0.5
        )

        def _extract_params(blk: dict) -> dict:
            raw = blk["meta"].get("params") or {}
            return {
                k: (v.get("default") if isinstance(v, dict) else v)
                for k, v in raw.items()
            }

        if use_builtin_exit:
            from composer import BLOCK_CATALOG, SignalBlock

            exit_meta = BLOCK_CATALOG.get(best_builtin_exit, {}).get("params", {})
            exit_blk = SignalBlock(
                type=best_builtin_exit,
                role="exit",
                params={k: v["default"] for k, v in exit_meta.items()},
            )
            _add_step(
                run_id,
                f"  → Using builtin exit: {best_builtin_exit} (successful in the past)",
            )
            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=idea.get("name", f"Custom {iter_idx}"),
                description=idea.get("description", ""),
                blocks=[
                    SignalBlock(
                        type=entry_name,
                        role="entry",
                        params=_extract_params(entry_block),
                    ),
                    exit_blk,
                ],
                trade_size=0.01,
                order_type="market",
                entry_logic="OR",
                exit_logic="OR",
            )
        else:
            _add_step(run_id, f"  ⚙ Generating custom exit block: {exit_label}…")
            exit_block = propose_custom_block(exit_label, idea["exit_desc"], "exit")
            _add_tokens(run_id, exit_block.get("usage"))  # M1583
            exit_block["name"] = exit_name
            save_custom(exit_name, exit_block["meta"], exit_block["code"], prompt=hint)
            register_custom_from_disk(exit_name)
            _add_step(run_id, f"  ✓ Exit block saved: {exit_name}")
            # Session log + copy the code under {run_id}_blocks/
            _session_log(
                run_id,
                "custom_block_generated",
                iteration=iter_idx,
                round=round_num,
                name=exit_name,
                role="exit",
                label=exit_label,
                meta=exit_block.get("meta"),
                code=exit_block.get("code", ""),
            )
        try:
            blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
            blocks_dir.mkdir(parents=True, exist_ok=True)
            if not use_builtin_exit:
                (blocks_dir / f"{exit_name}.py").write_text(exit_block.get("code", ""))
        except Exception:
            pass

        if not use_builtin_exit:
            spec = ComposedStrategySpec(
                id=new_spec_id(),
                name=idea.get("name", f"Custom {iter_idx}"),
                description=idea.get("description", ""),
                blocks=[
                    SignalBlock(
                        type=entry_name,
                        role="entry",
                        params=_extract_params(entry_block),
                    ),
                    SignalBlock(
                        type=exit_name, role="exit", params=_extract_params(exit_block)
                    ),
                ],
                trade_size=0.01,
                order_type="market",
                entry_logic="OR",
                exit_logic="OR",
            )
        err = spec.validate()
        if err:
            raise RuntimeError(f"Custom spec invalid: {err}")
        return spec

    except (GeneratedCodeError, Exception) as e:
        _add_step(run_id, f"  ⚠ Could not generate custom block: {e} — falling back to builtin")
        return None


def _cleanup_agent_blocks(run_id: str) -> None:
    """Legacy hook kept for compatibility; run-specific blocks are retained."""
    return None


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    from server import get_market_info, templates

    # If there is an unfinished run, AUTOMATICALLY bind the page to it — so after
    # a server restart / tab refresh the user sees the running run (even if the
    # run was started from the API, i.e. not from this browser). Pick the newest
    # active run (first done=False from the end of the insertion-ordered dict).
    active_run_id = None
    with _AGENT_LOCK:
        for rid, st in reversed(_AGENT_PROGRESS.items()):
            if not st.get("done"):
                active_run_id = rid
                break

    return templates.TemplateResponse(
        request,
        "agent_backtest.html",
        {
            "active": "agent",
            "page_title": "Autonomous Backtest Agent",
            "market": get_market_info(),
            "active_run_id": active_run_id,
        },
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    hint: str = Form(default=""),
    symbol: str = Form(default="BTCUSDT"),
    category: str = Form(default="linear"),
    interval: str = Form(default="60"),
    multi_tf: str = Form(default=""),
    web_research: str = Form(default=""),
    n_iterations: int = Form(default=5),
    strict_mode: str = Form(default="strict"),
    trend_filter: str = Form(default=""),
    trend_interval: str = Form(default="60"),
    continuous: str = Form(default=""),
    source: str = Form(default="bybit"),
    instrument_id: str = Form(default=""),
    ext_interval: str = Form(default="1-DAY"),
    ext_trend_interval: str = Form(default="1-DAY"),
    max_hours: float = Form(default=0.0),
    max_total_tokens: int = Form(default=0),
):
    from server import get_market_info, templates

    n_iterations = max(2, min(15, n_iterations))
    # M22: optional budget ceilings (0 = unlimited — default behavior unchanged).
    max_hours = max(0.0, max_hours)
    max_total_tokens = max(0, max_total_tokens)
    is_strict = strict_mode != "relaxed"
    use_trend_filter = trend_filter == "1"
    is_continuous = continuous == "1"
    is_multi_tf = multi_tf == "1"
    use_web_research = web_research == "1"
    is_external = source == "external"
    instrument_id = instrument_id.strip()

    if is_external and not instrument_id:
        return HTMLResponse(
            "<div class='empty-state'>Select an instrument for the catalog source.</div>",
            status_code=400,
        )

    # Intervals to try in Multi-TF mode
    # 15m removed (6% success), Daily added (clean signal, few trades but quality)
    if is_external:
        intervals: list[str] = (
            ["1-HOUR", "4-HOUR", "1-DAY"] if is_multi_tf else [ext_interval]
        )
        trend_interval = ext_trend_interval
    else:
        intervals = ["60", "240", "D"] if is_multi_tf else [interval]

    run_id = uuid.uuid4().hex[:8]

    def _release_session_lock(evict_id: str) -> None:
        # L3: an evicted run's session-log lock is released too (no unbounded
        # Lock buildup). Runs under _AGENT_LOCK, preserving the lock-nesting
        # order (_AGENT_LOCK → _SESSION_LOG_META) that test_lock_nesting pins.
        with _SESSION_LOG_META:
            _SESSION_LOG_LOCKS.pop(evict_id, None)

    # L16: done-first — an active (continuous) run's state is never dropped; if
    # every slot is active the new run is refused (429).
    created = _AGENT_STORE.create_or_refuse(
        run_id,
        {
            "phases": [
                {"n": i, "label": lbl, "status": "pending", "detail": "", "ts": ""}
                for i, lbl in enumerate(_PHASES)
            ],
            "steps": [],
            "done": False,
            "error": None,
            "strategy_name": "",
            "stop_requested": False,
            "continuous_mode": is_continuous,
            "winner_result": None,
            "winner_spec_name": "",
            "winner_spec_id": "",
            "winner_rob": None,
            "winner_holdout": None,
            "rob_scan_log": [],
            "rob_scan_current": 0,
            "rob_scan_total": 0,
            "hint": hint.strip(),
            # Token usage (accumulated from each Claude API call)
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            # For the live backtest results table (agent screen top panel)
            "backtest_results": [],
            # Timeline spans (SVG Gantt — see _tl_begin/_tl_end)
            "timeline": [],
        },
        on_evict=_release_session_lock,
    )
    if not created:
        return HTMLResponse(
            "<div class='empty-state'>⚠ 50 active runs limit — could not start a "
            "new run. Stop one of the existing runs first.</div>",
            status_code=429,
        )

    threading.Thread(
        target=_agent_worker,
        kwargs=dict(
            run_id=run_id,
            hint=hint.strip(),
            symbol=symbol,
            category=category,
            intervals=intervals,
            n_iterations=n_iterations,
            strict_mode=is_strict,
            trend_filter=use_trend_filter,
            trend_interval=trend_interval,
            continuous_mode=is_continuous,
            web_research=use_web_research,
            source=source,
            instrument_id=instrument_id,
            max_hours=max_hours,
            max_total_tokens=max_total_tokens,
        ),
        daemon=True,
    ).start()

    # Same locked-snapshot pattern as /progress: render a consistent copy instead
    # of handing the live dict to the template while the worker mutates it concurrently.
    with _AGENT_LOCK:
        _raw0 = _AGENT_PROGRESS[run_id]
        _initial_state = {
            **_raw0,
            "phases": [dict(p) for p in _raw0["phases"]],
            "steps": list(_raw0["steps"]),
        }
    return templates.TemplateResponse(
        request,
        "fragments/agent_progress.html",
        {
            "run_id": run_id,
            "phases": _PHASES,
            "state": _initial_state,
            "done": False,
            "error": None,
            "market": get_market_info(),
            "tl": None,
            "steps_by_key": {},
        },
    )


@router.post("/stop/{run_id}", response_class=HTMLResponse)
async def stop(request: Request, run_id: str):
    """Send a stop signal to the running agent (continuous mode AND single run).

    stop_requested; checked in the iteration loop, at round start, and in the
    robustness candidate scan → stops cleanly once the current step finishes. The
    button is rendered in fragments/agent_progress.html (during the run) and in
    agent_result.html (continuous mode, between rounds).
    """
    with _AGENT_LOCK:
        s = _AGENT_PROGRESS.get(run_id)
        if s:
            s["stop_requested"] = True
    if s is None:
        # L16: state not in memory (restart/eviction) — an honest message instead
        # of a fake "signal sent" badge.
        return HTMLResponse(
            "<span class='badge' style='background:rgba(148,163,184,0.2);"
            "color:#94a3b8;'>⚠ Run not in memory (the server may have been "
            "restarted) — there is no operation to stop</span>"
        )
    return HTMLResponse(
        "<span class='badge' style='background:rgba(251,146,60,0.2);color:#fb923c;'>"
        "⏹ Stop signal sent — it will stop after the current round finishes</span>"
    )


def _terminal_message(run_id: str) -> str:
    """Honest message for a run not in memory.

    Distinguishes by the last event of the on-disk session log: a run that has
    seen session_end really finished; if the log is cut off mid-way the process
    died (typical cause: the server was restarted) — the run is not 'completed'.
    """
    generic = "The run completed or timed out."
    try:
        log_path = SESSION_LOG_DIR / f"{run_id}.jsonl"
        if not log_path.exists():
            return generic
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            tail = f.read().decode("utf-8", errors="replace").strip().splitlines()
        last = json.loads(tail[-1]) if tail else {}
        if last.get("event") == "session_end":
            return (
                "The run completed — you can review its history in the "
                "<a href='/sessions'>Session Logs</a>."
            )
        return (
            "⚠ The run was cut off mid-way (most likely the server was "
            "restarted). The steps up to where it stopped are in the "
            "<a href='/sessions'>Session Logs</a>; you can restart the "
            "agent."
        )
    except Exception:
        return generic


@router.get("/progress/{run_id}", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    import asyncio

    from server import get_market_info, templates
    from web.viewmodels import iteration_row

    with _AGENT_LOCK:
        raw = _AGENT_PROGRESS.get(run_id)
        if raw is None:
            # Run already cleaned up or unknown — return terminal no-poll frame
            return HTMLResponse(
                "<div id='agent-progress-panel'>"
                "<div class='panel'><div class='panel-body empty-state'>"
                f"{_terminal_message(run_id)}"
                "</div></div></div>"
            )
        state = {
            "phases": [dict(p) for p in raw["phases"]],
            "steps": list(raw["steps"]),
            "done": raw["done"],
            "error": raw["error"],
            "strategy_name": raw["strategy_name"],
            "winner_result": raw["winner_result"],
            "winner_spec_name": raw["winner_spec_name"],
            "winner_spec_id": raw.get("winner_spec_id", ""),
            "winner_rob": raw.get("winner_rob"),
            "winner_holdout": raw.get("winner_holdout"),
            "rob_scan_log": list(raw.get("rob_scan_log", [])),
            "rob_scan_current": raw.get("rob_scan_current", 0),
            "rob_scan_total": raw.get("rob_scan_total", 0),
            "stop_requested": raw.get("stop_requested", False),
            "continuous_mode": raw.get("continuous_mode", False),
            "tokens_in": raw.get("tokens_in", 0),
            "tokens_out": raw.get("tokens_out", 0),
            "tokens_cache_read": raw.get("tokens_cache_read", 0),
            "tokens_cache_write": raw.get("tokens_cache_write", 0),
            "backtest_results": list(raw.get("backtest_results") or []),
            # Shallow copies — the worker mutates concurrently.
            "timeline": [dict(sp) for sp in raw.get("timeline") or []],
        }

    # If continuous mode has PERMANENTLY finished, stop polling — otherwise the
    # terminal result fragment would be re-rendered/chart-rebuilt every 2s forever
    # (flicker + constant CPU/network). continuous_finished is set when the worker
    # leaves the loop for good; it stays False during the inter-round (done=True +
    # 3s sleep) window, so the transition to the next round is preserved.
    is_continuous = state.get("continuous_mode", False) and not state.get(
        "continuous_finished"
    )

    # Timeline render model (filtered to the active round) + span→step mapping.
    from web.viewmodels import associate_steps, timeline_view

    _cur_round = max((sp.get("round", 1) for sp in state["timeline"]), default=1)
    tl = timeline_view(
        state["timeline"],
        now=None if state["done"] else datetime.now(UTC).timestamp(),
        round_num=_cur_round,
    )
    steps_by_key = associate_steps(
        state["timeline"], state["steps"], round_num=_cur_round
    )

    # L38: token usage + model-based ESTIMATED cost (_MODEL_PRICING).
    # On a claude-cli/OAuth subscription there is NO per-token billing → cost None
    # (templates hide the line; when filled it is shown with an '≈ estimated' label).
    _USD_EUR = 0.91
    _ti = state.get("tokens_in", 0) or 0
    _to = state.get("tokens_out", 0) or 0
    _tcr = state.get("tokens_cache_read", 0) or 0
    _tcw = state.get("tokens_cache_write", 0) or 0
    _model, _cost_usd = _llm_cost_usd(_ti, _to, _tcr, _tcw)
    token_info = {
        "input": _ti,
        "output": _to,
        "cache_read": _tcr,
        "cache_write": _tcw,
        "total": _ti + _to + _tcr + _tcw,
        "pricing_model": _model,
        "cost_usd": round(_cost_usd, 4) if _cost_usd is not None else None,
        "cost_eur": round(_cost_usd * _USD_EUR, 4) if _cost_usd is not None else None,
    }

    if state["done"] and state["winner_result"] is not None:
        result = state["winner_result"]
        last_row = iteration_row(result)
        last_row["rationale"] = result.rationale
        last_row["equity_curve"] = result.equity_curve
        last_row["equity_dates"] = result.equity_dates
        last_row["spec_name"] = state["winner_spec_name"]
        last_row["steps"] = state["steps"][-60:]  # cap to avoid huge DOM
        # M2625: generate the narrative once, cache it in the real progress dict.
        # Previously, while done+winner was set, EVERY poll (every 2s in
        # continuous) made a new LLM API call and blocked on the response; since
        # the winner is fixed, a single generation is enough.
        with _AGENT_LOCK:
            _real = _AGENT_PROGRESS.get(run_id)
            _narr = _real.get("winner_narrative") if _real else None
        if _narr is None:
            _narr = await asyncio.to_thread(_winner_narrative, last_row, state)
            with _AGENT_LOCK:
                _real = _AGENT_PROGRESS.get(run_id)
                if _real is not None:
                    _real["winner_narrative"] = _narr
        last_row["narrative"] = _narr

        # Chart URL
        bi = result.bars_info or {}
        if bi.get("symbol"):
            _sid = state.get("winner_spec_id", "")
            last_row["chart_url"] = _chart_url(bi, _sid)
            last_row["chart_symbol"] = bi["symbol"]
            last_row["chart_category"] = bi.get("category", "linear")
            last_row["chart_interval"] = bi.get("interval", "60")

        if not is_continuous:
            # Mark done so the next poll returns the same result (no pop — HTMX
            # may fire one more request after receiving the result fragment)
            pass

        return templates.TemplateResponse(
            request,
            "fragments/agent_result.html",
            {
                "last": last_row,
                "phases": state["phases"],
                "rob_scan_log": state["rob_scan_log"],
                "winner_rob": state["winner_rob"],
                "winner_holdout": state.get("winner_holdout"),
                "market": get_market_info(),
                "run_id": run_id,
                "is_continuous": is_continuous,
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    if state["done"] and state["error"]:
        return templates.TemplateResponse(
            request,
            "fragments/agent_progress.html",
            {
                "run_id": run_id,
                "phases": _PHASES,
                "state": state,
                "done": True,
                "error": state["error"],
                "market": get_market_info(),
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    # Robustness not found but done=True (winner_result=None, error=None)
    if state["done"]:
        return templates.TemplateResponse(
            request,
            "fragments/agent_progress.html",
            {
                "run_id": run_id,
                "phases": _PHASES,
                "state": state,
                "done": True,
                "error": "No strategy passed the robustness test.",
                "rob_scan_log": state["rob_scan_log"],
                "market": get_market_info(),
                "token_info": token_info,
                "tl": tl,
                "steps_by_key": steps_by_key,
            },
        )

    return templates.TemplateResponse(
        request,
        "fragments/agent_progress.html",
        {
            "run_id": run_id,
            "phases": _PHASES,
            "state": state,
            "done": False,
            "error": None,
            "market": get_market_info(),
            "token_info": token_info,
            "tl": tl,
            "steps_by_key": steps_by_key,
        },
    )


def _winner_narrative(last_row: dict, state: dict) -> str:
    try:
        from agent import MODEL, _get_client

        m = last_row
        client = _get_client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Summarize the winning strategy found by the autonomous backtest agent in 2-3 sentences in English:\n"
                        f"Strategy: {state['winner_spec_name']}\n"
                        f"PnL: {m.get('pnl_fmt', '?')} · Sharpe: {m.get('sharpe_fmt', '?')} · "
                        f"Sortino: {m.get('sortino_fmt', '?')} · Max DD: {m.get('max_dd_fmt', '?')}\n"
                        f"Trades: {m.get('n_trades', 0)} · Win Rate: {m.get('win_rate_fmt', '?')}\n"
                        "Begin with 'This strategy'."
                    ),
                }
            ],
        )
        return resp.content[0].text.strip() if resp.content else ""
    except Exception:
        pnl = last_row.get("pnl") or 0
        return (
            f"This strategy was saved to the catalog as {state['winner_spec_name']}. "
            f"With {last_row.get('n_trades', 0)} trades it "
            f"{'gained' if pnl >= 0 else 'lost'} {last_row.get('pnl_fmt', '?')} "
            f"and passed the robustness test."
        )
