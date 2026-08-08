"""A transient registry read failure must not delete anything.

`_read_registry` used to quarantine registry.json on ANY exception, "recovering"
from a corrupt file by renaming it away. On Windows a concurrent atomic replace
from `save_custom` can deny a reader for a moment (sharing violation), and that
was enough to rename the whole registry to .bak: every custom block lost its
registration, every strategy using one then looked invalid to `load_catalog`,
and the pruned catalog was written back to disk — permanent strategy loss from
one momentarily locked file.

Two guarantees are pinned here:
  1. an I/O failure raises (RegistryUnavailable) instead of reporting "empty";
  2. a registry that cannot be read leaves catalog.json exactly as it was.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import custom_block_store as cbs

    monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
    cbs.save_custom("blk_one", {"label": "One", "params": {}}, "def evaluate(): ...")
    return cbs


def test_io_error_raises_instead_of_reporting_an_empty_registry(store, monkeypatch):
    def _denied(*a, **k):
        raise PermissionError("[WinError 32] file in use by another process")

    monkeypatch.setattr(type(store.REGISTRY_FILE), "read_text", _denied)
    monkeypatch.setattr(store, "_READ_RETRY_SLEEP", 0.0)

    with pytest.raises(store.RegistryUnavailable):
        store.list_custom()


def test_io_error_does_not_quarantine_the_registry(store):
    before = store.REGISTRY_FILE.read_text(encoding="utf-8")

    def _denied(*a, **k):
        raise OSError("transient")

    # Scoped so the failure is undone WITHOUT undoing the fixture's redirection
    # of STORE_DIR/REGISTRY_FILE into tmp_path.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(store.REGISTRY_FILE), "read_text", _denied)
        mp.setattr(store, "_READ_RETRY_SLEEP", 0.0)
        with pytest.raises(store.RegistryUnavailable):
            store.list_custom()

    assert store.REGISTRY_FILE.exists(), "registry was renamed away on an I/O error"
    assert store.REGISTRY_FILE.read_text(encoding="utf-8") == before
    assert [b["name"] for b in store.list_custom()] == ["blk_one"]


def test_genuinely_corrupt_json_is_still_quarantined(store):
    store.REGISTRY_FILE.write_text("{not json at all", encoding="utf-8")

    with pytest.raises(store.RegistryUnavailable, match="refusing empty-registry"):
        store.list_custom()
    assert store.REGISTRY_FILE.with_suffix(".json.bak").exists()
    # The next request sees the quarantine marker too; it must not turn into a
    # seemingly valid empty store after the first caller moved the bad file.
    with pytest.raises(store.RegistryUnavailable, match="quarantined"):
        store.list_custom()


def test_unreadable_registry_leaves_the_catalog_untouched(tmp_path, monkeypatch):
    """The data-loss path: unknown custom blocks must not prune the catalog."""
    import composer

    catalog_file = tmp_path / "catalog.json"
    spec = {
        "id": "s1",
        "name": "uses a custom block",
        "description": "",
        "blocks": [{"type": "blk_one", "role": "entry", "params": {}}],
        "trade_size": 0.1,
    }
    catalog_file.write_text(json.dumps([spec]), encoding="utf-8")
    monkeypatch.setattr(composer, "CATALOG_FILE", catalog_file)
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)

    def _unreadable(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(composer, "load_catalog", composer.load_catalog)
    import custom_block_store as cbs

    monkeypatch.setattr(cbs, "list_custom", _unreadable)

    catalog = composer.load_catalog()

    assert json.loads(catalog_file.read_text(encoding="utf-8")) == [spec], (
        "catalog.json was rewritten while the custom block registry was unreadable"
    )
    assert [s.id for s in catalog] == ["s1"], "strategy was pruned, not preserved"


def test_corrupt_registry_leaves_the_catalog_untouched(store, tmp_path, monkeypatch):
    """A malformed registry is unavailable, never an empty custom-name set."""
    import composer

    catalog_file = tmp_path / "catalog.json"
    spec = {
        "id": "s-corrupt",
        "name": "uses a custom block",
        "description": "",
        "blocks": [{"type": "blk_one", "role": "entry", "params": {}}],
        "trade_size": 0.1,
    }
    catalog_file.write_text(json.dumps([spec]), encoding="utf-8")
    monkeypatch.setattr(composer, "CATALOG_FILE", catalog_file)
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)
    store.REGISTRY_FILE.write_text("{broken", encoding="utf-8")

    catalog = composer.load_catalog()

    assert json.loads(catalog_file.read_text(encoding="utf-8")) == [spec]
    assert [s.id for s in catalog] == ["s-corrupt"]


# ---------------------------------------------------------------------------
# _read_registry() mtime/size cache (DeepR 2026-08-08 [ORTA])
# ---------------------------------------------------------------------------


def test_registry_is_not_reread_when_the_file_is_unchanged(store, monkeypatch):
    calls = {"n": 0}
    real_read_text = type(store.REGISTRY_FILE).read_text

    def _counting_read_text(self, *a, **k):
        calls["n"] += 1
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(store.REGISTRY_FILE), "read_text", _counting_read_text)

    store.list_custom()
    store.list_custom()
    store.list_custom()

    assert calls["n"] == 1, "unchanged registry.json was re-read from disk"


def test_registry_cache_invalidates_on_save(store):
    before = [b["name"] for b in store.list_custom()]
    assert before == ["blk_one"]

    store.save_custom("blk_two", {"label": "Two", "params": {}}, "def evaluate(): ...")

    after = sorted(b["name"] for b in store.list_custom())
    assert after == ["blk_one", "blk_two"], "cache served stale content after a save"


def test_registry_cache_does_not_leak_across_different_registry_files(
    tmp_path, monkeypatch
):
    """Two stores pointed at DIFFERENT files must never see each other's
    cached content, even if their (mtime_ns, size) happened to collide."""
    import custom_block_store as cbs

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.setattr(cbs, "STORE_DIR", dir_a)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", dir_a / "registry.json")
    cbs.save_custom("only_in_a", {"label": "A", "params": {}}, "def evaluate(): ...")
    names_a = [b["name"] for b in cbs.list_custom()]

    monkeypatch.setattr(cbs, "STORE_DIR", dir_b)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", dir_b / "registry.json")
    cbs.save_custom("only_in_b", {"label": "B", "params": {}}, "def evaluate(): ...")
    names_b = [b["name"] for b in cbs.list_custom()]

    assert names_a == ["only_in_a"]
    assert names_b == ["only_in_b"]
