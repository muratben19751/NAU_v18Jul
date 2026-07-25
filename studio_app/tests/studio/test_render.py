import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import main
    from app.studio.store import StrategyStore
    store = StrategyStore(tmp_path / "t.db")
    from scripts.seed_studio import build_fixture
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    return TestClient(main.app)


def test_page_renders(client):
    r = client.get("/studio/wt-funding-v3")
    assert r.status_code == 200
    html = r.text
    for needle in ["WT-Funding Confluence v3", "IF · REGIME", "ENTRY · LONG",
                   "WaveTrend", "Funding z-score", "skip if",
                   "Walk-forward validation", "combinations"]:
        assert needle in html
    assert html.count("optd") >= 5  # amber chips for optimized params


def test_404(client):
    assert client.get("/studio/nope").status_code == 404


def test_history_endpoint(client):
    r = client.get("/studio/wt-funding-v3/history")
    assert r.status_code == 200 and r.json()[0]["version"] == 1
