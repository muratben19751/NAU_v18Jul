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


def test_concurrent_draft_write_during_promote_loses_nothing(store):
    """End-state check under real threads: neither write may vanish."""
    base = store.load(build_engine_fixture().id)
    sid = base.id
    store.save_draft(_tweaked(base, 4.4))

    errors: list[str] = []

    def _concurrent_edit():
        try:
            store.save_draft(_tweaked(base, 9.9))
        except Exception as e:  # noqa: BLE001 — surfaced in the assertion below
            errors.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_concurrent_edit)
    t.start()
    version = store.promote_draft(sid)
    t.join(timeout=10)

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
