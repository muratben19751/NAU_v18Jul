"""Real backend-selection coverage for `_find_claude_cli`/`_build_client`/
`_get_client` (agent.py decomposition, Faz 2, Adım 7 — optional, test-only).

Before this file: zero direct tests. Every existing test monkeypatches
`agent._get_client` wholesale, so the real `NAUTILUS_LLM_BACKEND` branch
selection, the `~/.nautilus_proxy_key` file fallback, and
`_find_claude_cli`'s override/PATH lookup have never actually run in a
test. These tests call the real functions and mock only the genuine I/O
boundaries (env vars, `Path.home`, `shutil.which`) -- no production code
changes.

`agent._client` is a module-level singleton `_get_client()` caches into;
an autouse fixture resets it before and after every test in this file so
one test's build never leaks into the next (the exact class of bug this
session's own thread-local/BLOCK_CATALOG findings warn about -- see
[[thread_local_testte_sonraki_testi_kirletir]]).

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import shutil

import pytest
from anthropic import Anthropic

import agent


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    agent._client = None
    yield
    agent._client = None


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
        monkeypatch.setattr(agent, "_build_openrouter_client", lambda: sentinel)

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
        monkeypatch.setattr(agent, "_find_claude_cli", lambda: "/fake/claude")

        client = agent._build_client()

        assert isinstance(client, agent._ClaudeCLIClient)

    def test_backend_claude_cli_without_the_cli_found_raises(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "claude-cli")
        monkeypatch.setattr(agent, "_find_claude_cli", lambda: None)

        with pytest.raises(RuntimeError, match="claude-cli"):
            agent._build_client()

    def test_backend_auto_with_nothing_available_raises(self, monkeypatch, tmp_path):
        from pathlib import Path

        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(agent, "_find_claude_cli", lambda: None)

        with pytest.raises(RuntimeError, match="No LLM access"):
            agent._build_client()


class TestGetClient:
    def test_is_a_singleton_and_only_builds_once(self, monkeypatch):
        calls = []

        def _fake_build():
            calls.append(1)
            return object()

        monkeypatch.setattr(agent, "_build_client", _fake_build)

        first = agent._get_client()
        second = agent._get_client()

        assert first is second
        assert len(calls) == 1
