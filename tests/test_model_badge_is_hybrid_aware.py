"""Rozet, amaç-başına eşleme devredeyken tek model adı yazmasın.

`NAUTILUS_MODEL_BY_PURPOSE` eklendiğinde muhasebe doğruydu (token defteri çağrı
başına gerçek modeli yazar) ama EKRAN değildi: rozet koşunun pinini gösteriyor,
eşlenmiş amaç (ör. `custom_block`) başka uca gidiyordu. Bu, projenin kendi
ilkesine aykırı — bkz. [[model_secici_ve_gorunurluk]]: "hangi LLM'in koştuğu her
ekranda çözülmüş adıyla yazılır".

Üç yüzey var ve üçü de ayrı beslendiği için üçü de ayrı sınanır:
  1. SIMPLE/PRO rozeti  → routes/studio.llm_badge()
  2. AUTO kokpiti       → mission.mission_view()
  3. Sidebar ENGINE     → templating._engine_model_label()

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import pytest

import llm_client


@pytest.fixture(autouse=True)
def _reset_map():
    llm_client._purpose_models = None
    yield
    llm_client._purpose_models = None


class TestHybridNote:
    def test_empty_without_a_mapping(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_MODEL_BY_PURPOSE", raising=False)

        assert llm_client.hybrid_note() == ""

    def test_uses_readable_labels_not_raw_ids(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")

        # "claude-fable-5" değil "Fable 5" — rozetin yanında duracak metin bu.
        assert llm_client.hybrid_note() == "custom_block → Fable 5"

    def test_openrouter_mapping_keeps_the_account_visible(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "narrative=or:qwen3.8-27b")

        assert llm_client.hybrid_note() == "narrative → OR · qwen3.8-27b"


class TestSurfacesCarryIt:
    def test_simple_pro_badge_context(self, monkeypatch):
        from web.routes.studio import llm_badge

        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        ctx = llm_badge()

        assert ctx["llm_model_hybrid"] == "custom_block → Fable 5"
        # Ana etiket DEĞİŞMEZ: koşan/varsayılan model hâlâ o.
        assert "+hibrit" not in ctx["llm_model_label"]

    def test_simple_pro_badge_is_quiet_without_a_mapping(self, monkeypatch):
        from web.routes.studio import llm_badge

        monkeypatch.delenv("NAUTILUS_MODEL_BY_PURPOSE", raising=False)

        assert llm_badge()["llm_model_hybrid"] == ""

    def test_auto_cockpit_view_model(self, monkeypatch):
        from web.mission import mission_view

        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")
        mv = mission_view({"brief": {"model": "or:qwen3.8-27b"}})

        assert mv["model_hybrid"] == "custom_block → Fable 5"
        # Pin hâlâ kendi adıyla görünür — hibrit onu GİZLEMEZ, tamamlar.
        assert mv["model_label"] == "OR · qwen3.8-27b"

    def test_sidebar_engine_card(self, monkeypatch):
        from web.templating import _engine_model_label

        monkeypatch.setenv("NAUTILUS_MODEL_BY_PURPOSE", "custom_block=claude-fable-5")

        assert _engine_model_label().endswith(" +hibrit")

    def test_sidebar_engine_card_is_quiet_without_a_mapping(self, monkeypatch):
        from web.templating import _engine_model_label

        monkeypatch.delenv("NAUTILUS_MODEL_BY_PURPOSE", raising=False)

        assert "+hibrit" not in _engine_model_label()
