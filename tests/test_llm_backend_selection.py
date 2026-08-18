"""Real backend-selection coverage for `_find_claude_cli`/`_build_client`/
`_get_client` (agent.py decomposition, Faz 2, Adım 7 — optional, test-only).

Before this file: zero direct tests. Every existing test monkeypatches
`agent._get_client` wholesale, so the real `NAUTILUS_LLM_BACKEND` branch
selection, the `~/.nautilus_proxy_key` file fallback, and
`_find_claude_cli`'s override/PATH lookup have never actually run in a
test. These tests call the real functions and mock only the genuine I/O
boundaries (env vars, `Path.home`, `shutil.which`) -- no production code
changes.

`llm_dispatch._client` is a module-level singleton `_get_client()` caches
into (agent.py decomposition Adım 9: the dispatch core, including `_client`,
moved to llm_dispatch.py -- `agent._get_client`/`agent._build_client`/
`agent._find_claude_cli` stay usable via re-export, but the singleton itself
and the internal names `_build_client`/`_get_client` read as bare globals now
live in llm_dispatch.py's own namespace, so this fixture and the monkeypatch
targets below point there directly); an autouse fixture resets it before and
after every test in this file so one test's build never leaks into the next
(the exact class of bug this session's own thread-local/BLOCK_CATALOG
findings warn about -- thread-local durum sonraki testi kirletir).

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from anthropic import Anthropic

import agent
import llm_dispatch


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    llm_dispatch._client = None
    yield
    llm_dispatch._client = None


class TestFindClaudeCli:
    def test_env_override_returns_the_path_when_it_exists(self, monkeypatch):
        import sys

        monkeypatch.setenv("NAUTILUS_CLAUDE_CLI", sys.executable)

        assert agent._find_claude_cli() == sys.executable

    def test_env_override_returns_none_when_the_path_does_not_exist(
        self, monkeypatch, tmp_path
    ):
        bogus = tmp_path / "does-not-exist" / "claude"
        monkeypatch.setenv("NAUTILUS_CLAUDE_CLI", str(bogus))

        assert agent._find_claude_cli() is None

    def test_falls_back_to_shutil_which_when_no_override(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_CLAUDE_CLI", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: f"/fake/path/to/{name}")

        assert agent._find_claude_cli() == "/fake/path/to/claude"


class TestBuildClient:
    def test_backend_openrouter_delegates_to_build_openrouter_client(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "openrouter")
        sentinel = object()
        # _build_client (llm_dispatch.py, Adım 9) reads _build_openrouter_client
        # as its own bare module global -- patching agent's re-exported copy
        # would not reach it.
        monkeypatch.setattr(llm_dispatch, "_build_openrouter_client", lambda: sentinel)

        assert agent._build_client() is sentinel

    def test_backend_api_with_env_key_builds_a_real_anthropic_client(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-from-env")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://example.invalid:6655")

        client = agent._build_client()

        assert isinstance(client, Anthropic)
        assert client.api_key == "test-key-from-env"
        assert str(client.base_url).startswith("http://example.invalid:6655")

    def test_backend_api_falls_back_to_the_proxy_key_file(self, monkeypatch, tmp_path):
        from pathlib import Path

        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "api")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".nautilus_proxy_key").write_text(
            "key-from-file\n", encoding="utf-8"
        )

        client = agent._build_client()

        assert isinstance(client, Anthropic)
        assert client.api_key == "key-from-file"

    def test_backend_api_without_any_key_raises(self, monkeypatch, tmp_path):
        from pathlib import Path

        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "api")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no key file here

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            agent._build_client()

    def test_backend_auto_without_a_key_falls_through_to_the_claude_cli(
        self, monkeypatch, tmp_path
    ):
        from pathlib import Path

        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # _build_client (llm_dispatch.py, Adım 9) reads _find_claude_cli as its
        # own bare module global -- patching agent's re-exported copy would
        # not reach it.
        monkeypatch.setattr(llm_dispatch, "_find_claude_cli", lambda: "/fake/claude")

        client = agent._build_client()

        assert isinstance(client, agent._ClaudeCLIClient)

    def test_backend_claude_cli_without_the_cli_found_raises(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "claude-cli")
        monkeypatch.setattr(llm_dispatch, "_find_claude_cli", lambda: None)

        with pytest.raises(RuntimeError, match="claude-cli"):
            agent._build_client()

    def test_backend_auto_with_nothing_available_raises(self, monkeypatch, tmp_path):
        from pathlib import Path

        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(llm_dispatch, "_find_claude_cli", lambda: None)

        with pytest.raises(RuntimeError, match="No LLM access"):
            agent._build_client()


class TestGetClient:
    def test_is_a_singleton_and_only_builds_once(self, monkeypatch):
        calls = []

        def _fake_build():
            calls.append(1)
            return object()

        # _get_client (llm_dispatch.py, Adım 9) reads _build_client as its own
        # bare module global -- patching agent's re-exported copy would not
        # reach it.
        monkeypatch.setattr(llm_dispatch, "_build_client", _fake_build)

        first = agent._get_client()
        second = agent._get_client()

        assert first is second
        assert len(calls) == 1


class TestLedgerRecord:
    """`llm_dispatch._ledger_record` (Adım 8 characterization; Adım 9 moved the
    function itself from agent.py to llm_dispatch.py -- NOT re-exported back
    into agent.py, so these calls target llm_dispatch directly; see
    llm_dispatch.py's own docstring for why).

    Patches ``token_ledger.record`` via the STRING form, never
    ``llm_dispatch._ledger_record`` directly -- the documented-safe pattern
    from test_regression_anchors.py's ``_no_real_ledger_writes`` autouse
    fixture, so a real row is never written to the developer's actual
    ~/.cache/nautilus_web_app/token_usage.jsonl.
    """

    def test_attributes_to_the_actual_responding_model(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "token_ledger.record",
            lambda model, usage, purpose: captured.update(
                model=model, usage=usage, purpose=purpose
            ),
        )
        resp = SimpleNamespace(model="claude-opus-4-8", usage=SimpleNamespace())

        llm_dispatch._ledger_record(resp, "claude-fable-5", "composed")

        assert captured["model"] == "claude-opus-4-8"
        assert captured["purpose"] == "composed"

    def test_falls_back_to_called_model_when_response_has_no_model_attribute(
        self, monkeypatch
    ):
        captured = {}
        monkeypatch.setattr(
            "token_ledger.record",
            lambda model, usage, purpose: captured.update(model=model),
        )
        # No `.model` attribute at all -- matches the Claude CLI backend's
        # real response shape (llm_client._CLIResponse has only `content`,
        # `usage`, `stop_reason`).
        resp = SimpleNamespace(usage=SimpleNamespace())

        llm_dispatch._ledger_record(resp, "claude-fable-5", "composed")

        assert captured["model"] == "claude-fable-5"

    def test_swallows_a_token_ledger_write_failure(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("token_ledger.record", _raise)
        resp = SimpleNamespace(model="claude-opus-4-8", usage=SimpleNamespace())

        llm_dispatch._ledger_record(
            resp, "claude-fable-5", "composed"
        )  # must not raise

    def test_is_credit_exhausted_recognizes_the_insufficient_credit_signal(self):
        assert agent._is_credit_exhausted(
            Exception("Error: insufficient credit balance")
        )
