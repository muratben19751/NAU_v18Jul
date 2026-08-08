"""Regression test: the autonomous loop must refresh market data every
iteration instead of freezing on the snapshot captured at /loop/start.

DeepR 2026-08-08 [YÜKSEK]: `_context["bars"]` (server.py) was set once in
`lifespan()` and never touched again; `/loop/start` handed that single
DataFrame straight to `run_loop`, which then backtested the exact same
stale window forever — days apart if the loop kept running, unlike every
other data path in the app (backtest/robustness/lab/chart/reports), which
all re-fetch fresh bars per request.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

import loop_runner
from state import AppState, IterationResult


def _bars(tag: str) -> pd.DataFrame:
    return pd.DataFrame({"tag": [tag]})


def test_run_loop_refetches_bars_every_catalog_iteration(monkeypatch):
    state = AppState()
    seen_bars: list[str] = []
    fetch_calls = {"n": 0}

    def fake_load_bybit_bars(symbol, interval, category):
        fetch_calls["n"] += 1
        return _bars(f"fresh-{fetch_calls['n']}")

    def fake_run_backtest_guarded(spec, bars_df, recipe, **kw):
        seen_bars.append(bars_df["tag"].iloc[0])
        if len(seen_bars) >= 2:
            state.stop_requested = True
        return IterationResult(
            id=kw["iteration_id"],
            strategy=f"composed:{spec.name}",
            params={},
            metrics={"pnl": 1.0},
            equity_curve=[],
            rationale="",
            error=None,
            timestamp=datetime.now(UTC),
        )

    monkeypatch.setattr(loop_runner, "load_bybit_bars", fake_load_bybit_bars)
    monkeypatch.setattr(
        loop_runner, "load_catalog", lambda: [SimpleNamespace(name="s1", id="s1")]
    )
    monkeypatch.setattr(loop_runner, "run_backtest_guarded", fake_run_backtest_guarded)
    monkeypatch.setattr(loop_runner, "_try_log", lambda *a, **kw: None)

    loop_runner.run_loop(state, _bars("initial-snapshot"), mode="catalog")

    # every iteration used a freshly-fetched frame, never the stale one
    # captured before run_loop started, and never the SAME fetch twice.
    assert seen_bars == ["fresh-1", "fresh-2"]
    assert fetch_calls["n"] == 2


def test_run_loop_keeps_prior_bars_on_a_failed_refresh(monkeypatch):
    """A transient fetch failure must degrade gracefully (reuse the last
    known-good frame), not crash the whole loop."""
    state = AppState()
    seen_bars: list[str] = []
    fetch_calls = {"n": 0}

    def flaky_load_bybit_bars(symbol, interval, category):
        fetch_calls["n"] += 1
        if fetch_calls["n"] == 2:
            raise ConnectionError("bybit unreachable")
        return _bars(f"fresh-{fetch_calls['n']}")

    def fake_run_backtest_guarded(spec, bars_df, recipe, **kw):
        seen_bars.append(bars_df["tag"].iloc[0])
        if len(seen_bars) >= 3:
            state.stop_requested = True
        return IterationResult(
            id=kw["iteration_id"],
            strategy=f"composed:{spec.name}",
            params={},
            metrics={"pnl": 1.0},
            equity_curve=[],
            rationale="",
            error=None,
            timestamp=datetime.now(UTC),
        )

    monkeypatch.setattr(loop_runner, "load_bybit_bars", flaky_load_bybit_bars)
    monkeypatch.setattr(
        loop_runner, "load_catalog", lambda: [SimpleNamespace(name="s1", id="s1")]
    )
    monkeypatch.setattr(loop_runner, "run_backtest_guarded", fake_run_backtest_guarded)
    monkeypatch.setattr(loop_runner, "_try_log", lambda *a, **kw: None)

    loop_runner.run_loop(state, _bars("initial-snapshot"), mode="catalog")

    # iteration 2's fetch failed → it reused iteration 1's frame, not a crash
    assert seen_bars == ["fresh-1", "fresh-1", "fresh-3"]
    assert state.iterations[1].error is None
