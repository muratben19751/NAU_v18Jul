"""Consistent free-text length cap across every LLM-calling /strategy route.

DeepR 2026-08-08 [ORTA]: only /strategy/blocks/generate checked
`_MAX_LLM_TEXT_LEN` — /suggest, /drafts/chat, and /blocks/chat took the same
kind of free-text field straight into a Claude call with no limit, so a
pasted multi-MB blob would be forwarded whole (extra token cost, and a risk
of exceeding the model's context window on an otherwise ordinary request).

Each route's guard must reject BEFORE the LLM is called — every LLM entry
point below is monkeypatched to raise, so a passing test proves the call was
never reached, not just that the eventual response happened to look right.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOO_LONG = "x" * 4001  # strategy._MAX_LLM_TEXT_LEN + 1


@pytest.fixture()
def client():
    from server import app

    return TestClient(app)


def _boom(*a, **k):
    raise AssertionError("LLM entry point was called despite the oversized input")


class TestSuggestLengthLimit:
    def test_oversized_description_is_rejected_before_calling_the_llm(
        self, client, monkeypatch
    ):
        import web.routes.strategy as strategy_mod

        monkeypatch.setattr(strategy_mod, "propose_composed_strategy", _boom)

        r = client.post("/strategy/suggest", data={"description": TOO_LONG})

        assert r.status_code == 400
        assert "too long" in r.text.lower()

    def test_ordinary_description_is_accepted(self, client, monkeypatch):
        import web.routes.strategy as strategy_mod

        monkeypatch.setattr(
            strategy_mod,
            "propose_composed_strategy",
            lambda history, catalog, hint="": (
                {
                    "blocks": [],
                    "description": hint,
                    "name": "x",
                    "strategy_options": {},
                },
                {},
            ),
        )

        r = client.post("/strategy/suggest", data={"description": "a short ask"})

        assert r.status_code == 200


class TestDraftsChatLengthLimit:
    def test_oversized_message_is_rejected_before_calling_the_llm(
        self, client, monkeypatch
    ):
        import web.routes.strategy as strategy_mod

        conv_id = strategy_mod._DRAFTS_CHAT.new(
            {"messages": [], "working_blocks": [], "last_error": ""}
        )
        monkeypatch.setattr("agent.chat_edit_blocks", _boom)

        r = client.post(
            "/strategy/drafts/chat",
            data={"conv_id": conv_id, "message": TOO_LONG},
        )

        assert r.status_code == 400
        assert "too long" in r.text.lower()


class TestBlockChatLengthLimit:
    def test_oversized_message_is_rejected_before_calling_the_llm(
        self, client, monkeypatch
    ):
        import web.routes.strategy as strategy_mod

        conv_id = strategy_mod._BLOCK_CHAT.new(
            {
                "messages": [],
                "name": "blk",
                "working_meta": {},
                "working_code": "",
                "last_error": "",
            }
        )
        monkeypatch.setattr("agent.chat_edit_block", _boom)

        r = client.post(
            "/strategy/blocks/chat",
            data={"conv_id": conv_id, "message": TOO_LONG},
        )

        assert r.status_code == 400
        assert "too long" in r.text.lower()
