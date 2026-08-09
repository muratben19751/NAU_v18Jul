"""web_research.py (agent.py decomposition, Adım 1 — safe-first slice).

Zero test coverage before this file existed: _ddg_search/web_research_strategies
were only ever exercised transitively through propose_composed_strategy's full
LLM-generation flow, never directly.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

from types import SimpleNamespace

import web_research


class TestDdgSearch:
    def test_extracts_title_and_snippet_from_each_result(self, monkeypatch):
        fake_results = [
            {"title": "A", "body": "snippet a"},
            {"title": "B", "body": "snippet b"},
        ]
        monkeypatch.setattr(
            "ddgs.DDGS",
            lambda: SimpleNamespace(text=lambda query, max_results: fake_results),
        )

        out = web_research._ddg_search("btc strategy")

        assert out == [
            {"title": "A", "snippet": "snippet a"},
            {"title": "B", "snippet": "snippet b"},
        ]

    def test_passes_max_results_through(self, monkeypatch):
        captured = {}

        def _text(query, max_results):
            captured["max_results"] = max_results
            return []

        monkeypatch.setattr("ddgs.DDGS", lambda: SimpleNamespace(text=_text))

        web_research._ddg_search("q", max_results=7)

        assert captured["max_results"] == 7

    def test_none_result_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            "ddgs.DDGS", lambda: SimpleNamespace(text=lambda query, max_results: None)
        )

        assert web_research._ddg_search("q") == []

    def test_an_exception_degrades_to_empty_list_not_a_raise(self, monkeypatch):
        def _boom():
            raise RuntimeError("network unreachable")

        monkeypatch.setattr("ddgs.DDGS", _boom)

        assert web_research._ddg_search("q") == []

    def test_missing_fields_default_to_empty_strings(self, monkeypatch):
        monkeypatch.setattr(
            "ddgs.DDGS",
            lambda: SimpleNamespace(text=lambda query, max_results: [{}]),
        )

        assert web_research._ddg_search("q") == [{"title": "", "snippet": ""}]


class TestWebResearchStrategies:
    def test_no_snippets_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(web_research, "_ddg_search", lambda *a, **k: [])

        assert web_research.web_research_strategies("some hint") == ""

    def test_snippets_are_joined_with_the_research_header(self, monkeypatch):
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda *a, **k: [{"title": "T1", "snippet": "S1"}],
        )

        out = web_research.web_research_strategies("hint")

        assert out.startswith("WEB RESEARCH — Successful strategy ideas")
        assert "- T1: S1" in out

    def test_only_the_first_two_queries_are_searched(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda q, max_results=4: calls.append(q) or [],
        )

        web_research.web_research_strategies("hint")

        assert len(calls) == 2

    def test_snippets_are_capped_at_eight(self, monkeypatch):
        many = [{"title": f"T{i}", "snippet": f"S{i}"} for i in range(10)]
        monkeypatch.setattr(web_research, "_ddg_search", lambda *a, **k: many)

        out = web_research.web_research_strategies("hint")

        assert out.count("- T") == 8

    def test_blank_title_and_snippet_pairs_are_skipped(self, monkeypatch):
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda *a, **k: [{"title": "", "snippet": ""}],
        )

        assert web_research.web_research_strategies("hint") == ""

    def test_market_given_uses_market_specific_queries_not_crypto(self, monkeypatch):
        queries_seen = []
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda q, max_results=4: queries_seen.append(q) or [],
        )

        web_research.web_research_strategies(
            "", market="US equity QQQ.NASDAQ (1-DAY bars, ...)"
        )

        assert all("crypto" not in q.lower() for q in queries_seen)
        assert any("QQQ.NASDAQ" in q for q in queries_seen)

    def test_no_market_uses_crypto_queries(self, monkeypatch):
        queries_seen = []
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda q, max_results=4: queries_seen.append(q) or [],
        )

        web_research.web_research_strategies("")

        assert any("crypto" in q.lower() for q in queries_seen)

    def test_hint_is_prepended_to_the_first_query_when_given(self, monkeypatch):
        queries_seen = []
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda q, max_results=4: queries_seen.append(q) or [],
        )

        web_research.web_research_strategies("mean reversion")

        assert queries_seen[0].startswith("mean reversion ")

    def test_empty_hint_does_not_add_an_extra_query(self, monkeypatch):
        queries_seen = []
        monkeypatch.setattr(
            web_research,
            "_ddg_search",
            lambda q, max_results=4: queries_seen.append(q) or [],
        )

        web_research.web_research_strategies("   ")  # whitespace-only

        assert len(queries_seen) == 2
        assert not queries_seen[0].startswith("   ")
