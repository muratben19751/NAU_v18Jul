"""`NAUTILUS_LLM_CALL_TIMEOUT` `or:` pinli çağrılara da ulaşsın.

Zamanaşımı enjeksiyonu VARSAYILAN istemciye bakıyordu:

    if not isinstance(client, _ClaudeCLIClient):
        kwargs.setdefault("timeout", NAUTILUS_LLM_CALL_TIMEOUT)

Gerekçe doğruydu (CLI'ın kendi 300 s'lik tavanı var, onu daha kısa bir değerle
ezmeyelim) ama karar YANLIŞ şeye bakıyordu. Uygulamanın varsayılan backend'i
Claude CLI iken `or:` pinli bir çağrı yine de OpenRouter'a/yerel uca gider —
ve o dalda kwargs'a zamanaşımı hiç konmadığı için `_run_openrouter_killable`
kendi 120 s'lik varsayılanına düşüyordu. Yani ayar yerel uç için SESSİZCE ÖLÜYDÜ:
`NAUTILUS_LLM_CALL_TIMEOUT=300` yazıyordu, çağrı 120 s'de kesiliyordu.

Canlı kanıt (AUTO koşusu 0057a0cd, 2026-08-15): env'de 300 ayarlıyken bir
`composed` çağrısı `TimeoutError: OpenRouter call exceeded 120s hard deadline`
ile düştü.

Wiki References
---------------
See: [[kesilme_ve_degrade_gorunurlugu]], [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import pytest

import llm_client
import llm_dispatch


@pytest.fixture(autouse=True)
def _clean_pins(monkeypatch):
    llm_client._purpose_models = None
    llm_client.set_thread_model(None)
    monkeypatch.setattr(llm_client, "_active_model", None)
    monkeypatch.setattr(llm_dispatch, "_ledger_record", lambda *a, **k: None)
    monkeypatch.setattr(llm_dispatch, "_observe_llm", lambda **k: None)
    monkeypatch.setattr(llm_dispatch, "_admit_llm_request", lambda *a, **k: None)
    yield
    llm_client._purpose_models = None
    llm_client.set_thread_model(None)


def _fake_cli_client():
    """Uygulamanın VARSAYILAN backend'i abonelik CLI'ı olduğunda."""

    class _Messages:
        def create(self, **kwargs):  # pragma: no cover - bu testte çağrılmamalı
            raise AssertionError("or: pinli çağrı CLI'a gitmemeli")

    class _CLI(llm_client._ClaudeCLIClient):
        def __init__(self):
            self.messages = _Messages()

    return _CLI()


class TestOpenRouterGetsTheTimeout:
    def test_or_pin_receives_the_env_timeout_even_on_a_cli_default_backend(
        self, monkeypatch
    ):
        seen: dict = {}

        def _fake_or(model, **kwargs):
            seen.update(model=model, timeout=kwargs.get("timeout"))
            return type(
                "R",
                (),
                {
                    "content": [type("B", (), {"type": "text", "text": "{}"})()],
                    "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
                    "stop_reason": "end_turn",
                },
            )()

        monkeypatch.setattr(llm_dispatch, "_or_create_with_backoff", _fake_or)
        monkeypatch.setenv("NAUTILUS_LLM_CALL_TIMEOUT", "300")
        llm_client.set_thread_model("or:qwen3.8-27b")

        llm_dispatch._create_message_once(
            _fake_cli_client(),
            "composed",
            max_tokens=100,
            messages=[{"role": "user", "content": "x"}],
        )

        assert seen["model"] == "qwen3.8-27b"
        # Asıl iddia: 120'ye DÜŞMEDİ.
        assert seen["timeout"] == 300.0

    def test_purpose_mapped_to_openrouter_also_gets_it(self, monkeypatch):
        """Hibritte hedefi belirleyen pin değil, amaç-başına eşleme olabilir."""
        seen: dict = {}
        monkeypatch.setattr(
            llm_dispatch,
            "_or_create_with_backoff",
            lambda model, **kw: (
                seen.update(timeout=kw.get("timeout"))
                or type(
                    "R",
                    (),
                    {
                        "content": [type("B", (), {"type": "text", "text": "{}"})()],
                        "usage": type(
                            "U", (), {"input_tokens": 1, "output_tokens": 1}
                        )(),
                        "stop_reason": "end_turn",
                    },
                )()
            ),
        )
        monkeypatch.setenv("NAUTILUS_LLM_CALL_TIMEOUT", "300")
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "narrative=or:qwen3.8-27b")
        llm_client.set_thread_model("claude-fable-5")

        llm_dispatch._create_message_once(
            _fake_cli_client(),
            "narrative",
            max_tokens=100,
            messages=[{"role": "user", "content": "x"}],
        )

        assert seen["timeout"] == 300.0


class TestCliCeilingIsStillNotOverridden:
    def test_cli_call_gets_no_injected_timeout(self, monkeypatch):
        """Eski gerekçe korunmalı: CLI kendi tavanını kullansın."""
        seen: dict = {}

        class _Messages:
            def create(self, **kwargs):
                seen.update(kwargs)
                return type(
                    "R",
                    (),
                    {
                        "content": [type("B", (), {"type": "text", "text": "{}"})()],
                        "usage": type(
                            "U", (), {"input_tokens": 1, "output_tokens": 1}
                        )(),
                        "stop_reason": "end_turn",
                        "model": "claude-fable-5",
                    },
                )()

        class _CLI(llm_client._ClaudeCLIClient):
            def __init__(self):
                self.messages = _Messages()

        monkeypatch.setenv("NAUTILUS_LLM_CALL_TIMEOUT", "300")
        llm_client.set_thread_model("claude-fable-5")

        llm_dispatch._create_message_once(
            _CLI(),
            "composed",
            max_tokens=100,
            messages=[{"role": "user", "content": "x"}],
        )

        assert "timeout" not in seen
