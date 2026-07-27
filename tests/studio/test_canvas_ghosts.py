"""Ghost suggestions on the canvas (Phase 4).

A pending suggestion is not part of the definition — it lives in the store — so
``to_graph`` takes it as an argument instead of reaching for it. These check
that separation holds and that a ghost lands on the block it was made for.
"""

from __future__ import annotations

import json

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


def _add_ghost(block="entry", kind="add_rule", diff=None, source="manual", sid="g1"):
    from web.routes import strategy_studio as main

    main.store.add_suggestion(
        sid,
        SID,
        block,
        json.dumps(
            {
                "kind": kind,
                "block": block,
                "diff": diff if diff is not None else {"indicator": "rsi"},
                "rationale": "momentum confirmation",
            }
        ),
        "review",
        source=source,
    )
    return sid


def test_to_graph_ignores_ghosts_it_is_not_given():
    """Purity: the store is not a hidden input."""
    from scripts.seed_studio import build_fixture
    from strategy_studio.graph import to_graph

    defn = build_fixture()
    assert not [n for n in to_graph(defn)["nodes"] if n["kind"] == "ghost"]
    g = to_graph(defn, ghosts=[{"id": "x", "block": "entry", "label": "Add Rule"}])
    assert [n["id"] for n in g["nodes"] if n["kind"] == "ghost"] == ["ghost:x"]


def test_ghost_hangs_off_its_block(client):
    _add_ghost(block="entry")
    g = client.get(f"/studio/{SID}/canvas/graph").json()
    ghost = next(n for n in g["nodes"] if n["kind"] == "ghost")
    assert ghost["label"] == "Add Rule" and ghost["detail"] == "rsi"
    edge = next(e for e in g["edges"] if e["source"] == ghost["id"])
    assert edge["target"] == "group:entry" and edge["kind"] == "ghost"
    assert (
        ghost["layer"]
        < next(n for n in g["nodes"] if n["id"] == "group:entry")["layer"]
    )


def test_ghost_for_a_missing_block_is_dropped(client):
    """The fixture has no allocation block, so there is nothing to hang it on."""
    _add_ghost(block="allocation")
    g = client.get(f"/studio/{SID}/canvas/graph").json()
    assert not [n for n in g["nodes"] if n["kind"] == "ghost"]


def test_ghosts_do_not_count_as_rules(client):
    before = client.get(f"/studio/{SID}/canvas/graph").json()["meta"]["counts"]["rules"]
    _add_ghost()
    after = client.get(f"/studio/{SID}/canvas/graph").json()["meta"]["counts"]["rules"]
    assert after == before


def test_selecting_a_ghost_opens_its_block_with_accept_and_dismiss(client):
    sid = _add_ghost(
        block="risk", kind="modify_risk", diff={"name": "take_profit_r", "value": 2.5}
    )
    r = client.get(f"/studio/{SID}/canvas/inspector/ghost:{sid}")
    assert r.status_code == 200
    assert 'id="block-risk"' in r.text
    assert f"/studio/{SID}/ai/suggestions/{sid}/accept" in r.text
    assert f"/studio/{SID}/ai/suggestions/{sid}/dismiss" in r.text


def test_loop_ghosts_are_labelled_as_such(client):
    _add_ghost(source="loop")
    g = client.get(f"/studio/{SID}/canvas/graph").json()
    assert next(n for n in g["nodes"] if n["kind"] == "ghost")["badge"] == "AI · LOOP"


def test_accepting_a_ghost_turns_it_into_a_rule(client):
    sid = _add_ghost(block="entry")
    before = client.get(f"/studio/{SID}/canvas/graph").json()
    assert client.post(f"/studio/{SID}/ai/suggestions/{sid}/accept").status_code == 200
    after = client.get(f"/studio/{SID}/canvas/graph").json()
    assert not [n for n in after["nodes"] if n["kind"] == "ghost"]
    assert after["meta"]["counts"]["rules"] == before["meta"]["counts"]["rules"] + 1
