"""Real end-to-end drive of `_agent_worker`'s Phase 0->1->2->3->4->5
orchestration (DeepR 2026-08-09 [YÜKSEK]).

Every existing worker test either drives one extracted helper in isolation
(test_agent_worker_helpers.py) or drives _agent_worker down an early-failure
path (test_agent_fixes.py::TestContinuousCircuitBreaker, a phase-0 fetch
error). None had ever proven the real Phase 0->1->2->3->4->5 wiring reaches
an actual winner/promotion — the sealed-holdout gate is AUTO's financial-
integrity checkpoint and had zero whole-worker coverage of its pass OR fail
path.

Only the four external I/O boundaries are mocked: load_bybit_bars,
propose_composed_strategy, run_backtest_guarded, run_robustness_guarded.
Every ranking/eligibility/robustness/holdout/promotion decision in between
runs for real, on real (if synthetic) data.

Wiki References
---------------
Bkz: [[nau_deepr_ucuncu_tur_2026_08_09]], [[webapp_module_map]]
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

import web.routes.agent_backtest as ab
from state import IterationResult

RUN_ID = "e2ewin01"  # real run_ids are 8 chars — custom_block_store enforces it
SYMBOL = "BTCUSDT"
CATEGORY = "linear"
INTERVAL = "60"


def _flat_bars(n_days: int = 90) -> pd.DataFrame:
    """Hourly bars, flat close — buy-and-hold benchmark is always ~0, so any
    positive pnl_pct trivially beats it. Spans enough real time for
    _split_holdout to find >=200 trimmed bars AND a full 60-day sealed
    window (OOS_HOLDOUT_DAYS=60 + WF_EMBARGO_DAYS=2 + >=200 hourly bars of
    training data ≈ 71 days minimum; 90 is a comfortable margin)."""
    idx = pd.date_range("2024-01-01", periods=n_days * 24, freq="1h", tz="UTC")
    price = 100.0
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": 10.0},
        index=idx,
    )


def _iteration_result(**overrides) -> IterationResult:
    base = dict(
        id=0,
        strategy="",
        params={},
        metrics={
            "n_trades": 25,
            "max_dd": -0.05,
            "sharpe_per_trade": 1.5,
            "sharpe": 1.5,
            "pnl": 500.0,
            "pnl_pct": 0.05,
        },
        equity_curve=[10_000.0, 10_500.0],
        rationale="",
        error=None,
        timestamp=datetime.now(UTC),
        equity_dates=["2024-01-01", "2024-01-02"],
        trades=[{"pnl": 20.0} for _ in range(25)],
    )
    base.update(overrides)
    return IterationResult(**base)


def _sealed_holdout_trades(hold_start: pd.Timestamp, n: int = 25) -> list[dict]:
    """n trades entered inside the sealed window, alternating pnl so the
    per-trade Sharpe has nonzero variance — identical pnls make
    _sealed_holdout_stats report sharpe=None, which the gate rejects
    outright (HOLDOUT_REQUIRE_POSITIVE_SHARPE)."""
    start_unix = int(hold_start.timestamp())
    return [
        {"entry_time": start_unix + i * 3600, "pnl": 12.0 if i % 2 == 0 else 8.0}
        for i in range(n)
    ]


def _fake_proposal() -> dict:
    return {
        "name": "E2E Momentum",
        "blocks": [{"type": "momentum", "role": "entry", "params": {}}],
    }


@pytest.fixture
def isolated_writes(tmp_path, monkeypatch):
    """A real winner promotion writes to the strategy catalog, the
    backtest/robustness logs, and the agent index DB — all real files under
    ~/.cache/nautilus_web_app outside this fixture. Redirect every one of
    them; SESSION_LOG_DIR is already isolated by the autouse conftest
    fixture (tests/conftest.py::_isolate_session_logs)."""
    import composer
    import web.shared as shared

    monkeypatch.setattr(composer, "CATALOG_FILE", tmp_path / "strategy_catalog.json")
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)
    monkeypatch.setattr(shared, "BACKTEST_LOG", tmp_path / "backtest_log.jsonl")
    monkeypatch.setattr(shared, "ROBUSTNESS_LOG", tmp_path / "robustness_log.jsonl")
    monkeypatch.setattr(ab, "_AGENT_INDEX_DB", tmp_path / "agent_index.db")
    return tmp_path


def _seed_progress(run_id: str) -> None:
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = {
            "phases": [
                {"n": i, "label": str(i), "status": "pending", "detail": "", "ts": ""}
                for i in range(6)
            ],
            "steps": [],
            "done": False,
            "error": None,
            "strategy_name": "",
            "stop_requested": False,
            "continuous_mode": False,
            "winner_result": None,
            "winner_spec_name": "",
            "winner_spec_id": "",
            "winner_rob": None,
            "winner_holdout": None,
            "rob_scan_log": [],
            "rob_scan_current": 0,
            "rob_scan_total": 0,
            "hint": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "backtest_results": [],
            "timeline": [],
        }


def _run_worker(monkeypatch, bars, fake_run_backtest_guarded) -> dict:
    monkeypatch.setattr("data.load_bybit_bars", lambda **k: bars.copy())
    monkeypatch.setattr(
        "agent.propose_composed_strategy",
        lambda *a, **k: (_fake_proposal(), {"input_tokens": 1, "output_tokens": 1}),
    )
    monkeypatch.setattr("sandbox.run_backtest_guarded", fake_run_backtest_guarded)
    monkeypatch.setattr(
        "sandbox.run_robustness_guarded",
        lambda *a, **k: {
            "split": {"overfitting_label": "robust"},
            "multi_symbol": {"generalization_label": "robust"},
        },
    )

    _seed_progress(RUN_ID)
    try:
        ab._agent_worker(
            RUN_ID,
            hint="",
            symbol=SYMBOL,
            category=CATEGORY,
            intervals=[INTERVAL],
            n_iterations=1,
            strict_mode=False,
        )
    finally:
        with ab._AGENT_LOCK:
            state = ab._AGENT_PROGRESS.pop(RUN_ID, None) or {}
    return state


def _session_events(run_id: str) -> list[dict]:
    lines = (ab.SESSION_LOG_DIR / f"{run_id}.jsonl").read_text().strip().splitlines()
    return [json.loads(line) for line in lines]


def test_real_orchestration_reaches_a_promoted_winner(monkeypatch, isolated_writes):
    bars = _flat_bars()
    hold_start = bars.index[-1] - pd.Timedelta(days=ab.OOS_HOLDOUT_DAYS)

    def fake_run_backtest_guarded(
        spec, bars_df, recipe, *, iteration_id, rationale, **kw
    ):
        if iteration_id == 999:  # _run_promotion_gate's sealed holdout run
            return _iteration_result(
                id=999,
                rationale=rationale,
                metrics={"pnl": 250.0},
                trades=_sealed_holdout_trades(hold_start),
            )
        return _iteration_result(id=iteration_id, rationale=rationale)  # Phase 2

    state = _run_worker(monkeypatch, bars, fake_run_backtest_guarded)

    assert state.get("error") is None
    assert state.get("done") is True
    assert state.get("winner_spec_name") == "E2E Momentum"
    assert state.get("winner_holdout", {}).get("measured") is True

    catalog_raw = json.loads((isolated_writes / "strategy_catalog.json").read_text())
    assert [s["name"] for s in catalog_raw] == ["E2E Momentum"]

    order = [e["event"] for e in _session_events(RUN_ID)]
    assert "winner" in order
    assert "session_end" in order
    assert order.index("winner") < order.index("session_end")


def test_a_failing_sealed_holdout_rejects_promotion_without_touching_the_catalog(
    monkeypatch, isolated_writes
):
    """Same winning candidate as above, but the sealed holdout itself comes
    back under HOLDOUT_MIN_TRADES — the catalog must stay untouched and the
    session must report a rejection, not a silent 'winner'. Proves the real
    gate can say no, not just yes."""
    bars = _flat_bars()
    hold_start = bars.index[-1] - pd.Timedelta(days=ab.OOS_HOLDOUT_DAYS)

    def fake_run_backtest_guarded(
        spec, bars_df, recipe, *, iteration_id, rationale, **kw
    ):
        if iteration_id == 999:
            return _iteration_result(
                id=999,
                rationale=rationale,
                metrics={"pnl": 250.0},
                trades=_sealed_holdout_trades(hold_start, n=5),  # < HOLDOUT_MIN_TRADES
            )
        return _iteration_result(id=iteration_id, rationale=rationale)

    state = _run_worker(monkeypatch, bars, fake_run_backtest_guarded)

    assert state.get("done") is True
    assert (
        state.get("winner_spec_name") == "E2E Momentum"
    )  # a finalist, not "no winner"
    assert state.get("winner_holdout", {}).get("measured") is False

    assert not (isolated_writes / "strategy_catalog.json").exists()

    events = _session_events(RUN_ID)
    end = next(e for e in events if e["event"] == "session_end")
    assert end["outcome"] == "promotion_rejected"
    finalist = next(e for e in events if e["event"] == "finalist_rejected")
    assert finalist["promoted"] is False
