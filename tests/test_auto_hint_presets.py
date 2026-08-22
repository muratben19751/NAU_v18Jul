"""AUTO brief'in GUIDANCE alanı: hazır prompt seçilir, düzenlenir, serbest yazılır.

İstek (2026-08-22): dört koşuda aynı hint'le kazanan çıkmayınca kalibre edilmiş
hazır prompt'lar "prompt alanına seçilir olarak" kondu — ama seçim alanı
KİLİTLEMEZ: seçilen metin düzenlenebilir, elle yazım aynen sürer.

Üç katman: liste (web/auto_hint_presets.py), şablon globali (templating.py),
brief formu (studio.html + fragments/auto_hint_presets.html). Bu dosya form
sözleşmesini de çiviler: alan hâlâ `name="hint"` ile POST edilir ve tek bir
alandır — input'tan textarea'ya geçişte sessiz bir çift alan oluşmasın.
"""

from __future__ import annotations

import json
import re

import pytest
from markupsafe import escape

from web import auto_hint_presets as ahp

# ---------------------------------------------------------------------------
# 1) Liste
# ---------------------------------------------------------------------------


def test_there_are_several_presets_and_they_validate():
    assert len(ahp.PRESETS) >= 3
    ahp.validate_presets()  # geçersizse ValueError


@pytest.mark.parametrize(
    "bad, msg",
    [
        (
            (
                {"key": "a", "label": "x", "text": "t"},
                {"key": "a", "label": "y", "text": "t"},
            ),
            "anahtar",
        ),
        (
            (
                {"key": "a", "label": "x", "text": "t"},
                {"key": "b", "label": "x", "text": "t"},
            ),
            "etiket",
        ),
        (({"key": "a", "label": "x", "text": "   "},), "boş"),
        (({"key": "a", "label": "x", "text": "t" * (ahp.MAX_TEXT_CHARS + 1)},), "uzun"),
        (({"key": "a b", "label": "x", "text": "t"},), "anahtar"),
    ],
)
def test_validation_refuses_duplicates_empties_and_overlong(bad, msg):
    with pytest.raises(ValueError, match=msg):
        ahp.validate_presets(bad)


# ---------------------------------------------------------------------------
# 2) Şablon globali + brief formu (gerçek GET /studio)
# ---------------------------------------------------------------------------


@pytest.fixture
def studio_html(monkeypatch):
    from fastapi.testclient import TestClient

    import web.routes.agent_backtest as ab
    import web.routes.studio as studio
    from server import app

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    with ab._AGENT_LOCK:
        saved = dict(ab._AGENT_PROGRESS)
        ab._AGENT_PROGRESS.clear()
    try:
        r = TestClient(app).get("/studio")
        assert r.status_code == 200, r.text[:300]
        yield r.text
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.clear()
            ab._AGENT_PROGRESS.update(saved)


def test_the_brief_offers_every_preset_as_a_choice(studio_html):
    assert 'id="mc-hint-preset"' in studio_html
    for p in ahp.PRESETS:
        # Autoescape: etiketteki ' → &#39; — sayfada görünen metin bu.
        assert str(escape(p["label"])) in studio_html, p["key"]
        assert f'value="{p["key"]}"' in studio_html


def test_the_preset_texts_reach_the_page_intact_via_json(studio_html):
    """Metin `<option value>`'da değil JSON'da gider — çok satırlı Türkçe metin
    attribute içinde bozulurdu. JSON geri okunabilmeli ve metinler birebir olmalı.
    """
    m = re.search(
        r'<script type="application/json" id="mc-hint-presets-data">(.*?)</script>',
        studio_html,
        re.S,
    )
    assert m, "preset JSON bloğu yok"
    data = json.loads(m.group(1))
    assert {d["key"]: d["text"] for d in data} == {
        p["key"]: p["text"] for p in ahp.PRESETS
    }


def test_the_field_is_one_editable_textarea_still_posted_as_hint(studio_html):
    """Seçim doldurur, kilitlemez: alan textarea, adı `hint`, ve TEK alan."""
    assert re.search(r'<textarea[^>]*id="mc-hint"[^>]*name="hint"', studio_html)
    assert not re.search(r'<input[^>]*name="hint"', studio_html), (
        "input'tan textarea'ya geçişte ikinci bir hint alanı kalmış"
    )
    assert "readonly" not in re.search(
        r'<textarea[^>]*id="mc-hint"[^>]*>', studio_html
    ).group(0)


def test_the_fragment_is_silent_when_the_global_is_missing():
    """Eski süreç (restart öncesi) şablonu canlı okur: global yoksa alan
    görünmez ama sayfa KIRILMAZ — `is defined` korumasının sözleşmesi.
    """
    from web.templating import templates

    env = templates.env
    tpl = env.get_template("fragments/auto_hint_presets.html")
    # Global'i bu render için gölgeleyerek "tanımsız" durumu üret.
    html = tpl.render(auto_hint_presets=env.undefined())
    assert "mc-hint-preset" not in html
