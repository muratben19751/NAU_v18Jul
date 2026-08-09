"""llm_client.py — LLM control plane: cancellation, telemetry, budget
admission, degradation tagging (agent.py decomposition, Adım 3 — safe-first
slice).

Covers gaps the DeepR research pass on agent.py found before this file
existed: _check_llm_cancelled/_raise_if_llm_control_abort/
_interruptible_sleep/_tag_degraded/_fallback_rng were never called by name
in any test -- only exercised as side effects of other tests driving the
full _create_message_once dispatch path.

_interruptible_sleep has since moved to openrouter_backend.py (Faz 2, Adım
6 -- it calls _sleep, and its only production caller, _or_create_with_backoff,
is OpenRouter-only). Tested here via `agent._interruptible_sleep` (the
re-export) for the same "cover the gap regardless of which file it ends up
in" reason, but the `_sleep` patch target is `openrouter_backend._sleep` --
`_interruptible_sleep`'s own bare-name lookup now resolves there, not
against agent's namespace.

Wiki References
---------------
See: [[kesilme_ve_degrade_gorunurlugu]].
"""

from __future__ import annotations

import pytest

import agent
import llm_client
import openrouter_backend


@pytest.fixture(autouse=True)
def _reset_llm_control():
    """Every test in this file installs a thread-local cancel_check without
    its own cleanup (each relies on the NEXT test in file order resetting
    it, e.g. test_no_cancel_check_installed_is_a_noop's own opening reset) --
    fine as long as this file's tests always run consecutively, but a stale
    `lambda: False`/`lambda: True` cancel_check leaking into a DIFFERENT
    file's test (e.g. an OpenRouter backoff test that also drives
    _interruptible_sleep) silently flips it from a single _sleep(wait) call
    to a real 0.25s-granularity polling loop -- reproduced directly: running
    this file ahead of test_agent_run_form.py's 429 backoff tests made them
    spin for millions of iterations instead of asserting on the expected
    exact sleep durations.
    """
    llm_client.set_thread_llm_control(None, None, None)
    yield
    llm_client.set_thread_llm_control(None, None, None)


class TestCheckLlmCancelled:
    def test_no_cancel_check_installed_is_a_noop(self):
        llm_client.set_thread_llm_control(None, None, None)

        llm_client._check_llm_cancelled()  # must not raise

    def test_cancel_check_returning_false_is_a_noop(self):
        llm_client.set_thread_llm_control(lambda: False, None, None)

        llm_client._check_llm_cancelled()  # must not raise

    def test_cancel_check_returning_true_raises_llm_call_cancelled(self):
        llm_client.set_thread_llm_control(lambda: True, None, None)

        with pytest.raises(llm_client.LLMCallCancelled):
            llm_client._check_llm_cancelled()


class TestRaiseIfLlmControlAbort:
    def test_llm_call_cancelled_is_reraised(self):
        exc = llm_client.LLMCallCancelled("stopped")

        with pytest.raises(llm_client.LLMCallCancelled):
            llm_client._raise_if_llm_control_abort(exc)

    def test_an_exception_flagged_llm_control_abort_is_reraised(self):
        exc = RuntimeError("budget")
        exc.llm_control_abort = True

        with pytest.raises(RuntimeError, match="budget"):
            llm_client._raise_if_llm_control_abort(exc)

    def test_a_plain_exception_is_not_reraised(self):
        exc = RuntimeError("just a normal provider error")

        llm_client._raise_if_llm_control_abort(exc)  # must not raise

    def test_a_falsy_llm_control_abort_attribute_is_not_reraised(self):
        exc = RuntimeError("provider hiccup")
        exc.llm_control_abort = False

        llm_client._raise_if_llm_control_abort(exc)  # must not raise


class TestInterruptibleSleep:
    def test_no_cancel_check_installed_sleeps_via_the_patchable_alias(
        self, monkeypatch
    ):
        llm_client.set_thread_llm_control(None, None, None)
        calls = []
        monkeypatch.setattr(openrouter_backend, "_sleep", lambda s: calls.append(s))

        agent._interruptible_sleep(0.4)

        assert calls == [0.4]

    def test_cancellation_mid_sleep_raises_promptly(self, monkeypatch):
        # A cancel_check that flips to True after the first poll -- the sleep
        # must stop checking/raise rather than sleeping out the full duration.
        polls = {"n": 0}

        def _cancel_check():
            polls["n"] += 1
            return polls["n"] > 1

        llm_client.set_thread_llm_control(_cancel_check, None, None)
        monkeypatch.setattr(
            openrouter_backend, "_sleep", lambda s: None
        )  # instant, no real wait

        with pytest.raises(llm_client.LLMCallCancelled):
            agent._interruptible_sleep(10.0)

        assert polls["n"] >= 2

    def test_a_never_cancelled_sleep_completes_without_raising(self, monkeypatch):
        llm_client.set_thread_llm_control(lambda: False, None, None)
        monkeypatch.setattr(openrouter_backend, "_sleep", lambda s: None)

        agent._interruptible_sleep(0.01)  # must not raise


class TestTagDegraded:
    def test_stamps_all_three_fields(self):
        fb = {"name": "fallback strategy"}

        out = agent._tag_degraded(fb, RuntimeError("provider down"))

        assert out["degraded"] == "RuntimeError"
        assert out["degraded_detail"] == "provider down"
        assert out["degraded_terminal"] is False

    def test_mutates_in_place_and_returns_the_same_object(self):
        fb = {"name": "x"}

        out = agent._tag_degraded(fb, RuntimeError("x"))

        assert out is fb

    def test_terminal_errors_are_flagged_terminal(self):
        fb = {}

        out = agent._tag_degraded(fb, RuntimeError("invalid api key"))

        assert out["degraded_terminal"] is True

    def test_detail_is_truncated_at_500_characters(self):
        fb = {}
        long_message = "x" * 1000

        out = agent._tag_degraded(fb, RuntimeError(long_message))

        assert len(out["degraded_detail"]) == 500


class TestFallbackRng:
    def test_no_seed_pinned_returns_the_global_random_module(self):
        agent.set_thread_random_seed(None)

        assert agent._fallback_rng() is agent.random

    def test_a_pinned_seed_returns_a_dedicated_rng_not_the_global_module(self):
        agent.set_thread_random_seed("run-1")

        rng = agent._fallback_rng()

        assert rng is not agent.random
        assert hasattr(rng, "choice")

    def test_the_same_seed_is_deterministic(self):
        agent.set_thread_random_seed("same-seed")
        first = agent._fallback_rng().randint(1, 1_000_000)

        agent.set_thread_random_seed("same-seed")
        second = agent._fallback_rng().randint(1, 1_000_000)

        assert first == second

        agent.set_thread_random_seed(None)  # don't leak the pin to later tests
