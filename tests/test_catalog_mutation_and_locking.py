"""Catalog file I/O — 4 specific, previously-uncovered gaps (composer.py
decomposition, Faz 2, Adım 5 — test-first, no code moves in this step).

Research before this file found "catalog I/O is undertested" too blunt a
claim: `load_catalog`/`CATALOG_FILE` already have real, unmocked-disk
coverage (`tests/test_block_registry_resilience.py`,
`tests/test_agent_worker_e2e.py`). These four gaps are the genuinely
uncovered ones:

1. `mutate_catalog` — never called by name in any test, real or mocked.
   Backs `web/routes/strategy.py`'s edit-in-place-save AND delete-strategy
   routes. Its own `None`-vs-`[]` distinction (composer.py:725-727) is a
   pinned regression: `or cat` used to silently swallow an intentional
   empty-list return and cancel the deletion.
2. `_catalog_is_valid`'s actual PRUNE branch (composer.py:590-608) — every
   existing real-disk test takes `load_catalog`'s `custom_names is None`
   early return (composer.py:675-676), which exits BEFORE the prune loop
   ever runs. Two H1338/M1342-tagged data-loss-prevention comments sit
   next to code nothing has ever exercised.
3. `save_catalog`'s atomic tmp-file-then-`os.replace` write — never called
   by name in any test; only ever exercised as an implementation detail
   nested inside one E2E flow.
4. `_CATALOG_LOCK`'s `RLock` reentrancy (composer.py:692-695: an RLock so
   `load_catalog`'s own locked auto-save can re-enter while
   `append_to_catalog` still holds the outer lock) — never exercised with
   real threads. No `pytest-timeout` plugin exists in this repo, so a
   same-thread test that risked a genuine deadlock would hang the whole
   suite with nothing to save it — run on a background thread with a
   bounded `.join(timeout=...)` instead, same pattern as
   `tests/test_data_atomic_and_lock.py`.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

import json
import threading

import pytest

import composer
import custom_block_store


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(composer, "CATALOG_FILE", tmp_path / "strategy_catalog.json")
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)
    return tmp_path / "strategy_catalog.json"


def _spec(name: str, block_type: str = "ma_cross", role: str = "entry"):
    # ma_cross's own registry validator (_validate_cross_fast_slow) rejects
    # slow<=fast; empty params default both to 0 and fail it. Only matters
    # for a real builtin type -- an unknown type is rejected earlier, before
    # validate() is ever reached, so its params are never inspected.
    params = {"fast": 10, "slow": 30} if block_type == "ma_cross" else {}
    return composer.build_spec(
        name=name,
        description="",
        blocks=[composer.SignalBlock(type=block_type, role=role, params=params)],
    )


class TestMutateCatalog:
    def test_applies_an_in_place_edit(self):
        spec_a, spec_b = _spec("a"), _spec("b")
        composer.save_catalog([spec_a, spec_b])

        def _edit(cat):
            for s in cat:
                if s.id == spec_a.id:
                    s.name = "a-edited"
            return cat

        composer.mutate_catalog(_edit)

        names = {s.id: s.name for s in composer.load_catalog()}
        assert names[spec_a.id] == "a-edited"
        assert names[spec_b.id] == "b"

    def test_can_delete_down_to_zero_strategies(self, _isolated_catalog):
        """Pins the composer.py:725-727 regression: `or cat` used to
        silently swallow an intentional [] return and cancel the deletion.
        """
        spec = _spec("only")
        composer.save_catalog([spec])

        composer.mutate_catalog(lambda cat: [s for s in cat if s.id != spec.id])

        assert composer.load_catalog() == []
        assert json.loads(_isolated_catalog.read_text(encoding="utf-8")) == []

    def test_a_none_return_is_a_no_op(self):
        spec = _spec("keep-me")
        composer.save_catalog([spec])

        composer.mutate_catalog(lambda cat: None)

        reloaded = composer.load_catalog()
        assert len(reloaded) == 1
        assert reloaded[0].name == "keep-me"


class TestCatalogIsValidPrunePath:
    def test_load_catalog_prunes_a_genuinely_unknown_block_type(
        self, _isolated_catalog, monkeypatch
    ):
        """Every existing real-disk test takes the `custom_names is None`
        early return; a readable-but-empty registry (`list_custom` returns
        `[]`, not raises) is required to actually reach the prune loop.
        """
        monkeypatch.setattr(custom_block_store, "list_custom", lambda: [])
        valid = _spec("valid")
        invalid = _spec("invalid", block_type="totally_made_up_block_xyz")
        composer.save_catalog([valid, invalid])

        result = composer.load_catalog()

        assert [s.name for s in result] == ["valid"]
        on_disk = json.loads(_isolated_catalog.read_text(encoding="utf-8"))
        assert [s["name"] for s in on_disk] == ["valid"]

    def test_preserves_a_spec_whose_custom_block_is_on_disk_but_unloaded(
        self, _isolated_catalog, monkeypatch
    ):
        """H1338: a block type absent from the live BLOCK_CATALOG but
        present in the (readable) on-disk custom registry must NOT be
        pruned -- it may simply not be loaded into this process yet.
        Pins the correct side of the branch Gap-2's sibling test proves the
        wrong side of.
        """
        monkeypatch.setattr(
            custom_block_store, "list_custom", lambda: [{"name": "my_custom_block"}]
        )
        spec = _spec("uses-custom", block_type="my_custom_block")
        composer.save_catalog([spec])

        result = composer.load_catalog()

        assert [s.name for s in result] == ["uses-custom"]
        on_disk = json.loads(_isolated_catalog.read_text(encoding="utf-8"))
        assert [s["name"] for s in on_disk] == ["uses-custom"]


class TestSaveCatalogAtomicWrite:
    def test_writes_atomically_via_tmp_file_and_replace(self, _isolated_catalog):
        spec1, spec2 = _spec("one"), _spec("two")

        composer.save_catalog([spec1, spec2])

        assert [s.name for s in composer.load_catalog()] == ["one", "two"]
        assert not _isolated_catalog.with_suffix(".json.tmp").exists()

    def test_leaves_the_original_file_untouched_if_the_write_step_raises(
        self, _isolated_catalog, monkeypatch
    ):
        _isolated_catalog.write_text('[{"sentinel": "original"}]', encoding="utf-8")

        def _denied(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(type(_isolated_catalog), "write_text", _denied)

        with pytest.raises(OSError):
            composer.save_catalog([_spec("new")])

        # The monkeypatch also blocks a plain re-read via .read_text() -- it
        # only patched write_text, so reading is still real.
        assert (
            _isolated_catalog.read_text(encoding="utf-8")
            == '[{"sentinel": "original"}]'
        )


class TestCatalogLockReentrancy:
    def test_isinstance_rlock(self):
        assert isinstance(composer._CATALOG_LOCK, type(threading.RLock()))

    def test_append_does_not_deadlock_when_it_triggers_an_auto_prune(
        self, _isolated_catalog, monkeypatch
    ):
        """Forces the real nested-acquisition path: append_to_catalog takes
        _CATALOG_LOCK, calls load_catalog(), which finds a stale invalid
        spec and re-enters _CATALOG_LOCK for its own auto-save while the
        outer scope still holds it (composer.py:686-688). If _CATALOG_LOCK
        ever regresses from RLock to a plain Lock, this thread blocks
        forever -- the bounded join is what turns that into a clean test
        failure instead of hanging the whole suite (no pytest-timeout
        plugin in this repo).
        """
        monkeypatch.setattr(custom_block_store, "list_custom", lambda: [])
        stale_invalid = _spec("stale", block_type="totally_made_up_block_xyz")
        composer.save_catalog([stale_invalid])
        new_spec = _spec("new")

        errors = []

        def _append():
            try:
                composer.append_to_catalog(new_spec)
            except Exception as e:  # noqa: BLE001 -- surfaced via the assertion below
                errors.append(e)

        # daemon=True: if _CATALOG_LOCK ever regresses and this deadlocks,
        # the bounded join below still lets the assertion fail cleanly --
        # without daemon, Python would ALSO hang waiting for this thread at
        # interpreter shutdown, turning one clean test failure into a hung
        # suite (verified via a standalone mutation with a plain Lock).
        thread = threading.Thread(target=_append, daemon=True)
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive(), (
            "append_to_catalog deadlocked -- _CATALOG_LOCK may have regressed "
            "from RLock to a plain Lock"
        )
        assert not errors, errors
        final = composer.load_catalog()
        assert [s.name for s in final] == ["new"]
