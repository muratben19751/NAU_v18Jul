"""Canvas inspector — the node → existing-partial mapping (Phase 2).

The point of the inspector is reuse: whatever it returns must carry the form
view's own hx-* attributes, because those ARE the mutation path. A partial that
rendered without them would look right and silently edit nothing.
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


@pytest.fixture()
def defn(client):
    from web.routes import strategy_studio as main

    return main.store.load(SID)


def _inspect(client, node_id):
    return client.get(f"/studio/{SID}/canvas/inspector/{node_id}")


def test_rule_opens_its_containing_block(client, defn):
    rule = defn.entry.rules[0]
    r = _inspect(client, f"rule:{rule.id}")
    assert r.status_code == 200
    # the block, not a bare rule — a lone rule's controls target `closest .block`
    assert 'id="block-entry"' in r.text
    assert f'id="rule-{rule.id}"' in r.text
    assert f'data-focus="{rule.id}"' in r.text
    assert f'hx-delete="/studio/{SID}/rules/{rule.id}"' in r.text


def test_risk_node_returns_the_risk_block(client):
    r = _inspect(client, "risk")
    assert r.status_code == 200
    assert 'id="block-risk"' in r.text
    assert f'data-url="/studio/{SID}/risk"' in r.text


def test_regime_node_returns_the_regime_block(client):
    r = _inspect(client, "regime")
    assert r.status_code == 200
    assert 'id="block-regime"' in r.text
    assert f'hx-patch="/studio/{SID}/blocks/regime"' in r.text


def test_group_node_returns_its_own_block(client):
    r = _inspect(client, "group:exit")
    assert r.status_code == 200
    assert 'id="block-exit"' in r.text
    assert f'hx-post="/studio/{SID}/blocks/exit/rules"' in r.text


def test_instrument_node_returns_the_instrument_chips(client):
    r = _inspect(client, "inst:XAUUSD")
    assert r.status_code == 200
    assert 'id="instruments"' in r.text
    assert f'hx-patch="/studio/{SID}/instruments/XAUUSD"' in r.text


def test_filter_rule_resolves_to_its_block(client, defn):
    rule = defn.entry.filters[0]
    r = _inspect(client, f"rule:{rule.id}")
    assert r.status_code == 200
    assert 'id="block-entry"' in r.text  # ".filter" suffix must not leak through


def test_unknown_node_is_404(client):
    assert _inspect(client, "rule:doesnotexist").status_code == 404
    assert _inspect(client, "group:nope").status_code == 404
    assert _inspect(client, "allocation").status_code == 404  # fixture has none


def test_inspector_is_read_only(client, defn):
    """The endpoint must not touch the definition it renders."""
    from web.routes import strategy_studio as main

    before = main.store.load(SID).model_dump_json()
    for node in (f"rule:{defn.entry.rules[0].id}", "risk", "regime", "group:entry"):
        assert _inspect(client, node).status_code == 200
    assert main.store.load(SID).model_dump_json() == before


def test_edit_through_the_inspector_hits_the_existing_endpoint(client, defn):
    """The whole design rests on this: canvas edits are form-view edits."""
    rule = defn.entry.rules[0]
    r = client.patch(
        f"/studio/{SID}/rules/{rule.id}", data={"param": "n1", "value": "12"}
    )
    assert r.status_code == 200
    again = _inspect(client, f"rule:{rule.id}")
    assert 'data-value="12"' in again.text
