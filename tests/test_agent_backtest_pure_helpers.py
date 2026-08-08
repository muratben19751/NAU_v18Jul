"""Pure/near-pure helpers in web/routes/agent_backtest.py with zero prior
test coverage (DeepR 2026-08-08 [BİLGİ]): `_pick_best_exit_from_history`
(deterministic, no I/O) and `_winner_narrative`'s LLM-failure fallback text
(the deterministic "gained"/"lost" branch — the LLM-success branch is a thin
passthrough, not the risky part). The big file (4767 lines) is weighted
towards route/integration tests; these are cheap, valuable unit-test
candidates precisely because they're pure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import web.routes.agent_backtest as ab
from state import IterationResult


def _iter(strategy, *, n_trades=30, max_dd=-0.05, sharpe=1.5, pnl_pct=0.10, error=None):
    return IterationResult(
        id=1,
        strategy=strategy,
        params={},
        metrics=(
            {}
            if error
            else {
                "n_trades": n_trades,
                "max_dd": max_dd,
                "sharpe_per_trade": sharpe,
                "pnl_pct": pnl_pct,
            }
        ),
        equity_curve=[],
        rationale="",
        error=error,
        timestamp=datetime.now(UTC),
    )


class TestPickBestExitFromHistory:
    def test_empty_history_returns_none(self):
        assert ab._pick_best_exit_from_history([]) is None

    def test_no_positive_score_returns_none(self):
        """All scores negative/error → nothing to recommend."""
        history = [
            _iter("composed:momentum", sharpe=-5, pnl_pct=-0.5),
            _iter("composed:rsi", error="crash"),
        ]
        assert ab._pick_best_exit_from_history(history) is None

    def test_matches_exit_type_named_in_the_winning_strategy(self):
        history = [
            _iter("composed:momentum", pnl_pct=0.20, sharpe=2.0),  # best
            _iter("composed:rsi_threshold", pnl_pct=0.05, sharpe=0.5),
        ]
        assert ab._pick_best_exit_from_history(history) == "momentum"

    def test_a_name_matching_two_exit_types_prefers_priority_order(self):
        """The best-scoring row is checked against _EXIT_PRIORITY in order —
        if its own name happens to contain both "momentum" and
        "macd_cross", momentum wins because it is earlier in the list."""
        history = [_iter("composed:momentum_macd_cross", pnl_pct=0.30, sharpe=3.0)]
        assert ab._pick_best_exit_from_history(history) == "momentum"

    def test_lower_scoring_row_is_only_consulted_if_the_top_row_has_no_match(self):
        """The scan is per-ROW in score order, not a global best-priority
        search across the top 3 — the #1 row's own name is checked FIRST,
        and a match there wins even over a "higher priority" type that only
        appears in a lower-scoring row."""
        history = [
            _iter(
                "composed:rsi_threshold+extra", pnl_pct=0.30, sharpe=3.0
            ),  # #1, matches
            _iter(
                "composed:macd_cross+extra", pnl_pct=0.25, sharpe=2.5
            ),  # #2, higher priority
        ]
        assert ab._pick_best_exit_from_history(history) == "rsi_threshold"

    def test_no_recognizable_exit_name_falls_back_to_first_priority(self):
        history = [_iter("composed:unrecognized_indicator_xyz", pnl_pct=0.10)]
        assert (
            ab._pick_best_exit_from_history(history) == "momentum"
        )  # _EXIT_PRIORITY[0]

    def test_only_the_top_3_scores_are_considered(self):
        """A name match outside the top 3 must not be picked over the
        top-3-only fallback — proves the [:3] slice is load-bearing."""
        top3 = [
            _iter(f"composed:unrecognized_{i}", pnl_pct=0.50 - i * 0.01)
            for i in range(3)
        ]
        fourth_place_macd = _iter("composed:macd_cross", pnl_pct=0.01, sharpe=0.1)
        history = [*top3, fourth_place_macd]
        assert ab._pick_best_exit_from_history(history) == "momentum"


class TestWinnerNarrativeFallback:
    def test_llm_failure_falls_back_to_deterministic_gained_text(self, monkeypatch):
        monkeypatch.setattr("agent._get_client", lambda: object())
        monkeypatch.setattr(
            "agent._create_message",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        last_row = {"pnl": 150.0, "n_trades": 12, "pnl_fmt": "+$150.00"}
        state = {"winner_spec_name": "Momentum RSI v3"}

        text = ab._winner_narrative(last_row, state)

        assert text == (
            "This strategy was saved to the catalog as Momentum RSI v3. "
            "With 12 trades it gained +$150.00 and passed the robustness test."
        )

    def test_llm_failure_fallback_says_lost_for_negative_pnl(self, monkeypatch):
        monkeypatch.setattr("agent._get_client", lambda: object())
        monkeypatch.setattr(
            "agent._create_message",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        last_row = {"pnl": -42.0, "n_trades": 8, "pnl_fmt": "-$42.00"}
        state = {"winner_spec_name": "RSI Fade"}

        text = ab._winner_narrative(last_row, state)

        assert "lost -$42.00" in text
        assert "gained" not in text

    def test_llm_success_returns_its_text_verbatim(self, monkeypatch):
        monkeypatch.setattr("agent._get_client", lambda: object())
        monkeypatch.setattr(
            "agent._create_message",
            lambda *a, **k: SimpleNamespace(
                content=[SimpleNamespace(text="  This strategy is a momentum play.  ")]
            ),
        )
        last_row = {"pnl": 10.0, "n_trades": 5, "pnl_fmt": "+$10.00"}
        state = {"winner_spec_name": "X"}

        text = ab._winner_narrative(last_row, state)

        assert text == "This strategy is a momentum play."
