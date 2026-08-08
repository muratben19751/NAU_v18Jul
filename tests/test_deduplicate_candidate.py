"""AUTO's candidate deduplication (`_deduplicate_candidate`) — DeepR
2026-08-08 [DÜŞÜK]: `_candidate_fingerprint` is well covered, but the
function that actually USES those fingerprints to decide replace-or-keep,
and its 64-attempt exhaustion path (`RuntimeError`, unhandled anywhere —
would crash AUTO in production), had no test at all.

`agent._fallback_composed` is monkeypatched at its source module: the
function under test does a LOCAL `from agent import _fallback_composed`
inside its body, which re-resolves from `agent`'s current namespace on
every call.
"""

from __future__ import annotations

import pytest

import web.routes.agent_backtest as ab


@pytest.fixture(autouse=True)
def _isolate_session_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path / "sessions")


def _spec(lookback=5):
    return ab._proposal_to_spec(
        {
            "blocks": [
                {"type": "momentum", "role": "entry", "params": {"lookback": lookback}}
            ]
        }
    )


def _fallback_proposal(lookback=99):
    return {
        "blocks": [
            {"type": "momentum", "role": "entry", "params": {"lookback": lookback}}
        ]
    }


class TestNoDuplicate:
    def test_unseen_candidate_is_returned_unchanged(self):
        spec = _spec()
        result = ab._deduplicate_candidate(
            spec,
            seen=set(),
            zero_trade_families=set(),
            run_id="run-1",
            round_num=1,
            iteration=1,
        )
        assert result is spec


class TestExactDuplicate:
    def test_exact_duplicate_is_replaced(self, monkeypatch):
        spec = _spec()
        exact = ab._candidate_fingerprint(spec)
        monkeypatch.setattr(
            "agent._fallback_composed", lambda: _fallback_proposal(lookback=99)
        )

        result = ab._deduplicate_candidate(
            spec,
            seen={exact},
            zero_trade_families=set(),
            run_id="run-2",
            round_num=1,
            iteration=1,
        )

        assert result is not spec
        assert ab._candidate_fingerprint(result) != exact

    def test_replacement_avoids_a_second_seen_fingerprint(self, monkeypatch):
        """The replacement must itself be checked against `seen` — the retry
        loop must not settle for a replacement that is ALSO a duplicate."""
        spec = _spec()
        exact = ab._candidate_fingerprint(spec)
        also_seen = _spec(lookback=99)
        also_seen_fp = ab._candidate_fingerprint(also_seen)

        calls = {"n": 0}

        def _fake_fallback():
            calls["n"] += 1
            # First attempt collides with `seen` too; second is clean.
            return (
                _fallback_proposal(lookback=99)
                if calls["n"] == 1
                else _fallback_proposal(lookback=7)
            )

        monkeypatch.setattr("agent._fallback_composed", _fake_fallback)

        result = ab._deduplicate_candidate(
            spec,
            seen={exact, also_seen_fp},
            zero_trade_families=set(),
            run_id="run-3",
            round_num=1,
            iteration=1,
        )

        assert calls["n"] == 2
        assert ab._candidate_fingerprint(result) not in {exact, also_seen_fp}


class TestZeroTradeFamily:
    def test_known_zero_trade_family_is_replaced(self, monkeypatch):
        spec = _spec()
        family = ab._candidate_fingerprint(spec, family=True)
        # A same-type (momentum), different-param replacement would STILL
        # collide on the family fingerprint (params are excluded from it) —
        # the replacement here must differ in block TYPE to actually escape.
        monkeypatch.setattr(
            "agent._fallback_composed",
            lambda: {
                "blocks": [{"type": "rsi", "role": "entry", "params": {"period": 14}}]
            },
        )

        result = ab._deduplicate_candidate(
            spec,
            seen=set(),
            zero_trade_families={family},
            run_id="run-4",
            round_num=1,
            iteration=1,
        )

        assert result is not spec


class TestExhaustion:
    def test_pool_exhausted_after_64_attempts_raises_runtimeerror(self, monkeypatch):
        """DeepR's core concern: a persistently-colliding fallback generator
        must surface a clear error, not loop forever or silently misbehave."""
        spec = _spec()
        exact = ab._candidate_fingerprint(spec)
        colliding = _fallback_proposal(lookback=99)
        colliding_fp = ab._candidate_fingerprint(ab._proposal_to_spec(colliding))
        calls = {"n": 0}

        def _always_colliding():
            calls["n"] += 1
            return colliding

        monkeypatch.setattr("agent._fallback_composed", _always_colliding)

        with pytest.raises(RuntimeError, match="fallback pool exhausted"):
            ab._deduplicate_candidate(
                spec,
                seen={exact, colliding_fp},
                zero_trade_families=set(),
                run_id="run-5",
                round_num=1,
                iteration=1,
            )
        assert calls["n"] == 64
