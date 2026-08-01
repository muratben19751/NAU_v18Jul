"""AI trial baselines must come from the same engine as the trial.

With the two engine switches set differently (STUDIO_BACKTEST=nautilus but
STUDIO_BACKTEST_OPT unset, say), reading the baseline from the recorded run
would pit a stub trial against Nautilus numbers — the guardrail would then be
deciding on the engine gap, not on the suggestion. The deploy gate is the
deliberate exception: it judges the run the user actually triggered.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from strategy_studio.backtest import BacktestMetrics

SID = "wt-funding-v3"


def _metrics(tag: float) -> BacktestMetrics:
    """Distinctive metrics so the source of a baseline is unambiguous."""
    return BacktestMetrics(
        net_pnl_pct=tag,
        sharpe=tag,
        dsr=0.9,
        max_dd_pct=-tag,
        trades=500,
        win_rate_pct=55.0,
        profit_factor=2.0,
        equity_curve=[1.0, 1.0 + tag / 100],
        folds=[],
    )


RECORDED = 11.0  # what the stored run says
TRIAL_ENGINE = 77.0  # what TRIAL_ADAPTER measures


class _TrialEngine:
    def run(self, compiled):
        return _metrics(TRIAL_ENGINE)


def _sugg():
    return json.dumps(
        {
            "kind": "modify_risk",
            "block": "risk",
            "diff": {"name": "take_profit_r", "value": 2.2},
            "rationale": "tighten TP",
            "expected": {"dsr_delta": 0.05},
        }
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.ai import MockLLMClient
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "LLM", MockLLMClient([_sugg()]))
    monkeypatch.setattr(main, "TRIAL_ADAPTER", _TrialEngine())
    main._TRIAL_BASELINE_CACHE.clear()

    c = TestClient(_host)
    c.store = store
    c.main = main
    return c


def _record_run(client):
    """Put a completed run in the store, as the Run button would."""
    client.store.create_run("run-1", SID, version=1, is_draft=False)
    client.store.finish_run("run-1", _metrics(RECORDED).to_json())


def test_trial_baseline_comes_from_the_trial_engine_not_the_stored_run(client):
    _record_run(client)

    client.post(f"/studio/{SID}/ai/suggest", data={})

    row = client.store.pending_suggestions(SID)[0]
    baseline = BacktestMetrics.from_json(row["baseline"])
    assert baseline.sharpe == TRIAL_ENGINE, (
        "baseline was read from the recorded run instead of measured on TRIAL_ADAPTER"
    )
    assert baseline.sharpe != RECORDED


def test_no_recorded_run_still_means_no_baseline(client):
    """Guardrails stay off until the strategy has actually been backtested."""
    client.post(f"/studio/{SID}/ai/suggest", data={})

    row = client.store.pending_suggestions(SID)[0]
    assert row["baseline"] is None


def test_baseline_is_cached_per_engine_and_definition(client):
    _record_run(client)

    calls = []
    original = _TrialEngine.run

    def _counting(self, compiled):
        calls.append(1)
        return original(self, compiled)

    client.main.TRIAL_ADAPTER.__class__.run = _counting
    try:
        client.main._trial_baseline(client.store.load(SID))
        client.main._trial_baseline(client.store.load(SID))
    finally:
        client.main.TRIAL_ADAPTER.__class__.run = original

    assert len(calls) == 1, "baseline recomputed instead of served from cache"


def test_deploy_gate_still_reads_the_recorded_run(client):
    """The gate judges a real run the user triggered, not a trial measurement."""
    _record_run(client)

    metrics = client.main._baseline_metrics(SID)

    assert metrics.sharpe == RECORDED
    assert metrics.sharpe != TRIAL_ENGINE
