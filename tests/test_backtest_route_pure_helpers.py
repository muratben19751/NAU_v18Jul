"""Pure helpers in web/routes/backtest.py with zero prior unit-test coverage
(DeepR 2026-08-08 #59-B1): `_is_equity_target` and `_local_fallback_breakdown`
are already plain, single-call-site module functions (called only from
`plan_preview()`) — the gap is test coverage, not architecture. No
production code changes in this pass; see #59's decomposition plan
(deferred: `run()`'s 3 instrument-kind branches, `describe()`'s worker body,
unifying `_preview_signals` with composer.py's `_eval_*` evaluators).
"""

from __future__ import annotations

import web.routes.backtest as bt


class TestIsEquityTarget:
    def test_all_empty_bybit_is_not_equity(self):
        assert bt._is_equity_target("bybit", "linear", "", "") is False

    def test_empty_instrument_kind_no_ticker_is_not_equity(self):
        assert bt._is_equity_target("", "", "", "") is False

    def test_ticker_set_is_equity_regardless_of_kind(self):
        assert bt._is_equity_target("bybit", "linear", "AAPL", "") is True

    def test_ext_instrument_set_is_equity_regardless_of_kind(self):
        assert bt._is_equity_target("bybit", "linear", "", "AAPL.NASDAQ") is True

    def test_index_kind_is_equity(self):
        assert bt._is_equity_target("index", "", "", "") is True

    def test_external_kind_is_equity(self):
        assert bt._is_equity_target("external", "", "", "") is True

    def test_us_index_kind_is_equity(self):
        assert bt._is_equity_target("us-index", "", "", "") is True

    def test_kind_match_is_case_insensitive(self):
        assert bt._is_equity_target("INDEX", "", "", "") is True
        assert bt._is_equity_target("External", "", "", "") is True
        assert bt._is_equity_target("US-INDEX", "", "", "") is True

    def test_none_kind_does_not_raise(self):
        assert bt._is_equity_target(None, "", "", "") is False


class TestLocalFallbackBreakdown:
    def test_no_indicators_uses_truncated_description_as_label(self):
        long_desc = "x" * 100
        bd = bt._local_fallback_breakdown(long_desc, [])
        assert bd["label"] == long_desc[:40]

    def test_no_indicators_and_blank_description_uses_placeholder(self):
        bd = bt._local_fallback_breakdown("   ", [])
        assert bd["label"] == "Described strategy"

    def test_first_indicator_used_as_label_when_present(self):
        bd = bt._local_fallback_breakdown("some description", ["rsi_threshold"])
        assert bd["label"] == "rsi_threshold-based strategy"

    def test_fixed_shape(self):
        bd = bt._local_fallback_breakdown("desc", ["ma_cross"])
        assert bd["entry_logic"] == "OR"
        assert bd["exit_logic"] == "OR"
        assert bd["usage"] == {}
        assert len(bd["conditions"]) == 2
        roles = [c["role"] for c in bd["conditions"]]
        assert roles == ["entry", "exit"]
        assert bd["conditions"][0]["desc"] == "desc"
