import json

import pytest
from fastapi.testclient import TestClient

# The design fixture: a regime branch and a funding_z rule, so no runner can
# execute it. Gate and modal tests still use it — those run before lowering.
SID = "wt-funding-v3"
# The one that lowers onto a runnable spec; everything that produces an
# artifact has to use it, because an artifact is a thing a runner executes.
EID = "rsi-adx-btc"
ENAME = "RSI Dip + ADX Trend (BTC)"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_engine_fixture, build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    store.save(build_engine_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(_host)
    c.store = store
    return c


def _run_backtest(client, sid=SID):
    client.post(f"/studio/{sid}/backtest")
    from strategy_studio.backtest import BacktestMetrics

    return BacktestMetrics.from_json(client.store.latest_run(sid)["metrics"])


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
    r = client.post(
        f"/studio/{SID}/deploy",
        data={"environment": "paper", "gate_enabled": "true", "gate_min": "0.8"},
    )
    assert r.status_code == 422 and "no completed walk-forward run" in r.text


def test_gate_blocks_low_dsr(client):
    m = _run_backtest(client)
    r = client.post(
        f"/studio/{SID}/deploy",
        data={
            "environment": "paper",
            "gate_enabled": "true",
            "gate_min": str(m.dsr + 0.05),
        },
    )
    assert r.status_code == 422 and "below required" in r.text
    assert client.store.latest_deployment(SID) is None


def test_paper_deploy_creates_record_and_artifact(client):
    m = _run_backtest(client, EID)
    r = client.post(
        f"/studio/{EID}/deploy",
        data={
            "environment": "paper",
            "gate_enabled": "true",
            "gate_min": str(max(0.0, m.dsr - 0.05)),
            "capital": "25000",
            "kill_switch": "3",
        },
    )
    # Onay satırı runner'ın GERÇEKTEN var olup olmadığını söyler: STUDIO_RUNNER
    # ayarlı değilken "pending runner pickup" demek, beklenen bir pickup varmış
    # gibi okunuyordu — oysa yalnız simüle edilen bir tanesi var ve hiçbir emir
    # gönderilmeyecek (DeepR 2026-08-11 [ORTA]). Test ortamında runner yok.
    assert r.status_code == 200
    assert "simulated pickup only" in r.text and "nothing will trade" in r.text
    dep = client.store.latest_deployment(EID)
    # stub runner picked it up in the background task after the response
    assert dep["environment"] == "paper" and dep["status"] == "running"
    art = json.loads(dep["config"])
    assert art["capital"] == 25000
    assert art["kill_switch_daily_pct"] == 3.0
    assert art["version"] == 1 and art["instruments"]


def test_the_artifact_is_something_a_runner_can_actually_execute(client):
    """The point of the artifact. It used to carry condition *counts* under
    `compiled` — a description of the strategy, not the strategy."""
    from composer import ComposedStrategySpec

    _run_backtest(client, EID)
    client.post(
        f"/studio/{EID}/deploy", data={"environment": "paper", "capital": "25000"}
    )
    art = json.loads(client.store.latest_deployment(EID)["config"])

    spec = ComposedStrategySpec.from_dict(art["spec"])
    assert spec.validate() is None, spec.validate()
    assert [b.type for b in spec.blocks]  # the rules survived lowering
    assert spec.trade_size_capital == 25000  # deploy capital, not default
    # The spec is instrument-free by design, so the runner needs the pairing.
    assert art["instruments"] == [{"symbol": "BTCUSDT", "timeframe": "1h"}]
    assert art["artifact_schema"] >= 2


def test_a_strategy_no_runner_can_execute_is_refused_with_its_reasons(client):
    """wt-funding-v3 cannot be lowered (regime branch, funding_z). Recording a
    deployment for it would file an artifact that fails later, on the runner,
    detached from the click that caused it."""
    _run_backtest(client)
    r = client.post(
        f"/studio/{SID}/deploy", data={"environment": "paper", "kill_switch": "off"}
    )

    assert r.status_code == 422
    assert "cannot deploy" in r.text
    assert "regime" in r.text and "funding_z" in r.text
    assert client.store.latest_deployment(SID) is None


def test_instruments_all_is_honoured_instead_of_contradicting_the_config(client):
    """The artifact used to list the ACTIVE set while `config` said "all"."""
    from strategy_studio.schema import InstrumentConfig

    defn = client.store.load(EID)
    defn.instruments.append(
        InstrumentConfig(symbol="ETHUSDT", timeframe="1h", active=False)
    )
    client.store.save(defn)
    _run_backtest(client, EID)

    for selection, expected in (
        ("active", ["BTCUSDT"]),
        ("all", ["BTCUSDT", "ETHUSDT"]),
    ):
        client.post(
            f"/studio/{EID}/deploy",
            data={"environment": "paper", "instruments": selection},
        )
        art = json.loads(client.store.latest_deployment(EID)["config"])
        assert [i["symbol"] for i in art["instruments"]] == expected, selection
        assert art["config"]["instruments"] == selection


def test_deploy_ignores_draft_uses_saved(client):
    _run_backtest(client, EID)
    client.patch(f"/studio/{EID}/risk", data={"name": "take_profit_r", "value": "3.3"})
    client.post(
        f"/studio/{EID}/deploy", data={"environment": "paper", "kill_switch": "off"}
    )
    art = json.loads(client.store.latest_deployment(EID)["config"])
    assert art["risk"]["take_profit_r"] == 1.5  # saved v1 value, not 3.3
    assert art["kill_switch_daily_pct"] is None


def test_live_requires_name_confirmation(client):
    _run_backtest(client, EID)
    r = client.post(
        f"/studio/{EID}/deploy",
        data={"environment": "live", "confirm_name": "wrong name"},
    )
    assert r.status_code == 422 and "exact strategy name" in r.text
    r2 = client.post(
        f"/studio/{EID}/deploy", data={"environment": "live", "confirm_name": ENAME}
    )
    assert r2.status_code == 200
    assert client.store.latest_deployment(EID)["environment"] == "live"


def test_the_modal_says_why_the_button_is_dead(client):
    """A user should not fill the form and submit into a 422."""
    r = client.get(f"/studio/{SID}/deploy/modal")

    assert "No runner can execute this strategy" in r.text
    assert "regime" in r.text and "funding_z" in r.text
    assert "disabled" in r.text

    ok = client.get(f"/studio/{EID}/deploy/modal")
    assert "No runner can execute" not in ok.text
