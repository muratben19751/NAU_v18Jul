"""Canvas mutations (Phase 3) — the paths the drop and the ✕ actually take.

There is nothing canvas-specific to test on the server, and that IS the test:
the canvas calls the endpoints the form view already had, so the same server
rules bind it. These assert the round trip a drag-and-drop performs — mutate
through the existing endpoint, then read the graph back — and that a refused
mutation stays refused no matter which view asked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SID = "wt-funding-v3"


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


def _graph(client):
    return client.get(f"/studio/{SID}/canvas/graph").json()


def _rule_nodes(graph, block=None):
    return [
        n
        for n in graph["nodes"]
        if n["kind"] in ("rule", "filter") and (block is None or n["block"] == block)
    ]


def test_drop_adds_exactly_one_node(client):
    before = _graph(client)
    r = client.post(f"/studio/{SID}/blocks/exit/rules", data={"indicator": "rsi"})
    assert r.status_code == 200
    after = _graph(client)
    assert len(after["nodes"]) == len(before["nodes"]) + 1
    fresh = {n["id"] for n in after["nodes"]} - {n["id"] for n in before["nodes"]}
    assert len(fresh) == 1
    node = next(n for n in after["nodes"] if n["id"] in fresh)
    assert node["kind"] == "rule" and node["block"] == "exit"
    assert node["label"] == "RSI"
    # and it is immediately inspectable — the drop selects what it created
    assert client.get(f"/studio/{SID}/canvas/inspector/{node['id']}").status_code == 200


def test_drop_as_filter_lands_on_the_dashed_edge(client):
    client.post(
        f"/studio/{SID}/blocks/exit/rules",
        data={"indicator": "rsi", "as_filter": "true"},
    )
    g = _graph(client)
    node = next(n for n in _rule_nodes(g, "exit.filter") if n["label"] == "RSI")
    edge = next(e for e in g["edges"] if e["source"] == node["id"])
    assert edge["kind"] == "filter" and edge["target"] == "group:exit"


def test_delete_removes_exactly_one_node(client):
    g = _graph(client)
    victim = _rule_nodes(g, "exit")[0]
    r = client.delete(f"/studio/{SID}/rules/{victim['ref']['rule_id']}")
    assert r.status_code == 200
    after = _graph(client)
    assert len(after["nodes"]) == len(g["nodes"]) - 1
    assert victim["id"] not in {n["id"] for n in after["nodes"]}
    # its edges went with it — the graph is derived, never patched
    assert victim["id"] not in {e["source"] for e in after["edges"]}


def test_the_last_entry_rule_cannot_be_deleted_from_the_canvas_either(client):
    """The canvas opens no new write path, so this server rule binds it too."""
    for node in _rule_nodes(_graph(client), "entry"):
        client.delete(f"/studio/{SID}/rules/{node['ref']['rule_id']}")
    remaining = _rule_nodes(_graph(client), "entry")
    assert len(remaining) == 1
    r = client.delete(f"/studio/{SID}/rules/{remaining[0]['ref']['rule_id']}")
    assert r.status_code == 422
    assert "at least one rule" in r.text
    assert len(_rule_nodes(_graph(client), "entry")) == 1


def test_canvas_page_carries_palette_and_save_controls(client):
    html = client.get(f"/studio/{SID}/canvas").text
    assert 'class="lib-item"' in html or "lib-item" in html
    assert 'draggable="true"' in html  # the drag source
    assert f'hx-post="/studio/{SID}/save"' in html
    assert f'hx-post="/studio/{SID}/discard"' in html
    assert "studio.js" in html  # partial behaviour comes with it


def test_discard_restores_the_saved_graph(client):
    before = _graph(client)
    client.post(f"/studio/{SID}/blocks/exit/rules", data={"indicator": "rsi"})
    assert len(_graph(client)["nodes"]) == len(before["nodes"]) + 1
    client.post(f"/studio/{SID}/discard")
    assert {n["id"] for n in _graph(client)["nodes"]} == {
        n["id"] for n in before["nodes"]
    }
