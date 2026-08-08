"""/backtest/run worker: log/snapshot write failures must be logged, not
silently swallowed (DeepR 2026-08-08 [DÜŞÜK]).

Both `except Exception: pass` blocks around `_log_backtest`/
`_save_result_snapshot` were bare — a persistently failing write (disk
full, permission error) had no observable symptom beyond /sessions and the
tear sheet quietly staying empty forever. `run_backtest_guarded` is
monkeypatched at its source module (`sandbox`) so no real engine/subprocess
runs — the worker's own `from sandbox import run_backtest_guarded` is a
fresh local rebind on every call, so patching `sandbox.run_backtest_guarded`
is what actually takes effect inside the background thread. Bars must be a
real (small) non-empty DataFrame — an empty one short-circuits the worker
via `_set_error()` before it ever reaches `run_backtest_guarded`.
"""

from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import data
import web.routes.backtest as bt


@pytest.fixture
def stub_catalog(monkeypatch):
    spec = SimpleNamespace(id="spec-1", name="Stub Strategy", trade_size=0.01)
    monkeypatch.setattr(bt, "load_catalog", lambda: [spec])
    return spec


@pytest.fixture(autouse=True)
def _stub_bars(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [10.0] * 5,
        },
        index=idx,
    )
    monkeypatch.setattr(data, "load_bybit_bars", lambda **k: bars)


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _run_and_wait(client, sid: str, **form) -> str:
    client.cookies.set("nautlab_sid", sid)
    r = client.post(
        "/backtest/run",
        data={"spec_id": "spec-1", "symbol": "BTCUSDT", "interval": "1", **form},
    )
    assert r.status_code == 200
    m = re.search(r"/backtest/progress/([0-9a-f]+)", r.text)
    assert m, "progress fragment should reference the run id"
    run_id = m.group(1)
    for _ in range(100):
        raw = bt._RUN_STORE.get(run_id)
        if raw is None or raw.get("done"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker did not finish in time")
    return run_id


def test_log_write_failure_is_logged_not_silent(stub_catalog, monkeypatch, caplog):
    monkeypatch.setattr(
        "sandbox.run_backtest_guarded",
        lambda *a, **k: SimpleNamespace(error="synthetic failure"),
    )

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bt, "_log_backtest", _boom)

    with caplog.at_level(logging.WARNING, logger="web.routes.backtest"):
        run_id = _run_and_wait(_client(), "sid-log-failure")

    assert any(
        "_log_backtest failed" in r.message and run_id in r.message
        for r in caplog.records
    )


def test_snapshot_write_failure_is_logged_not_silent(stub_catalog, monkeypatch, caplog):
    monkeypatch.setattr(
        "sandbox.run_backtest_guarded",
        lambda *a, **k: SimpleNamespace(error=None),
    )
    monkeypatch.setattr(bt, "_log_backtest", lambda *a, **k: "2026-08-08T00:00:00")
    monkeypatch.setattr(bt, "_result_viewmodel", lambda *a, **k: {})

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bt, "_save_result_snapshot", _boom)

    with caplog.at_level(logging.WARNING, logger="web.routes.backtest"):
        run_id = _run_and_wait(_client(), "sid-snapshot-failure")

    assert any(
        "_save_result_snapshot failed" in r.message and run_id in r.message
        for r in caplog.records
    )


def test_a_successful_run_logs_nothing_at_warning_level(
    stub_catalog, monkeypatch, caplog
):
    """Sanity check: the new logging must not fire on the happy path."""
    monkeypatch.setattr(
        "sandbox.run_backtest_guarded",
        lambda *a, **k: SimpleNamespace(error=None),
    )
    monkeypatch.setattr(bt, "_log_backtest", lambda *a, **k: "2026-08-08T00:00:00")
    monkeypatch.setattr(bt, "_result_viewmodel", lambda *a, **k: {})
    monkeypatch.setattr(bt, "_save_result_snapshot", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="web.routes.backtest"):
        _run_and_wait(_client(), "sid-happy-path")

    assert not any(
        "_log_backtest failed" in r.message
        or "_save_result_snapshot failed" in r.message
        for r in caplog.records
    )
