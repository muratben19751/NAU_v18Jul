"""Agent Session Logs — view all autonomous session logs.

Endpoints:
    GET /sessions          Session list
    GET /sessions/{run_id} Single session detail
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix="/sessions")

# Canonical path — shared with agent_backtest.py (import avoids duplication)
try:
    from web.routes.agent_backtest import SESSION_LOG_DIR
except ImportError:
    SESSION_LOG_DIR = Path.home() / ".cache" / "nautilus_web_app" / "agent_sessions"

# ── Helpers ───────────────────────────────────────────────────────────────────


# H7: structural events are ALWAYS kept — the cap applies only to 'step's.
# The old line-count cap silently dropped later rounds'
# backtest_result/winner/token_snapshot events (which arrive AFTER thousands
# of steps) in continuous sessions: the page would show a 10-round session as 2 rounds.
_STRUCTURAL_EVENTS = frozenset(
    {
        "session_start",
        "session_end",
        "winner",
        "backtest_result",
        "robustness_result",
        "token_snapshot",
        "timeline",
        "phase_change",
        "strategy_proposed",
        "custom_block_generated",
        "holdout_result",
        "custom_block_error",
    }
)


def _read_events(run_id: str, max_lines: int | None = None) -> tuple[list[dict], bool]:
    """Read JSONL events → (events, truncated).

    H7: ``max_lines`` limits only step-like events (the NEWEST steps are kept
    via deque); structural events are collected all the way to end of file.
    M13: only dict lines with an ``'event'`` key are accepted — a schemaless
    but valid-JSON single line was dropping the whole page to 500.
    """
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return [], False
    structural: list[tuple[int, dict]] = []
    steps: deque[tuple[int, dict]] = deque(maxlen=max_lines or None)
    n_steps_seen = 0
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or "event" not in obj:
                    continue  # M13: parse guard
                if obj["event"] in _STRUCTURAL_EVENTS:
                    structural.append((i, obj))
                else:
                    steps.append((i, obj))
                    n_steps_seen += 1
    except Exception:
        pass
    truncated = bool(max_lines) and n_steps_seen > len(steps)
    merged = sorted([*structural, *steps], key=lambda t: t[0])
    return [e for _, e in merged], truncated


def _build_timeline_spans(events: list[dict]) -> list[dict]:
    """Rebuild timeline spans from JSONL events (replay).

    Primary source: ``timeline`` events (op=begin/end — epochs in payload).
    Spans still open at EOF (crash/kill) are closed as ``warn`` with the last
    event's ISO ts. Fallback for old sessions: coarse phase spans are
    synthesized from ``phase_change`` pairs.
    """
    spans: list[dict] = []
    open_by_key: dict[str, dict] = {}
    for e in events:
        if e.get("event") != "timeline":
            continue
        if e.get("op") == "begin":
            sp = {
                "key": e.get("key", "?"),
                "lane": e.get("lane", "data"),
                "label": e.get("label", e.get("key", "?")),
                "t0": float(e.get("t0") or 0),
                "t1": None,
                "status": "running",
                "sub": bool(e.get("sub")),
                "round": int(e.get("round") or 1),
                "meta": dict(e.get("meta") or {}),
            }
            spans.append(sp)
            open_by_key[sp["key"]] = sp
        elif e.get("op") == "end":
            sp = open_by_key.pop(e.get("key", ""), None)
            if sp is not None:
                sp["t1"] = float(e.get("t1") or sp["t0"])
                sp["status"] = e.get("status", "ok")
                sp["meta"].update(e.get("meta") or {})

    if spans:
        if open_by_key:
            # Session interrupted midway — close open ones with the last event's time.
            last_t = max(
                (sp["t1"] for sp in spans if sp["t1"] is not None),
                default=None,
            )
            if last_t is None:
                try:
                    last_t = datetime.fromisoformat(
                        events[-1].get("ts", "")
                    ).timestamp()
                except (ValueError, TypeError):
                    last_t = max(sp["t0"] for sp in open_by_key.values()) + 1.0
            for sp in open_by_key.values():
                sp["t1"] = max(last_t, sp["t0"])
                sp["status"] = "warn"
        return spans

    # ── Fallback: no timeline events (old session) → from phase_change ──
    _lane = {
        0: "data",
        1: "llm",
        2: "backtest",
        3: "backtest",
        4: "robustness",
        5: "data",
    }
    open_phase: dict[int, dict] = {}
    rnd = 1
    for e in events:
        ev = e.get("event")
        if ev == "step" and "Continuous mode: round" in str(e.get("msg", "")):
            rnd += 1
            continue
        if ev != "phase_change":
            continue
        try:
            t = datetime.fromisoformat(e.get("ts", "")).timestamp()
        except (ValueError, TypeError):
            continue
        idx = int(e.get("phase_idx") or 0)
        if e.get("status") == "running":
            sp = {
                "key": f"phase-{idx}-r{rnd}-{len(spans)}",
                "lane": _lane.get(idx, "data"),
                "label": e.get("phase_label", f"Phase {idx}"),
                "t0": t,
                "t1": None,
                "status": "running",
                "sub": False,
                "round": rnd,
                "meta": {},
            }
            spans.append(sp)
            open_phase[idx] = sp
        elif e.get("status") == "done" and idx in open_phase:
            sp = open_phase.pop(idx)
            sp["t1"] = t
            sp["status"] = "ok"
    last_t = max((sp["t1"] for sp in spans if sp["t1"] is not None), default=None)
    for sp in open_phase.values():
        sp["t1"] = last_t if last_t is not None else sp["t0"] + 1.0
        sp["status"] = "warn"
    return spans


# M24: summary cache keyed by (mtime_ns, size) — /sessions was re-parsing the
# ENTIRE corpus (jsonl's that can be hundreds of MB) on every visit. Closed
# sessions' files don't change → their summaries are computed once; only the
# active (growing) file is re-read. Invalidation is in the key itself.
_SUMMARY_CACHE: dict[str, tuple[tuple, dict]] = {}


def _session_summary(run_id: str) -> dict:
    """Fast summary for the session list — read only key events."""
    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    try:
        st = path.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _SUMMARY_CACHE.get(run_id)
        if cached and cached[0] == cache_key:
            return cached[1]
    size_mb = round(path.stat().st_size / 1_048_576, 1) if path.exists() else 0

    start_ev = end_ev = tok_ev = winner_ev = None
    n_backtest = n_phase = n_step = n_rob = n_custom = 0
    last_ts = ""

    try:
        fh = path.open()
    except OSError:
        return {
            "run_id": run_id,
            "ts_start": "—",
            "ts_end": "—",
            "elapsed": "",
            "size_mb": size_mb,
            "symbol": "—",
            "intervals": [],
            "n_iterations": "—",
            "continuous": False,
            "hint": "",
            "outcome": "unreadable",
            "total_rounds": "?",
            "n_backtest": 0,
            "n_rob": 0,
            "n_custom": 0,
            "n_step": 0,
            "winner_spec": "",
            "winner_score": None,
            "cost_eur": None,
            "cost_usd": None,
            "has_blocks": False,
        }
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event", "")
            _ts = e.get("ts", "")
            # M251: step events' ts is 'HH:MM:SS' (dateless — _add_step overwrites
            # ISO); if the last line was a step, ts_end appeared as a clock value and
            # broke elapsed. Update last_ts only from FULL ISO ts's (containing a date)
            # — ignore steps' short ts.
            if _ts and ("T" in _ts or _ts[:4].isdigit()):
                last_ts = _ts
            if ev == "session_start":
                start_ev = e
            elif ev == "session_end":
                end_ev = e
            elif ev == "token_snapshot":
                tok_ev = e
            elif ev == "winner":
                winner_ev = e
            elif ev == "backtest_result":
                n_backtest += 1
            elif ev == "phase_change":
                n_phase += 1
            elif ev == "step":
                n_step += 1
            elif ev == "robustness_result":
                n_rob += 1
            elif ev == "custom_block_generated":
                n_custom += 1

    ts_start = (start_ev or {}).get("ts", "")
    ts_end = last_ts
    # Elapsed
    elapsed = ""
    try:
        if ts_start and ts_end:
            a = datetime.fromisoformat(ts_start)
            b = datetime.fromisoformat(ts_end)
            secs = int((b - a).total_seconds())
            elapsed = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    except Exception:
        pass

    outcome = (end_ev or {}).get("outcome", "running" if not end_ev else "unknown")
    if winner_ev and not end_ev:
        outcome = "winner_found"

    total_rounds = (
        (tok_ev or {}).get("round") or (end_ev or {}).get("total_rounds") or "?"
    )

    summary = {
        "run_id": run_id,
        "ts_start": ts_start[:19].replace("T", " ") if ts_start else "—",
        "ts_end": ts_end[:19].replace("T", " ") if ts_end else "—",
        "elapsed": elapsed,
        "size_mb": size_mb,
        "symbol": (start_ev or {}).get("symbol", "—"),
        "intervals": (start_ev or {}).get("intervals", []),
        "n_iterations": (start_ev or {}).get("n_iterations", "—"),
        "continuous": (start_ev or {}).get("continuous_mode", False),
        "hint": (start_ev or {}).get("hint", ""),
        "outcome": outcome,
        "total_rounds": total_rounds,
        "n_backtest": n_backtest,
        "n_rob": n_rob,
        "n_custom": n_custom,
        "n_step": n_step,
        "winner_spec": (winner_ev or {}).get("spec_name", ""),
        "winner_score": (winner_ev or {}).get("score"),
        "cost_eur": (tok_ev or {}).get("cost_eur"),
        "cost_usd": (tok_ev or {}).get("cost_usd"),
        "has_blocks": (SESSION_LOG_DIR / f"{run_id}_blocks").exists(),
    }
    if cache_key is not None:
        _SUMMARY_CACHE[run_id] = (cache_key, summary)
    return summary


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def sessions_list(request: Request):
    import asyncio

    from server import get_market_info, templates

    if not SESSION_LOG_DIR.exists():
        sessions = []
    else:
        jsonl_files = sorted(
            SESSION_LOG_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Run blocking file I/O in thread pool to avoid blocking the event loop
        sessions = await asyncio.gather(
            *[asyncio.to_thread(_session_summary, p.stem) for p in jsonl_files]
        )

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "active": "sessions",
            "page_title": "Session Logs",
            "market": get_market_info(),
            "sessions": sessions,
        },
    )


@router.get("/{run_id}", response_class=HTMLResponse)
async def session_detail(request: Request, run_id: str):
    import asyncio

    from server import get_market_info, templates

    # Security: only allow hex run_id's
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return HTMLResponse("Invalid run_id", status_code=400)

    path = SESSION_LOG_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return HTMLResponse("Session not found", status_code=404)

    # H7: RAM protection applies to steps (newest 20k steps, deque);
    # structural events (backtest_result/winner/token_snapshot…) are ALWAYS
    # read to end of file — later rounds of continuous sessions
    # are no longer lost. The truncated flag prints a warning in the template.
    events, steps_truncated = await asyncio.to_thread(_read_events, run_id, 20_000)

    # Group by event type — group steps by round
    # (M13: filters use .get — a line without 'event' was already dropped by the parse guard)
    session_start = next((e for e in events if e.get("event") == "session_start"), {})
    session_end = next((e for e in events if e.get("event") == "session_end"), None)
    token_snaps = [e for e in events if e.get("event") == "token_snapshot"]
    winners = [e for e in events if e.get("event") == "winner"]
    backtests = [e for e in events if e.get("event") == "backtest_result"]
    # In log files before Audit-91, backtest_result events may lack the 'score'
    # field; the template's sort(attribute='score') call would drop the page to
    # 500 on an Undefined comparison. Fill missing/None score with a sortable
    # -inf (no-op on existing records).
    for _bt in backtests:
        if _bt.get("score") is None:
            _bt["score"] = float("-inf")
    robustness = [e for e in events if e.get("event") == "robustness_result"]
    proposals = [e for e in events if e.get("event") == "strategy_proposed"]
    custom_blocks = [e for e in events if e.get("event") == "custom_block_generated"]
    steps = [e for e in events if e.get("event") == "step"]

    # Per-round backtest summaries
    rounds: dict[int, dict] = {}
    for bt in backtests:
        r = bt.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["backtests"].append(bt)
    for rob in robustness:
        r = rob.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["robustness"].append(rob)
    for pr in proposals:
        r = pr.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["proposals"].append(pr)
    for cb in custom_blocks:
        r = cb.get("round") or 1
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
            }
        rounds[r]["custom_blocks"].append(cb)

    # Blocks directory
    blocks_dir = SESSION_LOG_DIR / f"{run_id}_blocks"
    block_files = sorted(blocks_dir.glob("*.py")) if blocks_dir.exists() else []
    block_codes = []
    for bf in block_files:
        try:
            block_codes.append({"name": bf.stem, "code": bf.read_text()})
        except Exception:
            block_codes.append({"name": bf.stem, "code": "(could not read)"})

    # Last token snapshot
    last_tok = token_snaps[-1] if token_snaps else {}

    # Elapsed
    elapsed = ""
    ts_start = session_start.get("ts", "")
    ts_last = events[-1].get("ts", "") if events else ""
    try:
        if ts_start and ts_last:
            a = datetime.fromisoformat(ts_start)
            b = datetime.fromisoformat(ts_last)
            secs = int((b - a).total_seconds())
            elapsed = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
    except Exception:
        pass

    # Step timeline (last 200 — don't show all on large files)
    step_timeline = steps[-200:]

    # Timeline replay — per-round render model
    from web.viewmodels import associate_steps, timeline_view

    spans = _build_timeline_spans(events)
    tl_by_round: dict[int, dict] = {}
    for r in {sp.get("round", 1) for sp in spans}:
        tl = timeline_view(spans, round_num=r)
        if tl:
            tl_by_round[r] = {
                "tl": tl,
                "steps_by_key": associate_steps(spans, steps, round_num=r),
            }
    for r, rd in rounds.items():
        rd["timeline"] = tl_by_round.get(r)
    # Rounds with spans but no backtests (e.g. early error) should also show
    for r, tlr in tl_by_round.items():
        if r not in rounds:
            rounds[r] = {
                "backtests": [],
                "robustness": [],
                "proposals": [],
                "custom_blocks": [],
                "timeline": tlr,
            }

    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "active": "sessions",
            "page_title": f"Session {run_id}",
            "market": get_market_info(),
            "run_id": run_id,
            "session_start": session_start,
            "session_end": session_end,
            "elapsed": elapsed,
            "last_tok": last_tok,
            "winners": winners,
            "rounds": dict(sorted(rounds.items())),
            "block_codes": block_codes,
            "step_timeline": step_timeline,
            "n_events": len(events),
            "n_steps": len(steps),
            "steps_truncated": steps_truncated,
            "size_mb": round(path.stat().st_size / 1_048_576, 1),
        },
    )


@router.get("/{run_id}/block/{block_name}", response_class=Response)
async def get_block_code(run_id: str, block_name: str):
    """Return a single block's Python code."""
    if not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 8:
        return Response("Invalid run_id", status_code=400)
    # sanitize block_name
    safe = "".join(c for c in block_name if c.isalnum() or c == "_")
    path = SESSION_LOG_DIR / f"{run_id}_blocks" / f"{safe}.py"
    if not path.exists():
        return Response("Not found", status_code=404)
    return Response(path.read_text(), media_type="text/plain")
