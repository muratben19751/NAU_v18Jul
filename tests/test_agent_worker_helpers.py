"""_agent_worker decomposition (2026-08-08 DeepR #47), steps A1-A3:
- A1: the three pure closures `_market_for`/`_iv_for`/`_recipe` were promoted
  to module-level functions in web/routes/agent_backtest.py so they can be
  unit-tested in isolation, instead of only indirectly through a full worker
  run.
- A2: the ~10 round-persistent locals were collected into a `_WorkerState`
  dataclass (mechanical rename only, no logic change — verified by the full
  existing suite passing unchanged).
- A3: `_cleanup_generated`/`_winless_bump`/`_winless_stop` promoted to module
  functions taking `wstate` explicitly instead of closing over it.
"""

from __future__ import annotations

from types import SimpleNamespace

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
