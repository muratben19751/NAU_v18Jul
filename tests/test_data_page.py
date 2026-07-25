"""Data (/data) page — unified instrument card redesign smoke tests.

See plan: data page design2 monochrome redesign. These tests run at the render
level (TestClient); they do not trigger heavy data-fetching paths.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from server import app

    return TestClient(app)


class TestDataPageRedesign:
    def test_page_renders_english_and_unified_grid(self, client):
        r = client.get("/data")
        assert r.status_code == 200
        body = r.text
        # English header + compact page header
        assert "Instrument Catalog" in body
        # Unified timeframe grid used across all sources
        assert "tf-grid" in body
        # Chip legend (state vocabulary self-documents)
        assert "chip-legend" in body
        # No leftover Turkish UI strings
        assert "Enstrüman" not in body
        assert "Filtre" not in body

    def test_no_dead_bybit_cell_wrap_selector(self, client):
        """Dead .bybit-cell-wrap hx-on JS was removed."""
        r = client.get("/data")
        assert "bybit-cell-wrap" not in r.text

    def test_css_defines_previously_missing_classes(self, client):
        """.small and .panel-meta are defined in app.css (regression guard)."""
        r = client.get("/static/app.css")
        assert r.status_code == 200
        css = r.text
        assert ".small" in css
        assert ".panel-meta" in css
        assert ".tf-grid" in css
        # Old classes cleaned up
        assert ".bybit-grid" not in css
        assert ".bar-list" not in css

    def test_discover_returns_html_not_json(self, client):
        """POST /data/index/discover returns an HTML fragment (not JSON)."""
        r = client.post("/data/index/discover", data={"force": False})
        # 404 if INDEX_ROOT missing; 200 + HTML otherwise. Never a raw JSON body.
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert "text/html" in r.headers.get("content-type", "")
            assert "tickers" in r.text
            assert not r.text.lstrip().startswith("{")

    def test_catalog_button_has_busy_feedback(self, client):
        """Catalog-write button exposes an idle/busy label pair for feedback."""
        r = client.get("/static/app.css")
        assert ".tf-catalog-busy" in r.text
        assert ".tf-catalog-form.htmx-request" in r.text

    def test_dead_row_fragment_endpoint_removed(self, client):
        """GET /data/fragments/row/... removed → 404."""
        r = client.get("/data/fragments/row/bybit/BTCUSDT")
        assert r.status_code == 404
