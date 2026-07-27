"""Concurrent studio edits must all survive.

Every mutation route is a read-modify-write over the whole definition document.
Before `StrategyStore.editing` held a lock across all three steps, two
overlapping requests both read the same base and the second write dropped the
first — and the losing request still returned 200 with a fragment that looked
applied. Measured then: 24 concurrent instrument adds left 2; an instrument add
racing a rule edit lost one of the two in 10 of 10 runs.

These tests assert the accumulated EFFECT, not the status codes. Asserting
"every response was 200" passes just as happily on the broken version — the
whole point of this bug is that the lost write reports success.
"""

import concurrent.futures as cf

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
    c = TestClient(_host)
    c.store = store
    return c


def _symbols(client):
    d, _ = client.store.working_copy(SID)
    return {i.symbol for i in d.instruments}


def test_concurrent_adds_all_survive(client):
    n = 24
    syms = [f"STRESS{i:03d}" for i in range(n)]

    def add(sym):
        return client.post(
            f"/studio/{SID}/instruments", data={"symbol": sym, "timeframe": "1h"}
        ).status_code

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        codes = list(ex.map(add, syms))

    assert set(codes) == {200}
    landed = _symbols(client) & set(syms)
    assert landed == set(syms), f"lost {sorted(set(syms) - landed)}"


def test_concurrent_edits_to_different_blocks_do_not_clobber(client):
    """The document is read and written whole, so disjoint fields do not help."""
    d, _ = client.store.working_copy(SID)
    rule = d.regime.conditions.rules[0]
    param = next(iter(rule.params))

    def add_instrument():
        return client.post(
            f"/studio/{SID}/instruments", data={"symbol": "RACEA", "timeframe": "1h"}
        ).status_code

    def edit_rule():
        return client.patch(
            f"/studio/{SID}/rules/{rule.id}", data={"param": param, "value": "77"}
        ).status_code

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(add_instrument), ex.submit(edit_rule)
        assert f1.result() == 200 and f2.result() == 200

    d, _ = client.store.working_copy(SID)
    assert "RACEA" in {i.symbol for i in d.instruments}, "instrument edit was lost"
    edited = next(r for r in d.regime.conditions.rules if r.id == rule.id)
    assert edited.params[param].value == 77, "rule edit was lost"


def test_concurrent_deletes_cannot_empty_the_instrument_list(client):
    """The 'one must stay active' guard must hold under concurrency.

    Before the lock this passed by accident — last-write-wins left two
    instruments because each request validated its own private copy. The
    invariant has to be enforced against the state actually being written.
    """
    d, _ = client.store.working_copy(SID)
    syms = [i.symbol for i in d.instruments]
    assert len(syms) >= 2

    def remove(sym):
        return client.delete(f"/studio/{SID}/instruments/{sym}").status_code

    with cf.ThreadPoolExecutor(max_workers=len(syms)) as ex:
        codes = list(ex.map(remove, syms))

    left = _symbols(client)
    assert len(left) >= 1, "every instrument was deleted"
    assert 422 in codes, "the last-active guard never fired — it was not enforced"


def test_rejected_mutation_leaves_no_draft(client):
    """A 422 must not create a draft as a side effect of taking the edit path."""
    assert client.store.load_draft(SID) is None
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "BTC/USDT", "timeframe": "1h"}
    )
    assert r.status_code == 422
    assert client.store.load_draft(SID) is None

    r = client.patch(
        f"/studio/{SID}/blocks/entry", data={"else_mode": "flat"}
    )  # else_mode only applies to regime
    assert r.status_code == 422
    assert client.store.load_draft(SID) is None


def test_symbol_length_is_capped(client):
    r = client.post(
        f"/studio/{SID}/instruments", data={"symbol": "A" * 5000, "timeframe": "1h"}
    )
    assert r.status_code == 422 and "too long" in r.text
    assert not any(len(s) > 32 for s in _symbols(client))
