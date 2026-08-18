"""Amaç-başına model eşlemesi (`NAUTILUS_MODEL_BY_PURPOSE`) — hibrit koşu.

Model pini KOŞU başınaydı: bir turu bir uca bağlıyordun ve o turun her çağrısı
oraya gidiyordu. Ölçüm (2026-08-15) bunun tek düğme olmasının pahalı olduğunu
gösterdi — yerel Qwen3.8-27B `composed`'da 10/10, `custom_block`'ta 4/8.
`model_for_purpose` o düğmeyi amaç bazında böler.

Buradaki testlerin ÇOĞU dispatch'in kendi çağrı yolundan geçer (sahte istemci +
gerçek `_create_message_once`): eşlemenin doğru ayrıştırılması tek başına bir şey
kanıtlamaz — kanıt, sağlayıcıya GİDEN model adıdır.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]], [[llm_maliyet_kaldiraclari]] (max_tokens
tavanı modelin üslubuna bağlıdır — ölçüm orada).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import llm_client
import llm_dispatch


@pytest.fixture(autouse=True)
def _reset_purpose_map_and_pins(monkeypatch):
    """Cache ve thread-local'lar testler arası sızmasın."""
    llm_client._purpose_models = None
    llm_client.set_thread_model(None)
    monkeypatch.setattr(llm_client, "_active_model", None)
    yield
    llm_client._purpose_models = None
    llm_client.set_thread_model(None)


class _FakeMessages:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def create(self, *, model: str, **kwargs):
        self._sink.append(model)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{}")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
            model=model,
        )


class _FakeClient:
    def __init__(self, sink: list[str]) -> None:
        self.messages = _FakeMessages(sink)


@pytest.fixture
def called_models(monkeypatch):
    """Sağlayıcıya giden model adlarını toplayan sahte istemci."""
    sink: list[str] = []
    monkeypatch.setattr(llm_dispatch, "_ledger_record", lambda *a, **k: None)
    monkeypatch.setattr(llm_dispatch, "_observe_llm", lambda **k: None)
    monkeypatch.setattr(llm_dispatch, "_admit_llm_request", lambda *a, **k: None)
    return sink, _FakeClient(sink)


class TestParsing:
    def test_no_env_means_current_model_for_every_purpose(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_MODEL_BY_PURPOSE", raising=False)
        llm_client.set_thread_model("or:qwen3.8-27b")

        assert llm_client.purpose_model_map() == {}
        for purpose in ("composed", "custom_block", "narrative", ""):
            assert llm_client.model_for_purpose(purpose) == llm_client.current_model()

    def test_mapped_purpose_wins_over_the_run_pin(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        llm_client.set_thread_model("or:qwen3.8-27b")

        assert llm_client.model_for_purpose("custom_block") == "claude-fable-5"
        # Eşlenmemiş amaçlar pinde kalır — hibridin tamamı bu ayrımda.
        assert llm_client.model_for_purpose("composed") == "or:qwen3.8-27b"

    def test_invalid_entries_are_dropped_not_passed_through(self, monkeypatch, caplog):
        monkeypatch.setenv(
            "NAUTILUS_MODEL_BY_PURPOSE",
            "custom_block=not-a-real-model,composed=claude-opus-5,garbage",
        )

        assert llm_client.purpose_model_map() == {"composed": "claude-opus-5"}
        assert "not-a-real-model" in caplog.text

    def test_cache_refreshes_when_the_env_value_changes(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "idea=claude-opus-5")
        assert llm_client.purpose_model_map() == {"idea": "claude-opus-5"}

        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "idea=claude-haiku-4-5")
        assert llm_client.purpose_model_map() == {"idea": "claude-haiku-4-5"}


class TestCreditRule:
    """Kredi tükenmesi bir FATURA gerçeği — ama yalnız kendi hesabında."""

    def test_credit_fallback_outranks_a_claude_mapping(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "idea=claude-opus-5")
        monkeypatch.setattr(llm_client, "_active_model", llm_client.FALLBACK_MODEL)

        assert llm_client.model_for_purpose("idea") == llm_client.FALLBACK_MODEL

    def test_credit_fallback_does_not_touch_an_openrouter_mapping(self, monkeypatch):
        """ "Claude'un kredisi bitti, yereline geç" tam burada çalışmalı."""
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "idea=or:qwen3.8-27b")
        monkeypatch.setattr(llm_client, "_active_model", llm_client.FALLBACK_MODEL)

        assert llm_client.model_for_purpose("idea") == "or:qwen3.8-27b"


class TestDispatchActuallyRoutes:
    """Asıl kanıt: sağlayıcıya giden model adı."""

    def test_mapped_purpose_reaches_the_provider_with_the_mapped_model(
        self, monkeypatch, called_models
    ):
        sink, client = called_models
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        llm_client.set_thread_model("claude-opus-5")

        llm_dispatch._create_message_once(
            client,
            "custom_block",
            max_tokens=100,
            messages=[{"role": "user", "content": "x"}],
        )

        assert sink == ["claude-fable-5"]

    def test_unmapped_purpose_still_reaches_the_provider_with_the_pin(
        self, monkeypatch, called_models
    ):
        sink, client = called_models
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        llm_client.set_thread_model("claude-opus-5")

        llm_dispatch._create_message_once(
            client,
            "composed",
            max_tokens=100,
            messages=[{"role": "user", "content": "x"}],
        )

        assert sink == ["claude-opus-5"]


class TestLearnedCeilingIsKeyedByTheEffectiveModel:
    """Öğrenilen tavan yanlış uca sızmasın.

    `current_model()` anahtarda kalsaydı, yerel uçta öğrenilen 7200'lük tavan
    aynı turda Claude'a giden `custom_block` çağrısına da uygulanırdı — hiç
    doğrulanmamış bir uçta gereksiz büyük bütçe.
    """

    def test_key_uses_the_mapped_model_not_the_pin(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        llm_client.set_thread_model("or:qwen3.8-27b")

        seen: dict = {}

        def _spy(client, purpose="", **kwargs):
            seen["max_tokens"] = kwargs.get("max_tokens")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="{}")],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            )

        monkeypatch.setattr(llm_dispatch, "_create_message_once", _spy)
        monkeypatch.setattr(
            llm_dispatch,
            "_LEARNED_MAX_TOKENS",
            {("or:qwen3.8-27b", "custom_block"): 7200},
        )

        llm_dispatch._create_message(object(), "custom_block", max_tokens=1800)

        # Yerel uç için öğrenilen tavan Claude çağrısına UYGULANMADI.
        assert seen["max_tokens"] == 1800
