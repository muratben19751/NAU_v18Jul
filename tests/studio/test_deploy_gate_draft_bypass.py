"""The deploy gate must judge the definition that is actually deployed.

Deploy compiles the SAVED version, while the footer shows the newest run — and
that run may have measured an unsaved draft. Reading the newest run therefore
let anyone clear the gate with numbers from a definition that was never saved:
save a weak v1, tune the draft until the metrics pass, hit Backtest (not Save),
then Deploy. The gate passed on the draft's number and the artifact was built
from the saved definition instead.

The gate now matches runs on a content hash of the definition, which also keeps
the ordinary draft → backtest → save → deploy flow working: promoting a draft
only rewrites version bookkeeping, which the hash excludes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from strategy_studio.backtest import BacktestMetrics
from strategy_studio.store import definition_hash

SID = "rsi-adx-btc"  # the deployable seed fixture

PASSING_DSR = 0.92
FAILING_DSR = 0.10
GATE_MIN = 0.50


def _metrics(dsr: float) -> BacktestMetrics:
    return BacktestMetrics(
        net_pnl_pct=12.0,
        sharpe=1.4,
        dsr=dsr,
        max_dd_pct=-8.0,
        trades=500,
        win_rate_pct=55.0,
        profit_factor=2.0,
        equity_curve=[1.0, 1.12],
        folds=[],
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_engine_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_engine_fixture())
    monkeypatch.setattr(main, "store", store)

    c = TestClient(_host)
    c.store = store
    c.main = main
    return c


def _record_run(client, defn, dsr: float, *, is_draft: bool, run_id: str) -> None:
    """A completed run, recorded exactly as the Backtest button records one."""
    client.store.create_run(run_id, SID, defn.version, is_draft, definition_hash(defn))
    client.store.finish_run(run_id, _metrics(dsr).to_json())


def _tweaked(defn, take_profit_r: float):
    """Same strategy with one risk parameter changed — a different definition."""
    from strategy_studio.schema import Param

    return defn.model_copy(
        update={
            "risk": defn.risk.model_copy(
                update={"take_profit_r": Param(value=take_profit_r)}
            )
        }
    )


def _deploy(client, **overrides):
    data = {
        "environment": "paper",
        "instruments": "active",
        "capital": "10000",
        "kill_switch": "3",
        "gate_enabled": "true",
        "gate_min": str(GATE_MIN),
    }
    data.update(overrides)
    return client.post(f"/studio/{SID}/deploy", data=data)


def test_draft_run_cannot_clear_the_gate_for_the_saved_version(client):
    saved = client.store.load(SID)
    # The saved version was measured and it FAILS the gate.
    _record_run(client, saved, FAILING_DSR, is_draft=False, run_id="run-saved")
    # A draft tuned to a passing number, backtested but never saved. It is the
    # newest run, so the old gate read this one.
    draft = _tweaked(saved, 9.9)
    client.store.save_draft(draft)
    _record_run(client, draft, PASSING_DSR, is_draft=True, run_id="run-draft")

    r = _deploy(client)

    assert r.status_code == 422, "draft metrics cleared the gate for a saved version"
    assert "gate" in r.text.lower()
    assert not client.store.list_deployments(SID)


def test_gate_passes_on_a_run_of_the_saved_definition(client):
    saved = client.store.load(SID)
    _record_run(client, saved, PASSING_DSR, is_draft=False, run_id="run-saved")

    r = _deploy(client)

    assert r.status_code == 200, r.text
    assert len(client.store.list_deployments(SID)) == 1


def test_promoting_the_backtested_draft_keeps_its_measurement(client):
    """The normal flow: edit → backtest → save → deploy stays a single click."""
    saved = client.store.load(SID)
    draft = _tweaked(saved, 3.3)
    client.store.save_draft(draft)
    _record_run(client, draft, PASSING_DSR, is_draft=True, run_id="run-draft")
    client.store.promote_draft(SID)  # Save: same rules, new version number

    r = _deploy(client)

    assert r.status_code == 200, (
        "promoting the draft that was just measured should not require a re-run"
    )
    assert len(client.store.list_deployments(SID)) == 1


def test_gate_blocks_when_the_saved_version_was_never_run(client):
    r = _deploy(client)

    assert r.status_code == 422
    assert "no completed" in r.text.lower()


def test_non_numeric_kill_switch_is_a_422_not_a_500(client):
    saved = client.store.load(SID)
    _record_run(client, saved, PASSING_DSR, is_draft=False, run_id="run-saved")

    r = _deploy(client, kill_switch="disabled")
    assert r.status_code == 200, "'disabled' should read as 'no kill switch'"

    r = _deploy(client, kill_switch="not-a-number")
    assert r.status_code == 422
    assert "kill switch" in r.text.lower()
