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


# ── 1 · regime substrategy ───────────────────────────────────────

def test_else_toggle_seeds_compilable_substrategy(client):
    from app.studio.compiler import compile_strategy
    r = client.patch(f"/studio/{SID}/blocks/regime",
                     data={"else_mode": "substrategy"})
    assert r.status_code == 200
    assert "SUB · ENTRY" in r.text and "Mean-revert in chop" in r.text
    d = client.store.load_draft(SID)
    assert d.regime.else_ == "substrategy"
    c = compile_strategy(d)
    sub = c.regime["else_strategy"]
    assert sub["entry"].conditions[0].indicator == "rsi"
    assert sub["exit"].conditions[0].operator == "gt"


def test_else_toggle_back_preserves_substrategy(client):
    client.patch(f"/studio/{SID}/blocks/regime",
                 data={"else_mode": "substrategy"})
    client.patch(f"/studio/{SID}/blocks/regime", data={"else_mode": "flat"})
    d = client.store.load_draft(SID)
    assert d.regime.else_ == "flat"
    assert d.regime.substrategy is not None      # kept for re-enable
    from app.studio.compiler import compile_strategy
    assert "else_strategy" not in compile_strategy(d).regime


def test_sub_rules_editable_via_same_endpoints(client):
    client.patch(f"/studio/{SID}/blocks/regime",
                 data={"else_mode": "substrategy"})
    r = client.post(f"/studio/{SID}/blocks/sub_entry/rules",
                    data={"indicator": "stochrsi"})
    assert r.status_code == 200
    d = client.store.load_draft(SID)
    assert d.regime.substrategy.entry.rules[-1].indicator == "stochrsi"
    rid = d.regime.substrategy.entry.rules[0].id
    r2 = client.patch(f"/studio/{SID}/rules/{rid}",
                      data={"param": "len", "value": "21"})
    assert r2.status_code == 200
    d2 = client.store.load_draft(SID)
    assert d2.regime.substrategy.entry.rules[0].params["len"].value == 21


def test_substrategy_empty_entry_blocks_backtest(client):
    client.patch(f"/studio/{SID}/blocks/regime",
                 data={"else_mode": "substrategy"})
    d = client.store.load_draft(SID)
    d.regime.substrategy.entry.rules.clear()
    client.store.save_draft(d)
    r = client.post(f"/studio/{SID}/backtest")
    assert r.status_code == 422
    assert "substrategy entry block" in r.text


def test_sub_params_join_the_sweep(client):
    client.patch(f"/studio/{SID}/blocks/regime",
                 data={"else_mode": "substrategy"})
    d = client.store.load_draft(SID)
    rid = d.regime.substrategy.entry.rules[0].id
    r = client.patch(f"/studio/{SID}/opt/toggle",
                     data={"owner": rid, "param": "len"})
    assert r.status_code == 200
    d2 = client.store.load_draft(SID)
    assert any(b == "sub_entry" and n == "len"
               for b, _r, n, _p in d2.optimized_params())


# ── 2 · allocation block ─────────────────────────────────────────

def test_allocation_ranked_flow(client):
    for name, value in [("mode", "ranked"), ("sort_indicator", "adx"),
                        ("top_n", "2"), ("weighting", "inverse_volatility"),
                        ("rebalance", "weekly")]:
        r = client.patch(f"/studio/{SID}/allocation",
                         data={"name": name, "value": value})
        assert r.status_code == 200, name
    d = client.store.load_draft(SID)
    a = d.allocation
    assert (a.mode, a.sort_indicator, a.top_n, a.weighting, a.rebalance) == \
        ("ranked", "adx", 2, "inverse_volatility", "weekly")


def test_allocation_validation(client):
    r = client.patch(f"/studio/{SID}/allocation",
                     data={"name": "sort_indicator", "value": "hokus"})
    assert r.status_code == 422
    r2 = client.patch(f"/studio/{SID}/allocation",
                      data={"name": "top_n", "value": "0"})
    assert r2.status_code == 422


def test_allocation_top_n_vs_active_instruments(client):
    client.patch(f"/studio/{SID}/allocation", data={"name": "mode", "value": "ranked"})
    client.patch(f"/studio/{SID}/allocation", data={"name": "top_n", "value": "3"})
    # fixture has only XAUUSD active -> compile must reject top_n=3
    r = client.post(f"/studio/{SID}/backtest")
    assert r.status_code == 422 and "top_n" in r.text


def test_allocation_lands_in_deploy_artifact(client):
    client.patch(f"/studio/{SID}/allocation", data={"name": "mode", "value": "ranked"})
    client.patch(f"/studio/{SID}/allocation", data={"name": "top_n", "value": "1"})
    client.post(f"/studio/{SID}/save")
    client.post(f"/studio/{SID}/backtest")
    client.post(f"/studio/{SID}/deploy",
                data={"environment": "paper", "kill_switch": "off"})
    art = json.loads(client.store.latest_deployment(SID)["config"])
    assert art["allocation"]["mode"] == "ranked"
    assert art["allocation"]["top_n"] == 1


# ── 4 · deployment lifecycle ─────────────────────────────────────

def _deploy(client):
    client.post(f"/studio/{SID}/backtest")
    client.post(f"/studio/{SID}/deploy",
                data={"environment": "paper", "kill_switch": "off"})
    return client.store.latest_deployment(SID)


def test_stub_runner_picks_up(client):
    dep = _deploy(client)   # bg task ran after response in TestClient
    assert dep["status"] == "running"


def test_pause_resume_stop_transitions(client):
    dep = _deploy(client)
    did = dep["deploy_id"]
    r = client.post(f"/studio/{SID}/deployments/{did}/pause")
    assert r.status_code == 200
    assert client.store.get_deployment(did)["status"] == "paused"
    assert "▶ Resume" in r.text
    client.post(f"/studio/{SID}/deployments/{did}/resume")
    assert client.store.get_deployment(did)["status"] == "running"
    client.post(f"/studio/{SID}/deployments/{did}/stop")
    assert client.store.get_deployment(did)["status"] == "stopped"


def test_invalid_transitions_422(client):
    dep = _deploy(client)
    did = dep["deploy_id"]
    client.post(f"/studio/{SID}/deployments/{did}/stop")
    for action in ("pause", "resume", "stop"):
        r = client.post(f"/studio/{SID}/deployments/{did}/{action}")
        assert r.status_code == 422, action
    r2 = client.post(f"/studio/{SID}/deployments/{did}/launch")
    assert r2.status_code == 422 and "unknown action" in r2.text


def test_deployments_panel_lists_and_polls_pending(client):
    _deploy(client)
    r = client.get(f"/studio/{SID}/deployments/panel")
    assert "PAPER" in r.text and "RUNNING" in r.text
    # a hand-inserted pending row makes the panel poll
    client.store.create_deployment("pend01", SID, 1, "paper", "{}")
    r2 = client.get(f"/studio/{SID}/deployments/panel")
    assert 'hx-trigger="every 2s"' in r2.text and "PENDING" in r2.text
