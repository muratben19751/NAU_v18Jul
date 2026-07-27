"""Canvas view routes — the read-only pair added in Phase 1.

Both are GETs by construction: the canvas never introduces a second way to
write to a strategy, it re-uses the form view's mutation endpoints.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    return TestClient(_host)


def test_canvas_page_renders(client):
    r = client.get("/studio/wt-funding-v3/canvas")
    assert r.status_code == 200
    for needle in ["cv-embed", "cv-svg", "canvas.js", "canvas.css", "Form view"]:
        assert needle in r.text


def test_canvas_page_404(client):
    assert client.get("/studio/nope/canvas").status_code == 404


def test_graph_endpoint_shape(client):
    r = client.get("/studio/wt-funding-v3/canvas/graph")
    assert r.status_code == 200
    g = r.json()
    assert set(g) == {"nodes", "edges", "meta"}
    assert g["meta"]["strategy_id"] == "wt-funding-v3"
    assert g["meta"]["counts"]["nodes"] == len(g["nodes"])
    ids = {n["id"] for n in g["nodes"]}
    assert "risk" in ids and "group:entry" in ids
    for e in g["edges"]:  # nothing dangles
        assert e["source"] in ids and e["target"] in ids


def test_graph_endpoint_404(client):
    assert client.get("/studio/nope/canvas/graph").status_code == 404


def test_form_view_links_to_canvas(client):
    r = client.get("/studio/wt-funding-v3")
    assert '/studio/wt-funding-v3/canvas"' in r.text
