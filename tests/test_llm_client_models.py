"""llm_client.py — model/effort/OpenRouter-catalog selection (agent.py
decomposition, Adım 2 — safe-first slice).

Covers gaps the DeepR research pass on agent.py found before this file
existed: model_label/model_id were never called by any test; openrouter_
free_only/_openrouter_options/_or_row had zero direct calls; openrouter_
catalog's dual-TTL cache logic was ALWAYS monkeypatched away wholesale,
so its real cache-hit/expiry/fail-TTL branching had never actually run in
a test. (resolve_effort/current_model/current_effort/selectable_models/
_is_free/openrouter_paid_extras already had good coverage in
tests/test_agent_run_form.py and are not duplicated here.)

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import pytest

import llm_client


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    llm_client._or_catalog = []
    llm_client._or_catalog_at = 0.0
    yield
    llm_client._or_catalog = []
    llm_client._or_catalog_at = 0.0


class TestModelLabel:
    def test_no_model_resolves_via_current_model(self, monkeypatch):
        monkeypatch.setattr(llm_client, "current_model", lambda: "claude-sonnet-5")

        assert llm_client.model_label(None) == "Sonnet 5"
        assert llm_client.model_label("") == "Sonnet 5"

    def test_or_prefixed_model_is_labeled_with_the_account_marker(self):
        assert llm_client.model_label("or:deepseek/deepseek-chat") == (
            "OR · deepseek/deepseek-chat"
        )

    def test_known_model_id_uses_the_friendly_label(self):
        assert llm_client.model_label("claude-opus-5") == "Opus 5"

    def test_unknown_model_id_falls_back_to_the_id_itself(self):
        assert llm_client.model_label("some-future-model") == "some-future-model"


class TestModelId:
    def test_no_model_resolves_via_current_model(self, monkeypatch):
        monkeypatch.setattr(llm_client, "current_model", lambda: "claude-opus-5")

        assert llm_client.model_id(None) == "claude-opus-5"
        assert llm_client.model_id("") == "claude-opus-5"

    def test_or_prefix_is_preserved_unlike_model_label(self):
        # model_label formats "or:x" into "OR · x"; model_id keeps the raw
        # picker value as-is (it feeds set_thread_model, which parses "or:").
        assert llm_client.model_id("or:deepseek/deepseek-chat") == (
            "or:deepseek/deepseek-chat"
        )

    def test_strips_whitespace(self):
        assert llm_client.model_id("  claude-sonnet-5  ") == "claude-sonnet-5"


class TestOpenrouterFreeOnly:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_OPENROUTER_FREE_ONLY", raising=False)

        assert llm_client.openrouter_free_only() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
    def test_falsy_values_disable_the_filter(self, monkeypatch, value):
        monkeypatch.setenv("NAUTILUS_OPENROUTER_FREE_ONLY", value)

        assert llm_client.openrouter_free_only() is False

    @pytest.mark.parametrize("value", ["1", "yes", "true", "on", "anything"])
    def test_other_values_keep_the_filter_on(self, monkeypatch, value):
        monkeypatch.setenv("NAUTILUS_OPENROUTER_FREE_ONLY", value)

        assert llm_client.openrouter_free_only() is True


class TestOpenrouterOptions:
    def test_pinned_models_bypass_catalog_and_free_only_entirely(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_OPENROUTER_MODELS", "vendor/a, vendor/b")
        monkeypatch.setattr(
            llm_client,
            "openrouter_catalog",
            lambda: (_ for _ in ()).throw(AssertionError("must not hit the catalog")),
        )

        out = llm_client._openrouter_options()

        assert out == [
            ("or:vendor/a", "OR · vendor/a"),
            ("or:vendor/b", "OR · vendor/b"),
        ]

    def test_free_only_filters_out_paid_catalog_entries(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
        monkeypatch.delenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", raising=False)
        monkeypatch.setattr(llm_client, "openrouter_free_only", lambda: True)
        monkeypatch.setattr(
            llm_client,
            "openrouter_catalog",
            lambda: [("free/a", "Free A", True), ("paid/b", "Paid B", False)],
        )

        out = llm_client._openrouter_options()

        assert out == [("or:free/a", "OR · Free A")]

    def test_extra_models_are_appended_even_when_paid(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
        monkeypatch.setenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", "paid/b")
        monkeypatch.setattr(llm_client, "openrouter_free_only", lambda: True)
        monkeypatch.setattr(
            llm_client,
            "openrouter_catalog",
            lambda: [("free/a", "Free A", True), ("paid/b", "Paid B", False)],
        )

        out = llm_client._openrouter_options()

        assert ("or:paid/b", "OR · Paid B · paralı") in out

    def test_empty_catalog_falls_back_to_the_static_defaults(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_OPENROUTER_MODELS", raising=False)
        monkeypatch.delenv("NAUTILUS_OPENROUTER_EXTRA_MODELS", raising=False)
        monkeypatch.setattr(llm_client, "openrouter_free_only", lambda: True)
        monkeypatch.setattr(llm_client, "openrouter_catalog", lambda: [])

        out = llm_client._openrouter_options()

        expected_ids = {
            f"or:{s}" for s in llm_client._DEFAULT_OPENROUTER_FREE_MODELS.split(",")
        }
        assert {mid for mid, _ in out} == expected_ids


class TestOrRow:
    def test_free_row_has_no_paid_marker(self):
        assert llm_client._or_row("vendor/x", "Vendor X", True) == (
            "or:vendor/x",
            "OR · Vendor X",
        )

    def test_paid_row_is_labeled(self):
        assert llm_client._or_row("vendor/x", "Vendor X", False) == (
            "or:vendor/x",
            "OR · Vendor X · paralı",
        )


class TestOpenrouterCatalogDualTtlCache:
    """Real cache logic — only the HTTP-fetch boundary (_fetch_openrouter_catalog)
    is mocked, unlike every prior test of this function (which monkeypatched
    openrouter_catalog itself away wholesale)."""

    def test_a_second_call_within_ttl_does_not_refetch(self, monkeypatch):
        calls = {"n": 0}

        def _fake_fetch():
            calls["n"] += 1
            return [("vendor/x", "Vendor X", True)]

        monkeypatch.setattr(llm_client, "_fetch_openrouter_catalog", _fake_fetch)

        first = llm_client.openrouter_catalog()
        second = llm_client.openrouter_catalog()

        assert calls["n"] == 1
        assert first == second == [("vendor/x", "Vendor X", True)]

    def test_a_call_after_the_success_ttl_expires_refetches(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            llm_client,
            "_fetch_openrouter_catalog",
            lambda: calls.update(n=calls["n"] + 1) or [("v", "V", True)],
        )
        monkeypatch.setattr(llm_client, "_OR_CATALOG_TTL", 0.0)

        llm_client.openrouter_catalog()
        llm_client.openrouter_catalog()

        assert calls["n"] == 2

    def test_a_failed_fetch_returns_empty_and_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(
            llm_client,
            "_fetch_openrouter_catalog",
            lambda: (_ for _ in ()).throw(RuntimeError("network down")),
        )

        assert llm_client.openrouter_catalog() == []

    def test_a_failed_fetch_is_retried_after_only_the_shorter_fail_ttl(
        self, monkeypatch
    ):
        """The empty-catalog branch uses _OR_CATALOG_FAIL_TTL (60s), not the
        long success TTL (3600s) -- a down endpoint must not be treated as
        cache-valid for a full hour."""
        calls = {"n": 0}

        def _fake_fetch():
            calls["n"] += 1
            raise RuntimeError("network down")

        monkeypatch.setattr(llm_client, "_fetch_openrouter_catalog", _fake_fetch)
        # Success TTL stays long (proves the FAIL ttl, not the success ttl,
        # is what governs an empty-cache retry).
        monkeypatch.setattr(llm_client, "_OR_CATALOG_TTL", 3600.0)
        monkeypatch.setattr(llm_client, "_OR_CATALOG_FAIL_TTL", 0.0)

        llm_client.openrouter_catalog()
        llm_client.openrouter_catalog()

        assert calls["n"] == 2

    def test_force_bypasses_the_cache_even_within_ttl(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            llm_client,
            "_fetch_openrouter_catalog",
            lambda: calls.update(n=calls["n"] + 1) or [("v", "V", True)],
        )

        llm_client.openrouter_catalog()
        llm_client.openrouter_catalog(force=True)

        assert calls["n"] == 2
