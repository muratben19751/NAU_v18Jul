"""AUTO Mission Control — render model for the single-screen cockpit.

Design source: ``design_handoff_auto_mission_control/`` (option 1c). This module
turns the agent run state (``web.routes.agent_backtest._AGENT_PROGRESS[run_id]``)
into the flat dict the ``fragments/auto_mission.html`` template renders, so the
template stays presentation-only and the mapping is unit-testable.

Wiki References
---------------
See: [[auto_mission_control]], [[model_secici_ve_gorunurluk]],
[[tear_sheet_overlay]], [[webapp_module_map]]
"""

from __future__ import annotations

# Agent phase index → mission-control phase cell. The agent has six phases
# (see agent_backtest._PHASES) but the cockpit shows five: "Ranking" is folded
# into ROBUSTNESS because to the user it is one "pick + validate" stage.
_CELLS: list[tuple[str, tuple[int, ...]]] = [
    ("DATA", (0,)),
    ("STRATEGY", (1,)),
    ("BACKTEST", (2,)),
    ("ROBUSTNESS", (3, 4)),
    ("CATALOG", (5,)),
]

_KIND_COLORS = {
    "data": "#5ec9a0",
    "llm": "#d9a441",
    "test": "#e0762e",
    "rob": "#d9755d",
}


def _step_kind(msg: str) -> str:
    """Classify an agent step log line into a console kind tag.

    Patterns are taken from the actual ``_add_step`` call sites in
    ``web/routes/agent_backtest.py`` (see tests/test_auto_mission.py), ordered
    most-specific first: robustness lines mention backtests too, and block
    generation mentions strategies, so the order carries meaning.
    """
    m = msg.lower()
    if any(
        k in m
        for k in (
            "robustness",
            "monte carlo",
            "walk-forward",
            "is/oos",
            "sealed",
            "holdout",
            "multi-symbol",
            "overfitting",
        )
    ):
        return "rob"
    if any(
        k in m
        for k in (
            "loading catalog data",
            "date window",
            "cropped",
            "bar",
            "fetch error",
            "missing tf",
        )
    ):
        return "data"
    if any(
        k in m
        for k in (
            "block",
            "proposal",
            "web research",
            "claude",
            "strateji",
            "generating",
        )
    ):
        return "llm"
    return "test"


def fmt_clock(seconds: float | None) -> str:
    """Seconds → compact ``2m 09s`` / ``1h 04m`` budget label."""
    if not seconds or seconds < 0:
        return "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def fmt_tokens(n: int | None) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def _sparkline(points: list[float], w: int = 260, h: int = 46, cap: int = 40) -> str:
    """Equity points → ``<polyline points="...">`` coordinate string.

    Downsamples to ``cap`` points: the route already thins the stored curve, but
    a state built elsewhere (session replay, tests) may carry the raw curve and
    a 1s poll must never ship 100k coordinates.
    """
    if not points or len(points) < 2:
        return ""
    if len(points) > cap:
        step_i = (len(points) - 1) / (cap - 1)
        points = [points[int(round(i * step_i))] for i in range(cap)]
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = w / (len(points) - 1)
    return " ".join(
        f"{i * step:.1f},{h - ((v - lo) / span) * (h - 4) - 2:.1f}"
        for i, v in enumerate(points)
    )


def _candidates(state: dict, running: bool, stopping: bool = False) -> list[dict]:
    """Backtest results → right-rail candidate cards (leader first).

    ``running`` controls whether the in-flight and queued placeholders are
    drawn at all; ``stopping`` keeps the in-flight card but marks it paused
    (the iteration is not abandoned, it finishes the current step).
    """
    rows = [r for r in state.get("backtest_results") or []]
    cur_round = max((r.get("round", 1) for r in rows), default=1)
    rows = [r for r in rows if r.get("round", 1) == cur_round]
    scored = [r for r in rows if r.get("score") is not None and not r.get("error")]
    leader_key = None
    if scored:
        leader = max(scored, key=lambda r: r["score"])
        leader_key = (leader.get("iter"), leader.get("interval"))

    out: list[dict] = []
    for r in rows:
        key = (r.get("iter"), r.get("interval"))
        is_leader = key == leader_key
        pts = r.get("equity") or []
        out.append(
            {
                "state": "leader" if is_leader else "past",
                "title": f"#{r.get('iter', '?')} {r.get('spec_name') or '—'}",
                "score": ("★ " if is_leader else "")
                + (f"{r['score']:.2f}" if r.get("score") is not None else "—"),
                "spark": _sparkline(pts, h=46 if is_leader else 36),
                "spark_h": 46 if is_leader else 36,
                "metrics": [
                    f"PF {r['profit_factor']:.2f}"
                    if r.get("profit_factor") is not None
                    else "PF —",
                    f"DD {r['max_dd'] * 100:.1f}%"
                    if r.get("max_dd") is not None
                    else "DD —",
                    f"{r.get('n_trades', 0)} tr",
                ],
                # Backtest-log timestamp captured when this iteration was
                # logged — the tear sheet's key. Empty for a run that started
                # before the loop recorded it; the card then has no link
                # rather than a broken one.
                "ts": r.get("ts") or "",
            }
        )
    # Newest first: the just-finished iteration should sit near the top, but the
    # leader always leads (design: leader card at the top of the rail).
    out.sort(key=lambda c: 0 if c["state"] == "leader" else 1)

    total = int((state.get("brief") or {}).get("iterations") or 0)
    done_n = len(rows)
    if running and done_n < total:
        out.append(
            {
                "state": "active",
                "title": f"#{done_n + 1} running",
                "badge": "paused" if stopping else None,
                "note": "loop paused" if stopping else "backtest in progress",
            }
        )
        for i in range(done_n + 2, total + 1):
            out.append({"state": "queued", "title": f"#{i} queued"})
    return out


def _model_label(picked: str | None) -> str:
    """Brief'teki model seçiminin okunur adı; boş seçim = uygulama varsayılanı.

    ``agent`` tembel import ediliyor: bu modül saf bir render katmanı ve
    testleri LLM istemcisi kurmadan çalışabilmeli. Import edilemezse ham değer
    (ya da dürüstçe "default") döner — uydurma model adı yok.
    """
    try:
        from agent import model_label
    except Exception:
        return (picked or "").strip() or "default"
    return model_label(picked)


def _model_id(picked: str | None) -> str:
    """``_model_label`` ile aynı çözüm, ham id — rozetin title'ında gösterilir."""
    try:
        from agent import model_id
    except Exception:
        return (picked or "").strip()
    return model_id(picked)


def mission_view(
    state: dict,
    *,
    run_id: str = "",
    now: float | None = None,
    token_info: dict | None = None,
) -> dict:
    """Agent run state → AUTO Mission Control render model."""
    from datetime import UTC, datetime

    now = now if now is not None else datetime.now(UTC).timestamp()
    brief = dict(state.get("brief") or {})
    phases = state.get("phases") or []
    done = bool(state.get("done"))
    stopping = bool(state.get("stop_requested"))
    running = not done

    def ph(i: int) -> dict:
        return phases[i] if 0 <= i < len(phases) else {}

    # ── Phase strip ───────────────────────────────────────────────────────
    cells = []
    for label, idxs in _CELLS:
        sts = [ph(i).get("status", "pending") for i in idxs]
        if all(s == "done" for s in sts):
            cell_state, value = "done", "✓ done"
        elif "running" in sts:
            cell_state, value = (
                "active",
                ("paused" if stopping else "▸ running"),
            )
        else:
            cell_state, value = "pending", "waiting"
        detail = next((ph(i).get("detail") for i in idxs if ph(i).get("detail")), "")
        cells.append({"label": label, "state": cell_state, "value": detail or value})

    # ── Hero: current phase + ring ────────────────────────────────────────
    cur_i = next(
        (i for i, p in enumerate(phases) if p.get("status") == "running"),
        None,
    )
    rows = list(state.get("backtest_results") or [])
    cur_round = max((r.get("round", 1) for r in rows), default=1)
    done_n = len([r for r in rows if r.get("round", 1) == cur_round])
    total = int(brief.get("iterations") or 0)
    iter_no = min(done_n + 1, total) if (running and total) else done_n
    progress = int(round(100 * done_n / total)) if total else 0

    if not running:
        headline = (
            "Loop completed" if not state.get("error") else "Loop ended with an error"
        )
        subline = state.get("error") or "Results below · archived in Session Logs"
    elif stopping:
        headline = "Loop paused"
        subline = "Press START at the top right to continue…"
    else:
        headline = ph(cur_i or 0).get("label") or "Preparing"
        subline = ph(cur_i or 0).get("detail") or ""

    # ── Console: newest first (template renders column-reverse) ───────────
    lines = []
    for st in list(state.get("steps") or [])[-40:]:
        msg = (st.get("msg") or "").strip()
        if not msg:
            continue
        k = _step_kind(msg)
        lines.append({"t": st.get("ts", ""), "k": k, "c": _KIND_COLORS[k], "m": msg})
    tail = lines[-1] if lines else {"t": "", "m": "starting…"}
    lines = list(reversed(lines[:-1]))[:9]

    # ── Budget gauges ─────────────────────────────────────────────────────
    # `or now` would be wrong here: started_at is a unix timestamp and a test /
    # replay state can legitimately carry 0.0, which is falsy.
    started = state.get("started_at")
    started = now if started is None else started
    elapsed = max(0.0, now - started)
    max_hours = float(state.get("max_hours") or 0)
    tokens = int((token_info or {}).get("total") or 0)
    max_tokens = int(state.get("max_total_tokens") or 0)

    def pct(v: float, cap: float) -> int:
        if cap <= 0:
            return 6  # unlimited: a thin "alive" sliver, not a full bar
        return int(min(100, max(0, round(100 * v / cap))))

    tf_label = "+".join(_tf_short(t) for t in (brief.get("tfs") or [])) or "—"
    ring = progress if (running and not stopping) else 0

    return {
        "run_id": run_id,
        "running": running and not stopping,
        "stopping": stopping,
        "done": done,
        "brief": brief,
        "tf_label": tf_label,
        # Boş seçim = uygulama varsayılanı; kokpitte "default" yerine gerçek
        # model adı yazsın diye burada çözülür (agent.model_label).
        "model_label": _model_label(brief.get("model")),
        "model_id": _model_id(brief.get("model")),
        # Boş effort'u "medium" gibi bir seviyeyle DOLDURMUYORUZ: ucun kendi
        # varsayılanı sürüme göre değişir, uydurulan seviye yanlış bilgi olur.
        "effort_label": (brief.get("effort") or "").strip() or "varsayılan",
        "guidance_label": brief.get("guidance") or "— autonomous",
        "elapsed_label": fmt_clock(elapsed),
        "time_pct": pct(elapsed, max_hours * 3600),
        "token_label": fmt_tokens(tokens),
        "token_pct": pct(tokens, max_tokens),
        "cost_label": _cost_label(token_info),
        "progress": progress,
        # Stopped/finished: the design shows an empty ring with a dash, so the
        # number never looks like live progress.
        "progress_label": f"{progress}%" if (running and not stopping) else "—",
        "ring_bg": (
            f"conic-gradient(#e0762e 0turn {ring / 100:.4f}turn,"
            f"#241c17 {ring / 100:.4f}turn 1turn)"
        ),
        "status_label": "RUNNING" if (running and not stopping) else "STOPPED",
        # Degradasyon rozeti: kaç üretim adımı fallback'e düştü. Fazlar ✓ kapansa
        # bile bu sayı >0 ise koşunun ürettiği şey modelin önerisi değildir —
        # rozet olmadan "başarılı koşu" görüntüsü yalan söyler.
        "degraded_count": int(state.get("fallback_count") or 0),
        "degraded_reasons": ", ".join(state.get("fallback_reasons") or []),
        "headline": headline,
        "subline": subline,
        "iter": iter_no,
        "iter_of": f"/{total}" if total else "",
        "round": cur_round,
        "cells": cells,
        "lines": lines,
        "tail_line": tail["m"],
        "tail_time": tail["t"],
        "console_meta": f"{fmt_clock(elapsed)} · {len(state.get('steps') or [])} steps",
        "candidates": _candidates(state, running, stopping=stopping),
        "winner_spec_id": state.get("winner_spec_id") or "",
    }


def _tf_short(code: str) -> str:
    return {
        "1": "1M",
        "5": "5M",
        "15": "15M",
        "30": "30M",
        "60": "1H",
        "240": "4H",
        "720": "12H",
        "D": "1D",
    }.get(str(code), str(code))


def _cost_label(token_info: dict | None) -> str:
    ti = token_info or {}
    total = fmt_tokens(ti.get("total"))
    usd = ti.get("cost_usd")
    return f"{total} · ${usd:.2f}" if usd is not None else total
