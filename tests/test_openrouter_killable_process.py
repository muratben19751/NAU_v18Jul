"""Real multiprocessing spawn/pipe/timeout/kill coverage for
``_run_openrouter_killable`` (agent.py decomposition, Faz 2 — Adım 5,
written test-first; Adım 6 then moved the whole OpenRouter backend to
openrouter_backend.py, re-exported from agent.py so
``agent._run_openrouter_killable`` still works unchanged here -- only the
``_openrouter_process_main`` patch target moved, since
``_run_openrouter_killable``'s own bare-name lookup now resolves against
openrouter_backend's namespace, not agent's).

Before this file, the ONLY test touching this path
(``test_auto_360_fixes.py::test_openrouter_auto_path_uses_killable_process``)
replaces ``_run_openrouter_killable`` with a fake before calling it — the
real ``multiprocessing.get_context("spawn")``/``Pipe``/``Process``/poll/kill
mechanics had zero real execution in any test. These tests call the real,
unmodified ``_run_openrouter_killable`` and swap out only the child's own
body (``_openrouter_process_main``, which would otherwise make a real
network call) via ``tests/_probe_openrouter.py`` — a checked-in, importable
module, required because "spawn" pickles the ``target=`` callable by
reference and the child re-imports whatever module currently defines it.

``PM2_HOME`` is unset under pytest, so ``sandbox.py``'s ``pythonw.exe``
gate (see [[llm_maliyet_kaldiraclari]]) never fires here — spawn always
uses the default console-attached interpreter, which is exactly the case
that does not hang. There is no ``pytest-timeout`` plugin in this repo: the
"kills the child" test below is deliberately testing the one mechanism
meant to prevent a hang, so if it ever regresses, this test can hang the
suite rather than fail cleanly — an accepted, deliberate trade-off, not an
oversight.

Wiki References
---------------
See: [[llm_maliyet_kaldiraclari]].
"""

from __future__ import annotations

import time

import _probe_openrouter
import pytest

import agent
import llm_client
import openrouter_backend


@pytest.fixture(autouse=True)
def _no_cancel_control():
    llm_client.set_thread_llm_control(None, None, None)
    yield
    llm_client.set_thread_llm_control(None, None, None)


class TestRunOpenrouterKillable:
    def test_ok_payload_returns_the_real_result(self, monkeypatch):
        monkeypatch.setattr(
            openrouter_backend,
            "_openrouter_process_main",
            _probe_openrouter.probe_openrouter_main,
        )

        result = agent._run_openrouter_killable(
            {"_probe_mode": "ok", "_probe_text": "hello from the child"},
            {"base_url": "https://example.invalid", "api_key": "x"},
            timeout=10.0,
        )

        assert result["ok"] is True
        assert result["text"] == "hello from the child"
        assert result["finish_reason"] == "stop"
        assert result["prompt_tokens"] == 11
        assert result["completion_tokens"] == 22

    def test_error_payload_raises_process_error_with_status_code(self, monkeypatch):
        monkeypatch.setattr(
            openrouter_backend,
            "_openrouter_process_main",
            _probe_openrouter.probe_openrouter_main,
        )

        with pytest.raises(agent._OpenRouterProcessError) as exc_info:
            agent._run_openrouter_killable(
                {
                    "_probe_mode": "error",
                    "_probe_error_type": "RateLimitError",
                    "_probe_error_message": "rate limited",
                    "_probe_status_code": 429,
                },
                {"base_url": "https://example.invalid", "api_key": "x"},
                timeout=10.0,
            )

        assert exc_info.value.status_code == 429
        assert "RateLimitError" in str(exc_info.value)
        assert "rate limited" in str(exc_info.value)

    def test_timeout_kills_the_child_before_it_can_finish(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            openrouter_backend,
            "_openrouter_process_main",
            _probe_openrouter.probe_openrouter_main,
        )
        marker = tmp_path / "reached.marker"

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            agent._run_openrouter_killable(
                {
                    "_probe_mode": "hang",
                    "_probe_sleep": 20.0,
                    "_probe_marker_path": str(marker),
                },
                {"base_url": "https://example.invalid", "api_key": "x"},
                timeout=2.0,
            )
        elapsed = time.monotonic() - started

        # The parent gave up promptly -- nowhere near the 20s the child would
        # have slept if left alone. Generous margin over the 2.0s deadline for
        # spawn/poll/join overhead on a loaded Windows box.
        assert elapsed < 10.0, elapsed
        # The child was actually TERMINATED (proc.kill()) while still asleep,
        # not merely abandoned by the parent -- if _stop_provider_process
        # silently failed to kill it, the sleep would complete and this file
        # would exist. Wall-clock timing alone could pass even with a broken
        # kill, as long as the parent's own poll loop still gave up on
        # schedule; this assertion is the one that actually proves the child
        # died.
        assert not marker.exists()
