"""_agent_worker decomposition (2026-08-08 DeepR #47), steps A1-A2:
- A1: the three pure closures `_market_for`/`_iv_for`/`_recipe` were promoted
  to module-level functions in web/routes/agent_backtest.py so they can be
  unit-tested in isolation, instead of only indirectly through a full worker
  run.
- A2: the ~10 round-persistent locals were collected into a `_WorkerState`
  dataclass (mechanical rename only, no logic change — verified by the full
  existing suite passing unchanged).
"""

from __future__ import annotations

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
