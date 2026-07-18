"""Parallel robustness executor — parity, failure isolation, orphan reaping.

The pool MUST produce byte-identical aggregates to the sequential path: the
candidate RNG never leaves the parent, and the per-window reduce follows the
same ascending-index strictly-greater rule.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import parallel_exec as PE
from backtest import comparable_metrics
from backtest_robustness import run_insample_oos_split, run_walk_forward
from composer import ComposedStrategySpec, SignalBlock
from sandbox import _build_instrument_bar_type

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _spec(**over):
    kw = dict(
        id="t",
        name="t",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 10, "slow": 30, "direction": "up"},
            ),
            SignalBlock(
                type="atr_stop", role="exit", params={"period": 14, "mult": 3.0}
            ),
        ],
    )
    kw.update(over)
    return ComposedStrategySpec(**kw)


def _bars(n: int = 4000) -> pd.DataFrame:
    """Deterministic synthetic hourly OHLCV (seeded random walk)."""
    rng = np.random.default_rng(7)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = 30_000 + np.cumsum(rng.normal(0, 30, n))
    close = np.maximum(close, 1_000.0)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 5,
            "low": np.minimum(open_, close) - 5,
            "close": close,
            "volume": np.ones(n),
        },
        index=idx,
    )


_BARS = _bars()


@pytest.fixture(scope="module")
def pool():
    """One 2-worker pool for the whole module (spawn+nautilus import is slow)."""
    snap = PE.make_snapshot(_BARS)
    p = PE.BacktestPool(snap, _RECIPE, max_workers=2)
    yield p
    p.shutdown()
    import shutil

    shutil.rmtree(Path(snap).parent, ignore_errors=True)


class TestWorkerConfig:
    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_PARALLEL", "0")
        assert PE.parallel_enabled() is False
        monkeypatch.setenv("NAUTILUS_PARALLEL", "1")
        assert PE.parallel_enabled() is True

    def test_worker_count_env_override_and_clamp(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_PARALLEL_WORKERS", "4")
        assert PE.get_worker_count() == 4
        monkeypatch.setenv("NAUTILUS_PARALLEL_WORKERS", "999")
        assert PE.get_worker_count() == PE.DEFAULT_WORKER_CLAMP[1]
        monkeypatch.setenv("NAUTILUS_PARALLEL_WORKERS", "0")
        assert PE.get_worker_count() == PE.DEFAULT_WORKER_CLAMP[0]
        monkeypatch.delenv("NAUTILUS_PARALLEL_WORKERS")
        assert PE.get_worker_count() >= 1


class TestParity:
    """Sequential vs parallel must be EXACTLY equal (same seeds, same engine)."""

    def test_walk_forward_parity(self, pool):
        spec = _spec()
        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        kw = dict(
            train_months=2,
            test_months=1,
            step_months=1,
            n_optimize=4,
        )
        seq = run_walk_forward(
            spec, _BARS, instrument, bar_type, instrument.id.venue, **kw
        )
        par = run_walk_forward(
            spec,
            _BARS,
            instrument,
            bar_type,
            instrument.id.venue,
            run_many=pool.run_units,
            **kw,
        )
        assert len(seq) == len(par) > 0, "window counts differ (or zero windows)"
        for ws, wp in zip(seq, par):
            assert ws["window"] == wp["window"]
            assert ws["chosen_params"] == wp["chosen_params"]
            assert ws["train_objective"] == wp["train_objective"]
            assert comparable_metrics(ws["train_metrics"]) == comparable_metrics(
                wp["train_metrics"]
            )
            assert comparable_metrics(ws["test_metrics"]) == comparable_metrics(
                wp["test_metrics"]
            )
            assert comparable_metrics(ws["test_metrics_naive"]) == comparable_metrics(
                wp["test_metrics_naive"]
            )
            assert ws["test_n_trades"] == wp["test_n_trades"]
            assert ws["test_equity"] == wp["test_equity"]

    def test_walk_forward_parallel_is_repeatable(self, pool):
        spec = _spec()
        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        kw = dict(train_months=2, test_months=1, step_months=1, n_optimize=3)
        a = run_walk_forward(
            spec,
            _BARS,
            instrument,
            bar_type,
            instrument.id.venue,
            run_many=pool.run_units,
            **kw,
        )
        b = run_walk_forward(
            spec,
            _BARS,
            instrument,
            bar_type,
            instrument.id.venue,
            run_many=pool.run_units,
            **kw,
        )
        assert [w["chosen_params"] for w in a] == [w["chosen_params"] for w in b]
        assert [w["train_objective"] for w in a] == [w["train_objective"] for w in b]

    def test_insample_oos_parity(self, pool):
        spec = _spec()
        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        seq = run_insample_oos_split(
            spec, _BARS, instrument, bar_type, instrument.id.venue, split_pct=0.7
        )
        par = run_insample_oos_split(
            spec,
            _BARS,
            instrument,
            bar_type,
            instrument.id.venue,
            split_pct=0.7,
            run_many=pool.run_units,
        )
        assert seq["split_date"] == par["split_date"]
        assert seq["overfitting_score"] == par["overfitting_score"]
        assert seq["overfitting_label"] == par["overfitting_label"]
        assert comparable_metrics(seq["in_sample_metrics"]) == comparable_metrics(
            par["in_sample_metrics"]
        )
        assert comparable_metrics(seq["oos_metrics"]) == comparable_metrics(
            par["oos_metrics"]
        )
        assert seq["in_sample_equity"] == par["in_sample_equity"]
        assert seq["oos_equity"] == par["oos_equity"]


class TestFailureIsolation:
    def test_bad_unit_stays_in_band(self, pool):
        good = _spec()
        bad = _spec()
        bad.blocks[0].type = "nonexistent_block_xyz"  # unknown → errors in worker
        units = [
            {
                "key": "ok1",
                "kind": "slice",
                "spec": good.to_dict(),
                "irange": [0, 400],
                "iteration_id": 1,
                "rationale": "t",
            },
            {
                "key": "bad",
                "kind": "slice",
                "spec": bad.to_dict(),
                "irange": [0, 400],
                "iteration_id": 2,
                "rationale": "t",
            },
            {
                "key": "ok2",
                "kind": "slice",
                "spec": good.to_dict(),
                "irange": [400, 800],
                "iteration_id": 3,
                "rationale": "t",
            },
        ]
        out = pool.run_units(units)
        assert set(out) == {"ok1", "bad", "ok2"}
        assert out["bad"]["error"], "invalid spec must surface as in-band error"
        assert not out["ok1"].get("error")
        assert not out["ok2"].get("error")


_ORPHAN_OWNER_SCRIPT = r"""
import sys, time
sys.path.insert(0, {repo!r})
import numpy as np, pandas as pd
import parallel_exec as PE

idx = pd.date_range("2024-01-01", periods=120, freq="1h", tz="UTC")
close = 30000 + np.cumsum(np.ones(120))
df = pd.DataFrame({{"open": close, "high": close + 5, "low": close - 5,
                    "close": close, "volume": np.ones(120)}}, index=idx)
snap = PE.make_snapshot(df)
pool = PE.BacktestPool(snap, {recipe!r}, max_workers=2)
# Force worker spawn + initializer via two tiny units.
from composer import ComposedStrategySpec, SignalBlock
spec = ComposedStrategySpec(id="t", name="t", description="", blocks=[
    SignalBlock(type="ma_cross", role="entry",
                params={{"fast": 5, "slow": 15, "direction": "up"}})])
pool.run_units([
    {{"key": "a", "kind": "slice", "spec": spec.to_dict(), "irange": [0, 60],
      "iteration_id": 1, "rationale": "t"}},
    {{"key": "b", "kind": "slice", "spec": spec.to_dict(), "irange": [60, 120],
      "iteration_id": 2, "rationale": "t"}},
])
pids = sorted(p.pid for p in pool._pool._processes.values())
print("PIDS:" + ",".join(map(str, pids)), flush=True)
time.sleep(300)  # hang until the test terminates us
"""


def _pid_alive(pid: int) -> bool:
    """stdlib-only liveness check (psutil is not installed)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


class TestOrphanReaping:
    def test_workers_die_when_owner_is_terminated(self):
        repo = str(Path(__file__).resolve().parents[1])
        script = _ORPHAN_OWNER_SCRIPT.format(repo=repo, recipe=_RECIPE)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=repo,
        )
        try:
            pids: list[int] = []
            deadline = time.monotonic() + 120  # worker spawn + nautilus import
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        pytest.fail("owner exited before reporting worker PIDs")
                    continue
                if line.startswith("PIDS:"):
                    pids = [int(x) for x in line[5:].strip().split(",") if x]
                    break
            assert pids, "owner never reported worker PIDs"
            assert all(_pid_alive(p) for p in pids), "workers should be alive"

            proc.terminate()  # hard-kill the pool owner (no cleanup runs)
            proc.wait(timeout=10)

            # Watchdog polls parent liveness every 1s → workers self-exit.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not any(_pid_alive(p) for p in pids):
                    break
                time.sleep(0.5)
            leftovers = [p for p in pids if _pid_alive(p)]
            assert not leftovers, f"orphaned pool workers survived: {leftovers}"
        finally:
            if proc.poll() is None:
                proc.kill()
            # deterministically close the stdout pipe — text=True + no encoding
            # on Windows opens a cp1254 TextIOWrapper; if not closed, GC reaps it
            # and emits a ResourceWarning (result-identical; only reclaims the fd).
            if proc.stdout:
                proc.stdout.close()
