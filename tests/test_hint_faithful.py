"""Hint-faithful exploration: when the hint contains explicit indicators the agent should stay on that set.

When the user provides 'RSI+ADX+ATR', the idea-prompt should use a 'stay on this
set, scan combination/parameter variations' directive instead of 'pick a different
indicator family' — otherwise the agent drifted to other indicators.
"""

from __future__ import annotations

from agent import _exploration_directive, _hint_indicators


class TestHintIndicators:
    def test_detects_named_indicators(self):
        inds = _hint_indicators("RSI VE ADX VE ATR ye gore")
        assert "RSI" in inds and "ADX/DMI" in inds and "ATR" in inds

    def test_english_and_turkish(self):
        assert "Stochastic" in _hint_indicators("use stochastic oscillator")
        assert "Volume" in _hint_indicators("hacim patlaması stratejisi")
        assert "MACD" in _hint_indicators("MACD histogram cross")

    def test_empty_hint_no_indicators(self):
        assert _hint_indicators("") == []
        assert _hint_indicators("kârlı bir şey bul") == []

    def test_no_false_positive_substring(self):
        # 'ma' must not falsely match in words that do not contain RSI/ADX
        assert _hint_indicators("maksimum kar minimum drawdown") == []
        # the 'ma' inside 'smart' must not be mistaken for SMA
        assert "SMA/MA" not in _hint_indicators("smart momentum idea")


class TestExplorationDirective:
    def test_faithful_when_indicators_present(self):
        d = _exploration_directive("RSI + ADX + ATR")
        assert "CORE" in d  # requested set is the strategy's core
        assert "RSI" in d and "ADX/DMI" in d and "ATR" in d
        # directive to add a creative complementary indicator
        assert "CREATIVE" in d and "COMPLEMENTARY" in d
        # there should be NO directive to REPLACE the set with a DIFFERENT family
        assert "pick a DIFFERENT indicator family" not in d

    def test_diverse_when_no_indicators(self):
        d = _exploration_directive("")
        assert "pick a DIFFERENT indicator family" in d
        assert "BU SETTE KAL" not in d

    def test_directive_fills_prompt_without_keyerror(self):
        """The _AGENT_IDEA_PROMPT.format call must not break with the new placeholder."""
        from agent import _AGENT_IDEA_PROMPT

        out = _AGENT_IDEA_PROMPT.format(
            market_tr="kripto trading",
            market_note="",
            exploration_directive=_exploration_directive("RSI ADX ATR"),
            history="yok",
            used_concepts="yok",
            hint="RSI ADX ATR",
        )
        assert "CORE" in out and "FORBIDDEN" in out
