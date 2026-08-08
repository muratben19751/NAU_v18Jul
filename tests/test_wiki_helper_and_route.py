"""wiki_helper.py + web/routes/wiki.py — zero test coverage before this
(DeepR 2026-08-08 [ORTA]). The path-traversal rejection in `wiki_path`
(`.resolve()` + `relative_to()`) is the highest-stakes gap: a regression
there (e.g. someone "simplifying" the resolve/relative_to pair) would let a
request read arbitrary files on disk, and nothing would catch it.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import wiki_helper


@pytest.fixture()
def wiki_root(tmp_path, monkeypatch):
    """An isolated WIKI_ROOT with a couple of real-shaped pages."""
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "sources").mkdir(parents=True)
    (tmp_path / "wiki" / "entities" / "message_bus.md").write_text(
        "---\ntitle: MessageBus\nsummary: pub/sub backbone\n---\n\n# MessageBus\n",
        encoding="utf-8",
    )
    (tmp_path / "wiki" / "entities" / "cache.md").write_text(
        "# Cache\n\nSee [[message_bus]] and [[missing_page]].\n", encoding="utf-8"
    )
    (tmp_path / "sources" / "01_notes.md").write_text(
        "# Source notes\n", encoding="utf-8"
    )
    monkeypatch.setattr(wiki_helper, "WIKI_ROOT", tmp_path)
    wiki_helper._slug_index.cache_clear()
    yield tmp_path
    wiki_helper._slug_index.cache_clear()


class TestStripFrontmatter:
    def test_removes_a_well_formed_block(self):
        text = "---\ntitle: X\n---\nBody text\n"
        assert wiki_helper._strip_frontmatter(text) == "Body text\n"

    def test_no_frontmatter_is_unchanged(self):
        text = "# Just a heading\n"
        assert wiki_helper._strip_frontmatter(text) == text

    def test_unterminated_frontmatter_is_left_alone(self):
        """No closing '---' — must not consume the whole document."""
        text = "---\ntitle: X\nno closing fence\n"
        assert wiki_helper._strip_frontmatter(text) == text


class TestSlugIndex:
    def test_resolves_a_page_under_wiki(self, wiki_root):
        path = wiki_helper.resolve_slug("message_bus")
        assert path == wiki_root / "wiki" / "entities" / "message_bus.md"

    def test_unknown_slug_is_none(self, wiki_root):
        assert wiki_helper.resolve_slug("does_not_exist") is None

    def test_wiki_dir_wins_over_sources_on_a_stem_collision(self, wiki_root):
        (wiki_root / "sources" / "message_bus.md").write_text(
            "duplicate stem", encoding="utf-8"
        )
        wiki_helper._slug_index.cache_clear()

        path = wiki_helper.resolve_slug("message_bus")

        assert path == wiki_root / "wiki" / "entities" / "message_bus.md"


class TestRewriteWikilinks:
    def test_resolved_link_becomes_a_markdown_link(self, wiki_root):
        out = wiki_helper._rewrite_wikilinks("See [[message_bus]] for details.")
        assert "[message bus](/wiki/wiki/entities/message_bus.md)" in out

    def test_custom_label_is_used_and_escaped(self, wiki_root):
        out = wiki_helper._rewrite_wikilinks("[[message_bus|The <Bus>]]")
        assert "[The &lt;Bus&gt;](/wiki/wiki/entities/message_bus.md)" in out

    def test_unresolved_slug_renders_as_visible_bold_not_a_dead_link(self, wiki_root):
        out = wiki_helper._rewrite_wikilinks("[[nope_not_a_page]]")
        assert out == "**[nope not a page]**"
        assert "](" not in out

    def test_wikilink_syntax_inside_a_fenced_code_block_is_untouched(self, wiki_root):
        md = "Example:\n```\n[[message_bus]]\n```\n"
        out = wiki_helper._rewrite_wikilinks(md)
        assert "[[message_bus]]" in out
        assert "/wiki/wiki/entities/message_bus.md" not in out

    def test_wikilink_syntax_inside_inline_code_is_untouched(self, wiki_root):
        out = wiki_helper._rewrite_wikilinks("Use `[[slug]]` syntax.")
        assert "`[[slug]]`" in out


class TestReadWikiPage:
    def test_strips_frontmatter_and_rewrites_links(self, wiki_root):
        body = wiki_helper.read_wiki_page("wiki/entities/cache.md")
        assert "title:" not in body
        assert "[message bus](/wiki/wiki/entities/message_bus.md)" in body
        assert "**[missing page]**" in body

    def test_bare_slug_is_accepted_like_a_rel_path(self, wiki_root):
        assert wiki_helper.read_wiki_page("message_bus") == wiki_helper.read_wiki_page(
            "wiki/entities/message_bus.md"
        )

    def test_missing_page_returns_a_visible_placeholder_not_an_exception(
        self, wiki_root
    ):
        out = wiki_helper.read_wiki_page("wiki/entities/nope.md")
        assert "not found" in out.lower()


class TestWikiUrlAndLinkMd:
    def test_wiki_url_of_a_resolved_page(self, wiki_root):
        assert (
            wiki_helper.wiki_url("message_bus") == "/wiki/wiki/entities/message_bus.md"
        )

    def test_wiki_url_of_an_unresolved_page_falls_back_to_file_url(self, wiki_root):
        url = wiki_helper.wiki_url("nope")
        assert url.startswith("file://")

    def test_wiki_link_md_default_label_is_titleized(self, wiki_root):
        md = wiki_helper.wiki_link_md("wiki/entities/message_bus.md")
        assert md == "[📖 Message Bus](/wiki/wiki/entities/message_bus.md)"


# ---------------------------------------------------------------------------
# web/routes/wiki.py — HTTP layer, against the REAL nautilus_wiki/ (the
# traversal guard's job is to reject escapes from whatever WIKI_ROOT is).
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from server import app

    return TestClient(app)


class TestWikiIndexRoute:
    def test_index_renders(self, client):
        r = client.get("/wiki")
        assert r.status_code == 200


class TestWikiBySlugRoute:
    def test_known_slug_renders(self, client):
        r = client.get("/wiki/slug/webapp_module_map")
        assert r.status_code == 200

    def test_unknown_slug_is_404(self, client):
        r = client.get("/wiki/slug/definitely_not_a_real_slug_xyz")
        assert r.status_code == 404


class TestWikiPathTraversalGuard:
    """The guard this whole module exists to test. A regression here (e.g.
    dropping `.resolve()` or the `relative_to()` check) turns `/wiki/*` into
    an arbitrary-file-read endpoint.

    httpx (TestClient's transport) collapses `..` dot-segments client-side
    before the request is even sent — `client.get("/wiki/../x")` never
    reaches the route with a literal `..` in it, so a `client.get(...)`-based
    "test" here would pass even with the guard deleted (verified: it 404s
    upstream of the handler with Starlette's generic "Not Found", not the
    handler's own "Path escapes wiki root"). Calling the async route
    function directly with a literal `..` `rel_path` exercises the actual
    guard code regardless of what any particular HTTP client normalizes.
    """

    @staticmethod
    def _call(rel_path: str):
        import asyncio

        from web.routes.wiki import wiki_path

        return asyncio.run(wiki_path(request=None, rel_path=rel_path))

    def test_dot_dot_escape_to_a_real_md_file_outside_wiki_root_is_rejected(self):
        # nautilus_wiki/../CLAUDE.md resolves to a REAL .md file at the repo
        # root — this is the target that actually proves the guard matters:
        # it passes both the exists() and suffix==".md" checks, so with
        # resolve()/relative_to() removed this call returns 200 with the
        # project's real CLAUDE.md rendered (verified while writing this
        # test, by temporarily deleting the guard — confirms it's not a
        # tautology that would also 404 for an unrelated reason).
        with pytest.raises(Exception) as exc_info:
            self._call("../CLAUDE.md")
        assert getattr(exc_info.value, "status_code", None) == 404
        assert "escapes wiki root" in str(exc_info.value.detail).lower()

    def test_dot_dot_escape_to_a_non_markdown_file_is_also_rejected_by_the_guard(self):
        """A non-.md traversal target would ALSO 404 via the suffix check if
        the relative_to() guard were removed — but with a different message
        ("No wiki page at ..." vs "Path escapes wiki root"), so asserting the
        exact message still catches a dropped guard even here."""
        with pytest.raises(Exception) as exc_info:
            self._call("../server.py")
        assert getattr(exc_info.value, "status_code", None) == 404
        assert "escapes wiki root" in str(exc_info.value.detail).lower()

    def test_http_level_dot_dot_request_also_never_reaches_a_file_outside_root(
        self, client
    ):
        """Not a guard test (httpx normalizes this before sending — see class
        docstring) — pins that the observable behavior for a normal client
        is ALSO a 404, so a future httpx/Starlette change can't silently
        start letting this through some other way."""
        r = client.get("/wiki/../CLAUDE.md")
        assert r.status_code == 404

    def test_ordinary_in_root_page_is_served(self, client):
        r = client.get("/wiki/index.md")
        assert r.status_code == 200

    def test_nonexistent_in_root_page_is_a_plain_404(self, client):
        r = client.get("/wiki/wiki/entities/does_not_exist_xyz.md")
        assert r.status_code == 404

    def test_non_markdown_file_is_rejected_even_if_it_exists(self, client):
        r = client.get("/wiki/tools/wiki_tools.py")
        assert r.status_code == 404
