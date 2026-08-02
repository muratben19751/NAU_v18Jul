"""AUTO Mission Control — render-model + template contract tests.

Covers the mapping in ``web/mission.py`` and asserts that the cockpit fragment
renders for the states the loop actually produces (live, stopping, finished),
so a template rename or a missing key fails here instead of in the browser.
"""

from __future__ import annotations

import pytest

from web.mission import fmt_clock, fmt_tokens, mission_view

PHASE_LABELS = [
    "Loading data",
    "Generating strategy",
    "Backtest loop",
    "Ranking",
    "Robustness scan",
    "Completed",
]


def _state(running_idx: int | None = 2, **over) -> dict:
    phases = []
    for i, lbl in enumerate(PHASE_LABELS):
        if running_idx is None:
            status = "done"
        elif i < running_idx:
            status = "done"
        elif i == running_idx:
            status = "running"
        else:
            status = "pending"
        phases.append({"label": lbl, "status": status, "detail": "", "ts": ""})
    st = {
        "phases": phases,
        "steps": [{"ts": "13:29:44", "msg": "Data: QQQ.NASDAQ 1-DAY · 4,107 bar"}],
        "done": running_idx is None,
        "error": None,
        "stop_requested": False,
        "started_at": 0.0,
        "max_hours": 0.0,
        "max_total_tokens": 0,
        "brief": {
            "symbol": "BTCUSDT",
            "category": "linear",
            "model": "Fable 5",
            "robustness": "Strict",
            "range": "max",
            "iterations": 5,
            "guidance": "",
            "tfs": ["60"],
            "continuous": False,
        },
        "backtest_results": [],
    }
    st.update(over)
    return st


def _bt(i: int, score: float, **over) -> dict:
    row = {
        "iter": i,
        "round": 1,
        "interval": "60",
        "spec_name": f"strat-{i}",
        "score": score,
        "sharpe": 1.1,
        "profit_factor": 1.6,
        "max_dd": -0.11,
        "n_trades": 300,
        "equity": [100.0, 101.0, 99.0, 120.0],
        "error": None,
    }
    row.update(over)
    return row


# ── formatters ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sec,expected",
    [(0, "0s"), (9, "9s"), (69, "1m 09s"), (3600, "1h 00m"), (None, "0s")],
)
def test_fmt_clock(sec, expected):
    assert fmt_clock(sec) == expected


@pytest.mark.parametrize(
    "n,expected", [(0, "0"), (999, "999"), (11500, "11.5K"), (2_500_000, "2.50M")]
)
def test_fmt_tokens(n, expected):
    assert fmt_tokens(n) == expected


# ── phase strip ───────────────────────────────────────────────────────────


def test_phase_strip_has_five_cells_and_one_active():
    mv = mission_view(_state(running_idx=2), now=10)
    assert [c["label"] for c in mv["cells"]] == [
        "DATA",
        "STRATEJİ",
        "BACKTEST",
        "ROBUSTNESS",
        "KATALOG",
    ]
    assert [c["state"] for c in mv["cells"]].count("active") == 1
    assert mv["cells"][2]["state"] == "active"


def test_ranking_folds_into_robustness_cell():
    # Agent phase 3 (Ranking) has no cell of its own; while it runs the
    # ROBUSTNESS cell must be the active one.
    mv = mission_view(_state(running_idx=3), now=10)
    assert mv["cells"][3]["state"] == "active"
    assert mv["cells"][2]["state"] == "done"


# ── status / headline ─────────────────────────────────────────────────────


def test_running_status():
    mv = mission_view(_state(), now=10)
    assert mv["running"] is True
    assert mv["status_label"] == "RUNNING"
    assert mv["headline"] == "Backtest loop"


def test_stop_requested_flips_status_and_headline():
    mv = mission_view(_state(stop_requested=True), now=10)
    assert mv["running"] is False
    assert mv["stopping"] is True
    assert mv["status_label"] == "DURDURULDU"
    assert mv["headline"] == "Döngü duraklatıldı"
    assert mv["cells"][2]["value"] == "duraklatıldı"


def test_finished_run_shows_dash_ring():
    mv = mission_view(_state(running_idx=None), now=10)
    assert mv["done"] is True
    assert mv["progress_label"] == "—"


def test_stopped_ring_is_empty_but_iteration_count_is_kept():
    # The design specifies an EMPTY ring with a dash when stopped, so a paused
    # loop never reads as still making progress — while the iteration counter
    # keeps showing where the loop actually got to.
    st = _state(stop_requested=True, backtest_results=[_bt(1, 1.0), _bt(2, 2.0)])
    mv = mission_view(st, now=10)
    assert mv["progress_label"] == "—"
    assert "0turn 0.0000turn" in mv["ring_bg"]
    assert mv["iter"] == 3
    assert mv["progress"] == 40  # the underlying number is still available


def test_stopped_run_keeps_active_card_and_marks_it_paused():
    st = _state(stop_requested=True, backtest_results=[_bt(1, 1.0)])
    active = [
        c for c in mission_view(st, now=10)["candidates"] if c["state"] == "active"
    ]
    assert len(active) == 1
    assert active[0]["badge"] == "duraklatıldı"


# ── progress / iterations ─────────────────────────────────────────────────


def test_progress_tracks_completed_iterations():
    st = _state(backtest_results=[_bt(1, 1.0), _bt(2, 2.0)])
    mv = mission_view(st, now=10)
    assert mv["progress"] == 40  # 2 of 5
    assert mv["iter"] == 3
    assert mv["iter_of"] == "/5"


def test_progress_is_zero_without_results():
    assert mission_view(_state(), now=10)["progress"] == 0


# ── candidates ────────────────────────────────────────────────────────────


def test_leader_is_highest_score_and_sorts_first():
    st = _state(backtest_results=[_bt(1, 1.0), _bt(2, 2.5), _bt(3, 0.4)])
    cands = mission_view(st, now=10)["candidates"]
    assert cands[0]["state"] == "leader"
    assert cands[0]["title"].startswith("#2")
    assert cands[0]["score"].startswith("★")


def test_errored_row_cannot_be_leader():
    st = _state(
        backtest_results=[_bt(1, 1.0), _bt(2, None, error="boom", equity=[])],
    )
    cands = mission_view(st, now=10)["candidates"]
    leaders = [c for c in cands if c["state"] == "leader"]
    assert len(leaders) == 1
    assert leaders[0]["title"].startswith("#1")


def test_queue_fills_up_to_iteration_budget():
    st = _state(backtest_results=[_bt(1, 1.0)])
    cands = mission_view(st, now=10)["candidates"]
    assert [c["state"] for c in cands].count("active") == 1
    assert [c["state"] for c in cands].count("queued") == 3  # #3..#5


def test_no_active_or_queued_card_when_finished():
    st = _state(running_idx=None, backtest_results=[_bt(1, 1.0)])
    states = {c["state"] for c in mission_view(st, now=10)["candidates"]}
    assert "active" not in states and "queued" not in states


def test_only_current_round_candidates_are_shown():
    st = _state(
        backtest_results=[_bt(1, 9.0, round=1), _bt(1, 1.0, round=2)],
    )
    cands = mission_view(st, now=10)["candidates"]
    scored = [c for c in cands if c["state"] in ("leader", "past")]
    assert len(scored) == 1
    assert mv_round(st) == 2


def mv_round(st: dict) -> int:
    return mission_view(st, now=10)["round"]


def test_sparkline_is_emitted_and_bounded():
    st = _state(backtest_results=[_bt(1, 1.0, equity=list(range(500)))])
    c = mission_view(st, now=10)["candidates"][0]
    assert c["spark"]
    assert len(c["spark"].split(" ")) <= 40


# ── budget gauges ─────────────────────────────────────────────────────────


def test_unlimited_budget_shows_sliver_not_full_bar():
    mv = mission_view(_state(), now=3600, token_info={"total": 5_000_000})
    assert mv["time_pct"] == 6
    assert mv["token_pct"] == 6


def test_bounded_budget_is_proportional_and_clamped():
    st = _state(max_hours=1.0, max_total_tokens=1000)
    mv = mission_view(st, now=1800, token_info={"total": 500})
    assert mv["time_pct"] == 50
    assert mv["token_pct"] == 50
    mv2 = mission_view(st, now=99999, token_info={"total": 10**9})
    assert mv2["time_pct"] == 100
    assert mv2["token_pct"] == 100


# ── console ───────────────────────────────────────────────────────────────


def test_console_tail_is_newest_step_and_history_is_capped():
    steps = [{"ts": f"13:00:{i:02d}", "msg": f"step {i}"} for i in range(30)]
    mv = mission_view(_state(steps=steps), now=10)
    assert mv["tail_line"] == "step 29"
    assert len(mv["lines"]) <= 9
    # Newest first (template renders column-reverse).
    assert mv["lines"][0]["m"] == "step 28"


def test_console_kind_tags_use_real_agent_messages():
    # Verbatim samples of _add_step() lines from web/routes/agent_backtest.py.
    cases = [
        ("Loading catalog data (QQQ.NASDAQ, 1-HOUR)…", "data"),
        ("1m cropped to the last 2 years (52,560 bars)", "data"),
        ("  ⚙ Generating custom entry block: ema_cross…", "llm"),
        ("🌐 Running web research…", "llm"),
        ("[2/5] Backtest [1H]: momentum-v2…", "test"),
        ("🎲 Monte Carlo — shuffles the sequence of 300 trades", "rob"),
        ("[1/3] Robustness: donchian [4H]", "rob"),
        ("🔒 Sealed OOS (90d): PnL=+120", "rob"),
        ("📊 IS/OOS Split — 70% of data for training", "rob"),
    ]
    steps = [{"ts": f"13:00:{i:02d}", "msg": m} for i, (m, _) in enumerate(cases)]
    mv = mission_view(_state(steps=steps), now=10)
    # tail_line is the newest and is not in `lines`; check the rest by message.
    got = {ln["m"]: ln["k"] for ln in mv["lines"]}
    for msg, expected in cases[:-1]:
        key = msg.strip()  # the view trims leading indent used for sub-steps
        assert got[key] == expected, f"{key!r} → {got[key]} (expected {expected})"


# ── template contract ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "st",
    [
        _state(),
        _state(stop_requested=True),
        _state(running_idx=None),
        _state(backtest_results=[_bt(1, 1.0), _bt(2, 2.0)]),
    ],
)
def test_fragment_renders_for_every_state(st):
    from server import templates

    mv = mission_view(st, run_id="deadbeef", now=10)
    mv["error"] = None
    html = templates.get_template("fragments/auto_mission.html").render(mv=mv)
    assert "mc-body" in html
    assert "agent-progress-panel" in html
    # A live run must keep polling; a finished one must not.
    assert ("hx-trigger" in html) is (not st["done"])
