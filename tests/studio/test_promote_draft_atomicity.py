"""Promoting a draft must not delete an edit that arrived while it ran.

`promote_draft` used to be three separate transactions: load draft → save as a
new version → delete draft. A `save_draft` landing in the gap (UI autosave, or
the AI loop writing back an accepted suggestion) was destroyed by the trailing
delete — the user's newest edit disappeared with no error anywhere.

The sequence is now one BEGIN IMMEDIATE transaction, and the delete is scoped to
the exact json that was read.
"""

from __future__ import annotations

import threading
import time

import pytest

from scripts.seed_studio import build_engine_fixture
from strategy_studio.schema import Param
from strategy_studio.store import StrategyStore


@pytest.fixture()
def store(tmp_path):
    st = StrategyStore(tmp_path / "t.db")
    st.save(build_engine_fixture())
    return st


def _tweaked(defn, take_profit_r: float):
    return defn.model_copy(
        update={
            "risk": defn.risk.model_copy(
                update={"take_profit_r": Param(value=take_profit_r)}
            )
        }
    )


def test_promote_persists_the_draft_and_clears_it(store):
    base = store.load(build_engine_fixture().id)
    store.save_draft(_tweaked(base, 4.4))

    version = store.promote_draft(base.id)

    assert version == base.version + 1
    assert store.load(base.id).risk.take_profit_r.value == 4.4
    assert store.load_draft(base.id) is None


def test_promote_runs_in_a_single_transaction(store, monkeypatch):
    """One connection, therefore one transaction, therefore no gap.

    This is the property that fixes the race, asserted directly rather than by
    racing threads: the old implementation was `load_draft` + `save` +
    `delete_draft`, three connections and three commits, and a `save_draft` from
    another thread landing in either gap was destroyed by the trailing delete.
    A thread-timing test could not tell the two apart reliably — the window is
    microseconds — so the structure is what gets pinned. Fails (3 != 1) the
    moment anyone splits this back into separate store calls.
    """
    base = store.load(build_engine_fixture().id)
    store.save_draft(_tweaked(base, 4.4))

    connects = []
    original = StrategyStore._connect

    def _counting(self):
        connects.append(1)
        return original(self)

    monkeypatch.setattr(StrategyStore, "_connect", _counting)
    store.promote_draft(base.id)
    monkeypatch.undo()

    assert len(connects) == 1, (
        f"promote_draft opened {len(connects)} connections — the draft delete is "
        "outside the version-insert transaction, so a concurrent save_draft can "
        "still be swallowed"
    )


def test_concurrent_draft_write_during_promote_loses_nothing(store, monkeypatch):
    """End-state check under real threads: neither write may vanish.

    The interleaving is pinned, not left to the scheduler: the concurrent
    `save_draft` is released only once promote's transaction holds the
    BEGIN IMMEDIATE write lock (signalled from inside `_insert_version`), so
    it is guaranteed to arrive DURING the promote — the exact window the old
    three-transaction implementation lost writes in. It queues on the SQLite
    lock and commits after, so it must survive as the next draft. Without the
    sync, a loaded machine could commit the 9.9 write before promote even
    read the draft, and promote would legitimately promote 9.9 instead —
    valid last-write-wins, but not the window under test.
    """
    base = store.load(build_engine_fixture().id)
    sid = base.id
    store.save_draft(_tweaked(base, 4.4))

    errors: list[str] = []
    in_txn = threading.Event()
    original = StrategyStore._insert_version

    def _signalling(self, con, defn):
        # The BEGIN IMMEDIATE write lock is held here. Wake the concurrent
        # writer and give it a moment to reach the lock before proceeding.
        in_txn.set()
        time.sleep(0.05)
        return original(self, con, defn)

    def _concurrent_edit():
        try:
            if not in_txn.wait(timeout=10):
                raise TimeoutError("promote never entered its transaction")
            store.save_draft(_tweaked(base, 9.9))
        except Exception as e:  # noqa: BLE001 — surfaced in the assertion below
            errors.append(f"{type(e).__name__}: {e}")

    monkeypatch.setattr(StrategyStore, "_insert_version", _signalling)
    t = threading.Thread(target=_concurrent_edit)
    t.start()
    version = store.promote_draft(sid)
    t.join(timeout=10)
    monkeypatch.undo()

    assert not errors, errors
    assert version == base.version + 1

    # The promoted value is saved; the concurrent edit is either still the draft
    # (it landed after the commit) or it was what got promoted. What must never
    # happen is both writes resolving to neither value.
    saved = store.load(sid).risk.take_profit_r.value
    draft = store.load_draft(sid)
    surviving = {saved} | ({draft.risk.take_profit_r.value} if draft else set())
    assert surviving <= {4.4, 9.9} and surviving, surviving
    assert 4.4 in surviving, f"the promoted draft's value was lost ({surviving})"
    # With the interleaving pinned, the end state is exact: 4.4 was promoted
    # and the mid-promote 9.9 write survived as the next draft.
    assert saved == 4.4
    assert draft is not None and draft.risk.take_profit_r.value == 9.9


def test_promote_without_a_draft_still_raises(store):
    with pytest.raises(KeyError):
        store.promote_draft(build_engine_fixture().id)


def test_failed_promote_leaves_the_draft_in_place(store, monkeypatch):
    """A crash mid-promote must roll back, not consume the draft."""
    base = store.load(build_engine_fixture().id)
    store.save_draft(_tweaked(base, 4.4))

    def _boom(self, con, defn):
        raise RuntimeError("insert exploded")

    monkeypatch.setattr(StrategyStore, "_insert_version", _boom)
    with pytest.raises(RuntimeError, match="insert exploded"):
        store.promote_draft(base.id)

    monkeypatch.undo()
    draft = store.load_draft(base.id)
    assert draft is not None, "draft was consumed by a failed promote"
    assert draft.risk.take_profit_r.value == 4.4
    assert store.load(base.id).version == base.version, "a version leaked through"
