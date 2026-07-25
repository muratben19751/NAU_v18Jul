from app.studio.store import StrategyStore
from scripts.seed_studio import build_fixture


def test_versioning_append_only(tmp_path):
    store = StrategyStore(tmp_path / "t.db")
    d = build_fixture()
    assert store.save(d) == 1
    assert store.save(d) == 2
    latest = store.load(d.id)
    assert latest.version == 2 and latest.parent_version == 1
    v1 = store.load(d.id, version=1)
    assert v1.version == 1 and v1.parent_version is None
    hist = store.history(d.id)
    assert [h["version"] for h in hist] == [2, 1]


def test_load_missing_raises(tmp_path):
    store = StrategyStore(tmp_path / "t.db")
    import pytest
    with pytest.raises(KeyError):
        store.load("nope")
