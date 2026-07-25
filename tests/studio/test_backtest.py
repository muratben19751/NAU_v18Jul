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


def test_stub_adapter_deterministic_and_param_sensitive():
    from scripts.seed_studio import build_fixture
    from strategy_studio.backtest import StubBacktestAdapter
    from strategy_studio.compiler import compile_strategy
    a = StubBacktestAdapter()
    d = build_fixture()
    m1, m2 = a.run(compile_strategy(d)), a.run(compile_strategy(d))
    assert m1.to_json() == m2.to_json()          # deterministic
    d.risk.take_profit_r.value = 2.4
    m3 = a.run(compile_strategy(d))
    assert m3.to_json() != m1.to_json()          # param-sensitive
    assert len(m1.equity_curve) == a.N_BARS
    assert len(m1.folds) == d.walkforward.folds


def test_backtest_end_to_end(client):
    r = client.post(f"/studio/{SID}/backtest")
    assert r.status_code == 200
    # TestClient runs background tasks after the response, so the run is done
    run = client.store.latest_run(SID)
    assert run["status"] == "done" and run["metrics"]
    foot = client.get(f"/studio/{SID}/runs/latest")
    assert "Net P&L" in foot.text and "Deflated SR" in foot.text
    assert 'class="sparkline"' in foot.text
    page = client.get(f"/studio/{SID}")
    assert "Net P&L" in page.text                # initial run shown on reload


def test_backtest_uses_draft(client):
    client.post(f"/studio/{SID}/blocks/entry/rules", data={"indicator": "rsi"})
    client.post(f"/studio/{SID}/backtest")
    run = client.store.latest_run(SID)
    assert run["is_draft"] == 1


def test_compile_error_surfaces_with_rule_id(client):
    d, _ = client.store.working_copy(SID)
    d.entry.rules[0].indicator = "ghost_indicator"
    client.store.save_draft(d)
    r = client.post(f"/studio/{SID}/backtest")
    assert r.status_code == 422
    assert "unknown indicator" in r.text
    assert r.headers.get("x-rule-id") == d.entry.rules[0].id
    assert client.store.latest_run(SID) is None   # no run row created


def test_engine_failure_marks_run_failed(client, monkeypatch):
    from web.routes import strategy_studio as main

    class Boom:
        def run(self, compiled):
            raise RuntimeError("engine exploded")

    monkeypatch.setattr(main, "ADAPTER", Boom())
    client.post(f"/studio/{SID}/backtest")
    run = client.store.latest_run(SID)
    assert run["status"] == "failed" and "engine exploded" in run["error"]
    foot = client.get(f"/studio/{SID}/runs/latest")
    assert "Failed" in foot.text and "engine exploded" in foot.text


def test_folds_pane(client):
    client.post(f"/studio/{SID}/backtest")
    r = client.get(f"/studio/{SID}/runs/latest/folds")
    assert r.status_code == 200
    assert "OOS folds" in r.text and "Fold 6" in r.text


def test_folds_pane_no_runs(client):
    r = client.get(f"/studio/{SID}/runs/latest/folds")
    assert "No completed run" in r.text


def test_spark_path_downsamples():
    from web.routes.strategy_studio import _spark_path
    path = _spark_path([1.0 + i / 1000 for i in range(5000)])
    assert path.startswith("M") and path.count("L") <= 260
