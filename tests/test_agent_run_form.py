"""POST /agent/run form plumbing: TF multi-select (tfs) + date window.

The worker is faked (captured kwargs, no LLM/backtest), so what is under test
is exactly the route's parsing: chip selections become the intervals list,
bogus codes are dropped, dates are validated before a worker ever starts.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient


def _client_and_capture(monkeypatch):
    import web.routes.agent_backtest as ab
    from server import app

    got: dict = {}
    done = threading.Event()

    def fake_worker(**kw):
        got.update(kw)
        done.set()

    monkeypatch.setattr(ab, "_agent_worker", fake_worker)
    return TestClient(app), got, done


def test_tfs_multiselect_becomes_intervals(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run",
        data={
            "tfs": ["15", "60"],
            "range_start": "2024-01-01",
            "range_end": "2024-06-30",
            "n_iterations": 2,
        },
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["intervals"] == ["15", "60"]
    assert got["range_start"] == "2024-01-01"
    assert got["range_end"] == "2024-06-30"


def test_bogus_tf_codes_fall_back_to_single_interval(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run", data={"tfs": ["banana"], "interval": "240", "n_iterations": 2}
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["intervals"] == ["240"]


def test_dotted_symbol_promotes_to_external_with_mapped_tfs(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    r = client.post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["60", "D"], "n_iterations": 2},
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert got["source"] == "external"
    assert got["instrument_id"] == "QQQ.NASDAQ"
    assert got["intervals"] == ["1-HOUR", "1-DAY"]  # bybit codes → granularities


def test_model_pick_reaches_worker_and_thread_pin(monkeypatch):
    import agent

    client, got, done = _client_and_capture(monkeypatch)
    r = client.post("/agent/run", data={"model": "claude-haiku-4-5", "n_iterations": 2})
    assert r.status_code == 200
    assert done.wait(5)
    assert got["model"] == "claude-haiku-4-5"
    # Thread pin: known model pins THIS thread only; unknown clears.
    agent.set_thread_model("claude-haiku-4-5")
    assert agent.current_model() == "claude-haiku-4-5"
    agent.set_thread_model("uydurma-model")
    assert agent.current_model() == agent.MODEL
    agent.set_thread_model(None)


def test_sealed_holdout_stats_counts_only_in_window_entries():
    import web.routes.agent_backtest as ab

    start = 1_000_000
    trades = [
        {"entry_time": start - 50, "pnl": 500.0},  # lead-in (warmup) — sayılmaz
        {"entry_time": start + 10, "pnl": 100.0},
        {"entry_time": start + 20, "pnl": -50.0},
        {"entry_time": start + 30, "pnl": 150.0},
    ]
    n, pnl_fr, sharpe = ab._sealed_holdout_stats(trades, start)
    assert n == 3
    assert pnl_fr == (100.0 - 50.0 + 150.0) / 10_000.0
    assert sharpe is not None and sharpe > 0
    # 0 işlem → ölçüm yok: n=0, sharpe None (uyarı yolunu tetikler).
    n0, pnl0, sh0 = ab._sealed_holdout_stats(
        [{"entry_time": start - 1, "pnl": 9.9}], start
    )
    assert (n0, pnl0, sh0) == (0, 0.0, None)
    assert ab._sealed_holdout_stats(None, start) == (0, 0.0, None)


def test_llm_cost_usd_is_notional_not_none(monkeypatch):
    import agent
    import web.routes.agent_backtest as ab

    agent.set_thread_model(None)
    model, cost = ab._llm_cost_usd(1_000_000, 0, 0, 0)
    assert model == agent.MODEL
    # Fable list price $10/MTok input — notional cost artık CLI'da da dolu.
    assert cost == pytest.approx(10.0)
    # 1h-TTL cache yazımı ×2 kuralı ledger'la aynı kaynaktan gelir.
    _, wcost = ab._llm_cost_usd(0, 0, 0, 1_000_000)
    assert wcost == pytest.approx(20.0)


def test_bad_dates_rejected_before_worker(monkeypatch):
    client, got, done = _client_and_capture(monkeypatch)
    assert client.post("/agent/run", data={"range_start": "kotu"}).status_code == 400
    assert (
        client.post(
            "/agent/run",
            data={"range_start": "2025-02-02", "range_end": "2025-01-01"},
        ).status_code
        == 400
    )
    assert not done.is_set()  # no worker was started
