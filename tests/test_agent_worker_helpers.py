"""_agent_worker decomposition (2026-08-08 DeepR #47), steps A1-A7:
- A1: the three pure closures `_market_for`/`_iv_for`/`_recipe` were promoted
  to module-level functions in web/routes/agent_backtest.py so they can be
  unit-tested in isolation, instead of only indirectly through a full worker
  run.
- A2: the ~10 round-persistent locals were collected into a `_WorkerState`
  dataclass (mechanical rename only, no logic change — verified by the full
  existing suite passing unchanged).
- A3: `_cleanup_generated`/`_winless_bump`/`_winless_stop` promoted to module
  functions taking `wstate` explicitly instead of closing over it.
- A4: `_make_llm_control` factory promoted to a module function — this is the
  cost/time budget enforcement gate, previously zero-coverage.
- A5: `_rank_and_filter` (Phase 3's pure ranking/filtering/logging) promoted
  to a module function; the winnerless return/continue control flow stays
  in `_agent_worker`.
- A6: `_propose_initial_strategy` (Phase 1) promoted to a module function,
  doing its own local imports (mirrors `_generate_custom_spec`'s established
  pattern in this file).
- A7: `_scan_one_candidate` (Phase 4's per-candidate robustness check)
  promoted to a module function. Caught two real extraction bugs before
  testing: the "effective score" log message referenced the caller's
  `len(passers)` (fixed via an explicit `n_passers_so_far` parameter), and
  `_MAX_PASSERS` was a local of `_agent_worker` unreachable from the new
  module function (promoted to a module constant).
- A8: `_run_promotion_gate` (Phase 5's sealed-holdout financial-integrity
  gate) promoted to a module function. Catalog append, the _AGENT_PROGRESS
  winner-fields write, and the final winner/session_end logging stay in
  `_agent_worker`.
- A9: `_run_backtest_iteration` (Phase 2's run-backtest-and-log-result half)
  promoted to a module function; the 'propose next spec' lookahead-generation
  block (deferred #47-A10 in the plan) stays in `_agent_worker`.
- A11a: `_load_timeframe_bars` (Phase 0's source-agnostic bar loader, the
  `_load_tf_uncached` extraction) promoted to a module function — no cache
  dict mutation inside it, the caller owns tf_cache. Highest-residual-risk
  step in the decomposition plan; regression-gated on the UNMODIFIED
  test_agent_fixes.py::TestContinuousCircuitBreaker passing before and after.
- A11b: `_TfLoader` class (the `_load_tf` extraction) — tf_cache/
  holdout_cache as instance attributes instead of closure-captured locals;
  one fresh instance per round. Same regression gate as A11a.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

import web.routes.agent_backtest as ab


class TestMarketFor:
    def test_bybit_is_always_none(self):
        assert ab._market_for(False, "AAPL", "60") is None
        assert ab._market_for(False, "", "D") is None

    def test_external_returns_exact_prompt_string(self):
        # Byte-for-byte: this string is fed to the LLM prompt directly.
        assert (
            ab._market_for(True, "AAPL", "60")
            == "US equity AAPL (60 bars, USD cash account)"
        )
        assert (
            ab._market_for(True, "MSFT", "D")
            == "US equity MSFT (D bars, USD cash account)"
        )


class TestIvFor:
    def test_regression_position_4_reachable_by_round_2(self):
        """2026-08-08 regression (run 3467219a): 5 timeframes, n_iterations=4
        — without the run_number offset, intervals[4] (index >= n_iterations)
        was never selected by any round of an unbounded continuous loop."""
        intervals = ["1", "5", "15", "60", "D"]
        n_iterations = 4
        reached_round1 = {ab._iv_for(i, 1, intervals) for i in range(n_iterations)}
        assert "D" not in reached_round1

        reached_by_round2 = set(reached_round1) | {
            ab._iv_for(i, 2, intervals) for i in range(n_iterations)
        }
        assert "D" in reached_by_round2

    def test_round_robin_wraps(self):
        intervals = ["1", "5", "15"]
        assert ab._iv_for(0, 1, intervals) == "1"
        assert ab._iv_for(1, 1, intervals) == "5"
        assert ab._iv_for(2, 1, intervals) == "15"
        assert ab._iv_for(3, 1, intervals) == "1"  # wraps


class TestRecipe:
    def test_bybit_shape(self):
        assert ab._recipe(False, "", "BTCUSDT", "linear", "60") == {
            "symbol": "BTCUSDT",
            "interval": "60",
            "category": "linear",
        }

    def test_external_shape(self):
        assert ab._recipe(True, "AAPL", "", "", "D") == {
            "source": "external",
            "instrument_id": "AAPL",
            "granularity": "D",
        }


class TestWorkerStateDefaults:
    def test_scalar_defaults(self):
        w = ab._WorkerState()
        assert w.last_err_str is None
        assert w.consec_err == 0
        assert w.winless_rounds == 0
        assert w.last_started_round == 0
        assert w.completed_rounds == 0
        assert w.worker_t0 > 0  # time.monotonic() at construction

    def test_set_defaults_are_empty_and_independent_per_instance(self):
        """Mutable-default footgun check: two instances must not share a set."""
        a, b = ab._WorkerState(), ab._WorkerState()
        a.degraded_spec_ids.add("x")
        assert b.degraded_spec_ids == set()
        for field_name in (
            "degraded_spec_ids",
            "holdout_consumed",
            "seen_candidate_fingerprints",
            "zero_trade_families",
            "retained_block_names",
        ):
            assert getattr(ab._WorkerState(), field_name) == set()


class _FakeSpec:
    def __init__(self, block_types):
        self.blocks = [SimpleNamespace(type=t) for t in block_types]


class TestCleanupGenerated:
    def test_keep_spec_types_land_in_retained_block_names_and_are_passed_through(
        self, monkeypatch
    ):
        calls = {}

        def fake_cleanup_agent_run(run_id, keep_names):
            calls["run_id"] = run_id
            calls["keep_names"] = set(keep_names)

        monkeypatch.setattr(
            "custom_block_store.cleanup_agent_run", fake_cleanup_agent_run
        )
        wstate = ab._WorkerState()
        ab._cleanup_generated("run-1", wstate, _FakeSpec(["momentum", "rsi"]))

        assert wstate.retained_block_names == {"momentum", "rsi"}
        assert calls == {"run_id": "run-1", "keep_names": {"momentum", "rsi"}}

    def test_exception_is_swallowed_not_propagated(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr("custom_block_store.cleanup_agent_run", boom)
        wstate = ab._WorkerState()

        with caplog.at_level("ERROR"):
            ab._cleanup_generated("run-1", wstate)  # must not raise

        assert "cleanup failed" in caplog.text


class TestWinlessBump:
    def test_returns_false_until_limit_then_true(self, monkeypatch):
        monkeypatch.setattr(ab, "_WINLESS_ROUND_LIMIT", 3)
        wstate = ab._WorkerState()
        assert ab._winless_bump(wstate) is False  # 1
        assert ab._winless_bump(wstate) is False  # 2
        assert ab._winless_bump(wstate) is True  # 3 == limit

    def test_same_counter_regardless_of_which_winnerless_branch_calls_it(
        self, monkeypatch
    ):
        """M22 regression: both winnerless branches (no-eligible, no-winner)
        must increment the SAME counter — verified here by calling it from
        two 'different' call sites and confirming the count is shared."""
        monkeypatch.setattr(ab, "_WINLESS_ROUND_LIMIT", 2)
        wstate = ab._WorkerState()
        ab._winless_bump(wstate)  # simulates "no eligible candidate" branch
        assert ab._winless_bump(wstate) is True  # simulates "no winner" branch


class TestWinlessStop:
    def test_marks_done_and_logs_session_end_with_round_counters(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        run_id = "run-winless"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = {
                "done": False,
                "continuous_finished": False,
                "steps": [],
            }
        try:
            wstate = ab._WorkerState()
            wstate.last_started_round = 5
            wstate.completed_rounds = 4
            ab._winless_stop(run_id, 6, wstate)

            assert ab._AGENT_PROGRESS[run_id]["done"] is True
            assert ab._AGENT_PROGRESS[run_id]["continuous_finished"] is True

            import json

            lines = (tmp_path / f"{run_id}.jsonl").read_text().strip().splitlines()
            events = [json.loads(ln) for ln in lines]
            end = [e for e in events if e["event"] == "session_end"]
            assert end and end[0]["outcome"] == "winless_limit"
            assert end[0]["started_round"] == 5
            assert end[0]["completed_rounds"] == 4
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(run_id, None)


class TestMakeLlmControl:
    """Currently zero-coverage safety-critical budget/cancel logic (2026-08-08
    DeepR #47-A4) — previously only reachable through a full worker run."""

    def _progress(self, **overrides):
        base = {"stop_requested": False, "max_total_tokens": 0}
        base.update(overrides)
        return base

    def test_stop_requested_cancels(self, monkeypatch):
        run_id = "run-stop"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = self._progress(stop_requested=True)
        try:
            cancelled, _ = ab._make_llm_control(run_id, 0.0, ab._WorkerState())
            assert cancelled() is True
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(run_id, None)

    def test_run_missing_from_progress_cancels(self):
        cancelled, _ = ab._make_llm_control("no-such-run", 0.0, ab._WorkerState())
        assert cancelled() is True

    def test_token_cap_raises_budget_reached(self, monkeypatch):
        run_id = "run-tokens"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = self._progress(max_total_tokens=100)
        monkeypatch.setattr(ab, "_token_usage", lambda state: 150)
        try:
            cancelled, _ = ab._make_llm_control(run_id, 0.0, ab._WorkerState())
            with pytest.raises(ab.AgentBudgetReached, match="token ceiling"):
                cancelled()
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(run_id, None)

    def test_wall_clock_cap_raises_budget_reached_after_token_check_passes(
        self, monkeypatch
    ):
        run_id = "run-clock"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = self._progress()  # no token cap
        wstate = ab._WorkerState()
        wstate.worker_t0 = 0.0
        monkeypatch.setattr(ab.time, "monotonic", lambda: 999999.0)
        try:
            cancelled, _ = ab._make_llm_control(run_id, max_hours=0.001, wstate=wstate)
            with pytest.raises(ab.AgentBudgetReached, match="time ceiling"):
                cancelled()
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(run_id, None)

    def test_no_condition_returns_false(self):
        run_id = "run-fine"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id] = self._progress()
        try:
            cancelled, _ = ab._make_llm_control(run_id, 0.0, ab._WorkerState())
            assert cancelled() is False
        finally:
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(run_id, None)

    def test_observer_records_usage_and_logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        run_id = "run-observe"
        recorded = {}
        monkeypatch.setattr(
            ab, "_add_tokens", lambda rid, usage: recorded.setdefault("usage", usage)
        )
        _, observer = ab._make_llm_control(run_id, 0.0, ab._WorkerState())
        observer({"usage": {"input_tokens": 10}, "model": "x"})

        assert recorded["usage"] == {"input_tokens": 10}
        import json

        lines = (tmp_path / f"{run_id}.jsonl").read_text().strip().splitlines()
        events = [json.loads(ln) for ln in lines]
        assert any(e["event"] == "llm_usage" and e["model"] == "x" for e in events)


class TestRankAndFilter:
    @staticmethod
    def _result(n_trades, *, pnl=100.0, sharpe=1.0, excess=0.05, error=None):
        return SimpleNamespace(
            error=error,
            metrics={
                "n_trades": n_trades,
                "pnl": pnl,
                "sharpe_per_trade": sharpe,
                "excess_return_fraction": excess,
                "sharpe": sharpe,
                "pnl_pct": 0.1,
                "win_rate": 0.5,
                "max_dd": -0.1,
            },
        )

    def _spec(self, name):
        return SimpleNamespace(name=name)

    def test_qualifying_and_non_qualifying_membership(self, monkeypatch):
        monkeypatch.setattr(ab, "_add_step", lambda *a, **k: None)
        good = (self._spec("good"), self._result(30), "60")
        junk_few_trades = (self._spec("junk1"), self._result(5), "60")
        junk_negative_pnl = (self._spec("junk2"), self._result(30, pnl=-10.0), "60")
        errored = (self._spec("junk3"), self._result(30, error="boom"), "60")
        results = [good, junk_few_trades, junk_negative_pnl, errored]

        ranked, eligible = ab._rank_and_filter("run-1", results)

        assert len(ranked) == 4  # ranking includes everything
        assert [s.name for s, _r, _iv in eligible] == ["good"]

    def test_logs_qualify_count_and_top5(self, monkeypatch):
        logged = []
        monkeypatch.setattr(ab, "_add_step", lambda run_id, msg: logged.append(msg))
        results = [(self._spec("a"), self._result(30), "60")]

        ab._rank_and_filter("run-1", results)

        assert any("1/1 results qualify" in m for m in logged)
        assert any("#1 a" in m for m in logged)


def _proposal(*, degraded="", degraded_terminal=False, blocks=None):
    return {
        "name": "Test Strategy",
        "blocks": blocks or [{"type": "momentum", "role": "entry", "params": {}}],
        "degraded": degraded,
        "degraded_terminal": degraded_terminal,
        "degraded_detail": "provider down" if degraded_terminal else "",
    }


class TestProposeInitialStrategy:
    def _call(self, monkeypatch, wstate, *, proposal=None):
        monkeypatch.setattr(ab, "_set_phase", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_tl_begin", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_tl_end", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_done_phase", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_add_step", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_session_log", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_enforce_token_budget", lambda run_id: None)
        monkeypatch.setattr(
            ab, "_mark_degraded", lambda run_id, reason, what: f"degraded:{reason}"
        )
        monkeypatch.setattr("composer.load_catalog", lambda: [])
        monkeypatch.setattr(
            "agent.propose_composed_strategy",
            lambda *a, **k: (proposal or _proposal(), {"input_tokens": 1}),
        )
        return ab._propose_initial_strategy(
            "run-1",
            1,
            "hint",
            False,
            False,
            "",
            ["60"],
            True,
            "60",
            wstate,
        )

    def test_degraded_non_terminal_returns_spec_and_records_id(self, monkeypatch):
        wstate = ab._WorkerState()
        spec = self._call(
            monkeypatch, wstate, proposal=_proposal(degraded="rate_limited")
        )
        assert spec is not None
        assert spec.id in wstate.degraded_spec_ids

    def test_degraded_terminal_raises_and_propagates(self, monkeypatch):
        from agent import TerminalLLMError

        wstate = ab._WorkerState()
        with pytest.raises(TerminalLLMError, match="provider down"):
            self._call(
                monkeypatch,
                wstate,
                proposal=_proposal(degraded="down", degraded_terminal=True),
            )

    def test_dedup_receives_wstate_sets_by_reference_not_copy(self, monkeypatch):
        wstate = ab._WorkerState()
        seen_arg = {}

        def fake_dedup(spec, *, seen, zero_trade_families, **kw):
            seen_arg["seen"] = seen
            seen_arg["zero_trade_families"] = zero_trade_families
            return spec

        monkeypatch.setattr(ab, "_deduplicate_candidate", fake_dedup)
        self._call(monkeypatch, wstate)

        assert seen_arg["seen"] is wstate.seen_candidate_fingerprints
        assert seen_arg["zero_trade_families"] is wstate.zero_trade_families

    def test_returned_spec_is_always_stamped(self, monkeypatch):
        wstate = ab._WorkerState()
        spec = self._call(monkeypatch, wstate)
        assert spec.trend_filter is True
        assert spec.trend_interval == "60"
        assert spec.model_slippage is True


class TestScanOneCandidate:
    def _spec(self, name="cand", id_="id-1"):
        return SimpleNamespace(name=name, id=id_)

    def _result(self, pnl=100.0, sharpe=1.0):
        return SimpleNamespace(
            error=None,
            metrics={
                "n_trades": 30,
                "pnl": pnl,
                "pnl_pct": pnl / 10_000.0,
                "sharpe_per_trade": sharpe,
                "max_dd": -0.1,
            },
            trades=[],
        )

    def _quiet(self, monkeypatch):
        for name in (
            "_set_robustness_scan",
            "_add_step",
            "_set_phase",
            "_tl_begin",
            "_tl_end",
            "_session_log",
        ):
            monkeypatch.setattr(ab, name, lambda *a, **k: None)
        monkeypatch.setattr(
            ab, "_make_rob_progress", lambda *a, **k: _FakeRobProgress()
        )
        monkeypatch.setattr(
            ab, "_write_session_artifact", lambda *a, **k: {"digest": "x"}
        )
        monkeypatch.setattr(ab, "_index_insert", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_robustness_log_summary", lambda rob: {})

    def _args(self, wstate, *, n_passers_so_far=0):
        return dict(
            run_id="run-1",
            run_number=1,
            rank_i=0,
            n_eligible=1,
            cand_spec=self._spec(),
            cand_result=self._result(),
            cand_iv="60",
            cand_df=None,
            strict_mode=False,
            wstate=wstate,
            is_external=False,
            instrument_id="",
            symbol="BTCUSDT",
            category="linear",
            max_hours=0.0,
            n_passers_so_far=n_passers_so_far,
        )

    def test_exception_is_caught_and_returns_failed_non_raising_outcome(
        self, monkeypatch
    ):
        self._quiet(monkeypatch)
        monkeypatch.setattr(
            "sandbox.run_robustness_guarded",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        outcome = ab._scan_one_candidate(**self._args(ab._WorkerState()))

        assert outcome.passed is False
        assert outcome.passer_entry is None
        assert outcome.log_entry["overfitting_label"] == "error: RuntimeError"

    def test_passing_candidate_positive_score_sign_safety(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr("sandbox.run_robustness_guarded", lambda *a, **k: {})
        monkeypatch.setattr(ab, "_robustness_passed", lambda *a, **k: True)
        monkeypatch.setattr(ab, "_ms_score_factor", lambda rob: 0.5)

        outcome = ab._scan_one_candidate(
            **self._args(ab._WorkerState(), n_passers_so_far=1)
        )

        assert outcome.passed is True
        raw = ab._score(self._result())
        expected = raw - (1.0 - 0.5) * abs(raw)
        assert outcome.passer_entry["effective"] == pytest.approx(expected)
        assert outcome.passer_entry["effective"] == pytest.approx(raw * 0.5)

    def test_passing_candidate_negative_score_penalty_pulls_down_not_up(
        self, monkeypatch
    ):
        """Sign-safety: for a NEGATIVE raw score, the MS-factor penalty must
        make the effective score MORE negative (worse), never less negative
        — a naive raw*factor would invert this for factor<1."""
        self._quiet(monkeypatch)
        monkeypatch.setattr("sandbox.run_robustness_guarded", lambda *a, **k: {})
        monkeypatch.setattr(ab, "_robustness_passed", lambda *a, **k: True)
        monkeypatch.setattr(ab, "_ms_score_factor", lambda rob: 0.5)

        args = self._args(ab._WorkerState())
        args["cand_result"] = self._result(pnl=-100.0, sharpe=-1.0)
        outcome = ab._scan_one_candidate(**args)

        raw = ab._score(self._result(pnl=-100.0, sharpe=-1.0))
        assert raw < 0
        assert outcome.passer_entry["effective"] < raw  # worse, not better

    def test_degraded_spec_forced_to_fail_regardless_of_robustness_verdict(
        self, monkeypatch
    ):
        self._quiet(monkeypatch)
        monkeypatch.setattr("sandbox.run_robustness_guarded", lambda *a, **k: {})
        monkeypatch.setattr(ab, "_robustness_passed", lambda *a, **k: True)
        monkeypatch.setattr(ab, "_ms_score_factor", lambda rob: 1.0)

        wstate = ab._WorkerState()
        args = self._args(wstate)
        args["cand_spec"] = self._spec(id_="degraded-1")
        wstate.degraded_spec_ids.add("degraded-1")

        outcome = ab._scan_one_candidate(**args)

        assert outcome.passed is False
        assert outcome.passer_entry is None


def _hold_frame():
    idx = pd.date_range("2026-01-01", periods=5, freq="1D", tz="UTC")
    return pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0, 104.0]}, index=idx)


class TestRunPromotionGate:
    def _spec(self):
        return SimpleNamespace(id="spec-1", name="Winner")

    def _quiet(self, monkeypatch):
        for name in ("_add_step", "_session_log", "_log_robustness"):
            monkeypatch.setattr(ab, name, lambda *a, **k: None)

    def _args(self, wstate, holdout_cache, *, research_only=False):
        return dict(
            run_id="run-1",
            run_number=1,
            winner_spec=self._spec(),
            winner_iv="60",
            winner_rob={},
            holdout_cache=holdout_cache,
            wstate=wstate,
            is_external=False,
            instrument_id="",
            symbol="BTCUSDT",
            category="linear",
            max_hours=0.0,
            research_only=research_only,
        )

    def test_already_consumed_timeframe_skips_holdout_run_entirely(self, monkeypatch):
        self._quiet(monkeypatch)
        calls = {"n": 0}
        monkeypatch.setattr(
            "sandbox.run_backtest_guarded",
            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
        )
        wstate = ab._WorkerState()
        wstate.holdout_consumed.add("60")
        hold_start = pd.Timestamp("2026-01-01", tz="UTC")
        holdout_cache = {"60": (_hold_frame(), hold_start)}

        gate = ab._run_promotion_gate(**self._args(wstate, holdout_cache))

        assert gate.promotion_passed is False
        assert gate.promotion_reason == (
            "sealed holdout already consumed for this timeframe"
        )
        assert calls["n"] == 0

    def test_passing_holdout_promotes(self, monkeypatch):
        self._quiet(monkeypatch)
        hold_start = pd.Timestamp("2026-01-01", tz="UTC")
        holdout_cache = {"60": (_hold_frame(), hold_start)}
        hold_res = SimpleNamespace(
            error=None, metrics={"pnl": 500.0}, trades=[{"pnl": 500.0}]
        )
        monkeypatch.setattr("sandbox.run_backtest_guarded", lambda *a, **k: hold_res)
        monkeypatch.setattr(
            ab, "_sealed_holdout_stats", lambda trades, start: (25, 0.05, 1.2)
        )
        monkeypatch.setattr(
            ab, "_holdout_promotion_verdict", lambda *a, **k: (True, "passed")
        )

        wstate = ab._WorkerState()
        gate = ab._run_promotion_gate(**self._args(wstate, holdout_cache))

        assert gate.promotion_passed is True
        assert gate.winner_holdout["n_trades"] == 25
        assert gate.winner_holdout["pnl_pct"] == 0.05
        assert gate.winner_holdout["measured"] is True
        assert "60" in wstate.holdout_consumed

    def test_research_only_forces_rejection_even_when_holdout_passes(self, monkeypatch):
        self._quiet(monkeypatch)
        hold_start = pd.Timestamp("2026-01-01", tz="UTC")
        holdout_cache = {"60": (_hold_frame(), hold_start)}
        hold_res = SimpleNamespace(
            error=None, metrics={"pnl": 500.0}, trades=[{"pnl": 500.0}]
        )
        monkeypatch.setattr("sandbox.run_backtest_guarded", lambda *a, **k: hold_res)
        monkeypatch.setattr(
            ab, "_sealed_holdout_stats", lambda trades, start: (25, 0.05, 1.2)
        )
        monkeypatch.setattr(
            ab, "_holdout_promotion_verdict", lambda *a, **k: (True, "passed")
        )

        wstate = ab._WorkerState()
        gate = ab._run_promotion_gate(
            **self._args(wstate, holdout_cache, research_only=True)
        )

        assert gate.promotion_passed is False
        assert gate.promotion_reason == (
            "research-only run used known-unadjusted external data"
        )

    def test_holdout_run_exception_is_caught_not_raised(self, monkeypatch):
        self._quiet(monkeypatch)
        hold_start = pd.Timestamp("2026-01-01", tz="UTC")
        holdout_cache = {"60": (_hold_frame(), hold_start)}
        monkeypatch.setattr(
            "sandbox.run_backtest_guarded",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sandbox died")),
        )

        wstate = ab._WorkerState()
        gate = ab._run_promotion_gate(**self._args(wstate, holdout_cache))

        assert gate.promotion_passed is False
        assert "sealed holdout exception:" in gate.promotion_reason


def _bars(n=5):
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10.0] * n,
        },
        index=idx,
    )


class TestRunBacktestIteration:
    def _spec(self):
        return SimpleNamespace(
            name="Strat",
            id="spec-1",
            blocks=[SimpleNamespace(type="momentum", role="entry", params={})],
        )

    def _quiet(self, monkeypatch):
        for name in ("_add_step", "_tl_begin", "_tl_end", "_set_phase", "_session_log"):
            monkeypatch.setattr(ab, name, lambda *a, **k: None)
        monkeypatch.setattr(ab, "_stamp_buy_hold_benchmark", lambda *a, **k: None)

    def _result(self, *, n_trades=0, error=None):
        return SimpleNamespace(
            error=error,
            metrics={"n_trades": n_trades, "pnl": 0.0},
            trades=[],
            equity_curve=[],
            equity_dates=[],
        )

    def _args(self, wstate, run_result):
        monkeypatch_target = "sandbox.run_backtest_guarded"
        return (
            monkeypatch_target,
            dict(
                run_id="run-1",
                run_number=1,
                i=0,
                n_iterations=3,
                spec=self._spec(),
                iter_iv="60",
                iter_df=_bars(),
                is_external=False,
                symbol="BTCUSDT",
                instrument_id="",
                category="linear",
                wstate=wstate,
                max_hours=0.0,
            ),
            run_result,
        )

    def test_happy_path_stamps_result_and_updates_wstate(self, monkeypatch):
        self._quiet(monkeypatch)
        wstate = ab._WorkerState()
        target, args, run_result = self._args(wstate, self._result(n_trades=0))
        monkeypatch.setattr(target, lambda *a, **k: run_result)
        monkeypatch.setattr(ab, "_log_backtest", lambda *a, **k: "ts-1")

        r = ab._run_backtest_iteration(**args)

        assert r.strategy == "composed:Strat [momentum]"
        assert r.bars_info["symbol"] == "BTCUSDT"
        assert (
            ab._candidate_fingerprint(args["spec"])
            in wstate.seen_candidate_fingerprints
        )
        assert (
            ab._candidate_fingerprint(args["spec"], family=True)
            in wstate.zero_trade_families
        )

    def test_non_zero_trades_does_not_mark_zero_trade_family(self, monkeypatch):
        self._quiet(monkeypatch)
        wstate = ab._WorkerState()
        target, args, run_result = self._args(wstate, self._result(n_trades=5))
        monkeypatch.setattr(target, lambda *a, **k: run_result)
        monkeypatch.setattr(ab, "_log_backtest", lambda *a, **k: "ts-1")

        ab._run_backtest_iteration(**args)

        assert (
            ab._candidate_fingerprint(args["spec"], family=True)
            not in wstate.zero_trade_families
        )

    def test_error_result_does_not_raise(self, monkeypatch):
        self._quiet(monkeypatch)
        wstate = ab._WorkerState()
        target, args, run_result = self._args(
            wstate, self._result(error="backtest exploded")
        )
        monkeypatch.setattr(target, lambda *a, **k: run_result)
        monkeypatch.setattr(ab, "_log_backtest", lambda *a, **k: "ts-1")

        r = ab._run_backtest_iteration(**args)  # must not raise

        assert r.error == "backtest exploded"

    def test_log_backtest_write_failure_is_swallowed(self, monkeypatch):
        self._quiet(monkeypatch)
        wstate = ab._WorkerState()
        target, args, run_result = self._args(wstate, self._result(n_trades=0))
        monkeypatch.setattr(target, lambda *a, **k: run_result)
        monkeypatch.setattr(
            ab,
            "_log_backtest",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        r = ab._run_backtest_iteration(**args)  # must not raise

        assert r is run_result


def _daily_bars(n=800, start="2024-01-01"):
    idx = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10.0] * n,
        },
        index=idx,
    )


class TestLoadTimeframeBars:
    def _args(self, **overrides):
        base = dict(
            run_id="run-1",
            is_external=False,
            instrument_id="",
            symbol="BTCUSDT",
            category="linear",
            iv="60",
            range_start="",
            range_end="",
        )
        base.update(overrides)
        return base

    def _quiet(self, monkeypatch):
        monkeypatch.setattr(ab, "_add_step", lambda *a, **k: None)

    def test_bybit_lookback_table_sizes_the_default_window(self, monkeypatch):
        self._quiet(monkeypatch)
        captured = {}

        def fake_load(**kw):
            captured.update(kw)
            return _daily_bars(200)

        monkeypatch.setattr("data.load_bybit_bars", fake_load)
        ab._load_timeframe_bars(**self._args(iv="5"))

        expected_days = 1460  # lookback table: "5" -> 1460
        delta = captured["end"] - captured["start"]
        assert abs(delta.days - expected_days) <= 1

    def test_bybit_explicit_range_overrides_lookback(self, monkeypatch):
        self._quiet(monkeypatch)
        captured = {}

        def fake_load(**kw):
            captured.update(kw)
            return _daily_bars(200)

        monkeypatch.setattr("data.load_bybit_bars", fake_load)
        ab._load_timeframe_bars(
            **self._args(range_start="2024-01-01", range_end="2024-01-10")
        )

        assert captured["start"] == datetime(2024, 1, 1, tzinfo=UTC)
        assert captured["end"] == datetime(2024, 1, 11, tzinfo=UTC)  # +1 day, inclusive

    def test_bybit_fetch_error_falls_back_to_cache_when_it_exists(self, monkeypatch):
        self._quiet(monkeypatch)

        def boom(**kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("data.load_bybit_bars", boom)
        fake_path = SimpleNamespace(exists=lambda: True)
        monkeypatch.setattr("data._bybit_cache_path", lambda *a, **k: fake_path)
        monkeypatch.setattr(pd, "read_parquet", lambda p: _daily_bars(200))

        df = ab._load_timeframe_bars(**self._args())
        assert len(df) == 200

    def test_bybit_fetch_error_no_cache_raises(self, monkeypatch):
        self._quiet(monkeypatch)

        def boom(**kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("data.load_bybit_bars", boom)
        fake_path = SimpleNamespace(exists=lambda: False)
        monkeypatch.setattr("data._bybit_cache_path", lambda *a, **k: fake_path)

        with pytest.raises(RuntimeError, match="Could not load"):
            ab._load_timeframe_bars(**self._args())

    def test_bybit_too_few_bars_raises(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr("data.load_bybit_bars", lambda **kw: _daily_bars(50))

        with pytest.raises(RuntimeError, match="Insufficient data"):
            ab._load_timeframe_bars(**self._args())

    def test_bybit_1m_guard_crops_to_last_730_days(self, monkeypatch):
        self._quiet(monkeypatch)
        big = _daily_bars(1_000_001, start="2000-01-01")
        monkeypatch.setattr("data.load_bybit_bars", lambda **kw: big)

        df = ab._load_timeframe_bars(**self._args(iv="1"))

        span_days = (df.index[-1] - df.index[0]).days
        assert span_days <= 731

    def test_external_date_window_slices_inclusive(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr("data.load_external_bars", lambda *a, **k: _daily_bars(800))

        df = ab._load_timeframe_bars(
            **self._args(
                is_external=True,
                instrument_id="AAPL.NASDAQ",
                range_start="2024-01-01",
                range_end="2024-06-01",  # 153 days, inclusive — clears the <100 guard
            )
        )

        assert len(df) == 153
        assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
        assert df.index[-1] == pd.Timestamp("2024-06-01", tz="UTC")

    def test_external_too_few_bars_raises(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr("data.load_external_bars", lambda *a, **k: _daily_bars(50))

        with pytest.raises(RuntimeError, match="Insufficient data"):
            ab._load_timeframe_bars(
                **self._args(is_external=True, instrument_id="AAPL.NASDAQ")
            )

    def test_external_1_minute_guard_crops_without_explicit_range(self, monkeypatch):
        self._quiet(monkeypatch)
        big = _daily_bars(1_000_001, start="2000-01-01")
        monkeypatch.setattr("data.load_external_bars", lambda *a, **k: big)

        df = ab._load_timeframe_bars(
            **self._args(is_external=True, instrument_id="AAPL.NASDAQ", iv="1-MINUTE")
        )

        span_days = (df.index[-1] - df.index[0]).days
        assert span_days <= 731

    def test_external_1_minute_guard_skipped_with_explicit_range(self, monkeypatch):
        self._quiet(monkeypatch)
        big = _daily_bars(1_000_001, start="2000-01-01")
        monkeypatch.setattr("data.load_external_bars", lambda *a, **k: big)

        df = ab._load_timeframe_bars(
            **self._args(
                is_external=True,
                instrument_id="AAPL.NASDAQ",
                iv="1-MINUTE",
                range_start="2000-01-01",
                range_end="2002-12-31",
            )
        )

        # explicit range honored as-is, not cropped to 730d despite >1M bars
        span_days = (df.index[-1] - df.index[0]).days
        assert span_days > 731


class TestTfLoader:
    def _quiet(self, monkeypatch):
        for name in ("_add_step", "_tl_begin", "_tl_end"):
            monkeypatch.setattr(ab, name, lambda *a, **k: None)

    def _loader(self):
        return ab._TfLoader("run-1", 1, False, "", "BTCUSDT", "linear", "", "")

    def test_cache_hit_does_not_reinvoke_the_loader(self, monkeypatch):
        self._quiet(monkeypatch)
        calls = {"n": 0}

        def fake_load(*a, **k):
            calls["n"] += 1
            return _daily_bars(400)

        monkeypatch.setattr(ab, "_load_timeframe_bars", fake_load)
        monkeypatch.setattr(ab, "_split_holdout", lambda df: (df, None))

        loader = self._loader()
        loader.get("60")
        loader.get("60")

        assert calls["n"] == 1

    def test_holdout_split_populated_with_correct_lead_in(self, monkeypatch):
        self._quiet(monkeypatch)
        full_df = _daily_bars(400)
        hold_df = full_df.iloc[-50:]
        trimmed_df = full_df.iloc[:300]  # arbitrary distinct object

        monkeypatch.setattr(ab, "_load_timeframe_bars", lambda *a, **k: full_df)
        monkeypatch.setattr(ab, "_split_holdout", lambda df: (trimmed_df, hold_df))

        loader = self._loader()
        result = loader.get("60")

        assert result is trimmed_df
        assert loader.tf_cache["60"] is trimmed_df
        sealed_start = loader.holdout_cache["60"][1]
        assert sealed_start == hold_df.index[0]
        run_frame = loader.holdout_cache["60"][0]
        # lead-in is exactly HOLDOUT_WARMUP_BARS rows ending right before
        # the sealed boundary, plus the sealed slice itself
        assert len(run_frame) == ab.HOLDOUT_WARMUP_BARS + len(hold_df)
        assert run_frame.index[ab.HOLDOUT_WARMUP_BARS] == hold_df.index[0]

    def test_holdout_split_none_when_insufficient_data(self, monkeypatch):
        self._quiet(monkeypatch)
        full_df = _daily_bars(400)
        monkeypatch.setattr(ab, "_load_timeframe_bars", lambda *a, **k: full_df)
        monkeypatch.setattr(ab, "_split_holdout", lambda df: (df, None))

        loader = self._loader()
        result = loader.get("60")

        assert result is full_df
        assert loader.tf_cache["60"] is full_df
        assert loader.holdout_cache["60"] is None

    def test_exception_ends_the_timeline_span_and_propagates(self, monkeypatch):
        monkeypatch.setattr(ab, "_add_step", lambda *a, **k: None)
        monkeypatch.setattr(ab, "_tl_begin", lambda *a, **k: None)
        ended = {}
        monkeypatch.setattr(
            ab, "_tl_end", lambda run_id, key, **k: ended.update(status=k.get("status"))
        )
        monkeypatch.setattr(
            ab,
            "_load_timeframe_bars",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no data")),
        )

        loader = self._loader()
        with pytest.raises(RuntimeError, match="no data"):
            loader.get("60")

        assert ended["status"] == "fail"


class _FakeRobProgress:
    def __call__(self, *a, **k):
        pass

    def close_open(self, status):
        pass
