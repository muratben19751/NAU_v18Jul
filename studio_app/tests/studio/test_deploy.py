import json

import pytest
from fastapi.testclient import TestClient

SID = "wt-funding-v3"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import main
    from app.studio.store import StrategyStore
    from scripts.seed_studio import build_fixture
    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(main.app)
    c.store = store
    return c


def _run_backtest(client):
    client.post(f"/studio/{SID}/backtest")
    from app.studio.backtest import BacktestMetrics
    return BacktestMetrics.from_json(client.store.latest_run(SID)["metrics"])


def test_modal_renders_with_gate_state(client):
    _run_backtest(client)
    r = client.get(f"/studio/{SID}/deploy/modal")
    assert r.status_code == 200
    assert "Deployment gate" in r.text and "Kill switch" in r.text
    assert "v1 (saved)" in r.text


def test_modal_warns_about_draft(client):
    client.post(f"/studio/{SID}/blocks/entry/rules", data={"indicator": "rsi"})
    r = client.get(f"/studio/{SID}/deploy/modal")
    assert "not</b> included" in r.text


def test_gate_blocks_without_run(client):
    r = client.post(f"/studio/{SID}/deploy",
                    data={"environment": "paper", "gate_enabled": "true",
                          "gate_min": "0.8"})
    assert r.status_code == 422 and "no completed walk-forward run" in r.text


def test_gate_blocks_low_dsr(client):
    m = _run_backtest(client)
    r = client.post(f"/studio/{SID}/deploy",
                    data={"environment": "paper", "gate_enabled": "true",
                          "gate_min": str(m.dsr + 0.05)})
    assert r.status_code == 422 and "below required" in r.text
    assert client.store.latest_deployment(SID) is None


def test_paper_deploy_creates_record_and_artifact(client):
    m = _run_backtest(client)
    r = client.post(f"/studio/{SID}/deploy",
                    data={"environment": "paper", "gate_enabled": "true",
                          "gate_min": str(max(0.0, m.dsr - 0.05)),
                          "capital": "25000", "kill_switch": "3"})
    assert r.status_code == 200 and "pending runner pickup" in r.text
    dep = client.store.latest_deployment(SID)
    # stub runner picked it up in the background task after the response
    assert dep["environment"] == "paper" and dep["status"] == "running"
    art = json.loads(dep["config"])
    assert art["capital"] == 25000
    assert art["kill_switch_daily_pct"] == 3.0
    assert art["version"] == 1 and art["instruments"]


def test_deploy_ignores_draft_uses_saved(client):
    _run_backtest(client)
    client.patch(f"/studio/{SID}/risk",
                 data={"name": "take_profit_r", "value": "3.3"})
    client.post(f"/studio/{SID}/deploy",
                data={"environment": "paper", "kill_switch": "off"})
    art = json.loads(client.store.latest_deployment(SID)["config"])
    assert art["risk"]["take_profit_r"] == 1.8    # saved v1 value, not 3.3
    assert art["kill_switch_daily_pct"] is None


def test_live_requires_name_confirmation(client):
    _run_backtest(client)
    r = client.post(f"/studio/{SID}/deploy",
                    data={"environment": "live",
                          "confirm_name": "wrong name"})
    assert r.status_code == 422 and "exact strategy name" in r.text
    r2 = client.post(f"/studio/{SID}/deploy",
                     data={"environment": "live",
                           "confirm_name": "WT-Funding Confluence v3"})
    assert r2.status_code == 200
    assert client.store.latest_deployment(SID)["environment"] == "live"
