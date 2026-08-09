"""Composer page UI-language consistency (DeepR 2026-08-09 [DÜŞÜK]).

catalog_list.html's action-icon tooltips (switch-to-backtest/edit/copy) were
Turkish while every other button label and table header on the same panel
(compose_body.html's "05 · Saved Strategies") is English -- a stray,
isolated inconsistency with no connection to any Turkish-language feature.

NOT touched: compose_body.html's "💬 AI ile düzenle" button and
custom_blocks_list.html's "✏️ AI ile düzenle" button. Both are the entry
point to a deliberately Turkish, LLM-chat-driven editing feature (see
web/routes/strategy.py's drafts_chat_new/block chat handlers -- the
empty-state message, the chat's first bubble, and the prompt sent to the
model are all Turkish too). Translating only the entry-point button would
have made THAT feature less consistent, not more -- this test locks in
that those labels stay as they are.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import pathlib

_CATALOG_LIST = (
    pathlib.Path(__file__).resolve().parents[1]
    / "web"
    / "templates"
    / "fragments"
    / "catalog_list.html"
).read_text(encoding="utf-8")

_COMPOSE_BODY = (
    pathlib.Path(__file__).resolve().parents[1]
    / "web"
    / "templates"
    / "fragments"
    / "compose_body.html"
).read_text(encoding="utf-8")


class TestCatalogListActionTooltipsAreEnglish:
    def test_old_turkish_tooltips_are_gone(self):
        for stray in (
            "Bu stratejiyle Backtest sekmesine geç",
            "Düzenle (Save üzerine yazar)",
            "Kopyala (Save yeni strateji oluşturur)",
        ):
            assert stray not in _CATALOG_LIST, (
                f"stray Turkish tooltip still present: {stray!r}"
            )

    def test_new_english_tooltips_are_present(self):
        for expected in (
            "Switch to the Backtest tab with this strategy",
            "Edit (Save overwrites this record)",
            "Copy (Save creates a new strategy)",
        ):
            assert expected in _CATALOG_LIST


class TestAiChatEntryPointsStayTurkish:
    """The deliberately-Turkish LLM-chat feature naming is left alone --
    see the module docstring for why translating just the button would
    hurt, not help, consistency."""

    def test_drafts_chat_button_is_unchanged(self):
        assert (
            'title="Blok listesini AI ile düzenle">💬 AI ile düzenle</button>'
            in _COMPOSE_BODY
        )
