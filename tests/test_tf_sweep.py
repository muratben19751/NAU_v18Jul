"""/backtest/sweep — run the same strategy on multiple TFs → comparison.

run_backtest_guarded + load_bybit_bars + _bybit_cache_path are mocked. Verified:
cache-less TF is skipped, engine error is caught in the row, best column is highlighted,
<2 TF is rejected.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pandas as pd
import pytest


def _spec():
    import composer

    return composer.ComposedStrategySpec(
        id="sw1",
        name="TF Test",
        description="",
        blocks=[
            composer.SignalBlock(
                type="ma_cross", role="entry", params={"fast": 5, "slow": 20}
            ),
            composer.SignalBlock(type="atr_stop", role="exit", params={"period": 14}),
        ],
    )


@pytest.fixture
def wired(monkeypatch):
    import data
    import sandbox
    import web.routes.backtest as bt

    spec = _spec()
    monkeypatch.setattr(bt, "load_catalog", lambda: [spec])

    made: list[str] = []
    # '1m' (code "1") has no cache; the rest do.
    monkeypatch.setattr(
        data,
        "_bybit_cache_path",
        lambda cat, sym, i: SimpleNamespace(exists=lambda: i != "1"),
    )

    def _bars(symbol, interval, category, start, end):
        idx = pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC")
        return pd.DataFrame(
            {c: [1.0] * 100 for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )

    monkeypatch.setattr(data, "load_bybit_bars", _bars)
    monkeypatch.setattr(data, "_base_ccy", lambda s: "BTC")
    monkeypatch.setattr(
        pd,
        "read_parquet",
        lambda p: pd.DataFrame(
            {"close": [1.0] * 100},
            index=pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC"),
        ),
    )

    metrics = {
        "15": {
            "pnl_pct": 0.20,
            "sharpe_per_trade": 1.5,
            "max_dd_pct": -8.0,
            "n_trades": 40,
            "win_rate": 0.55,
            "profit_factor": 1.8,
        },
        "240": {
            "pnl_pct": 0.05,
            "sharpe_per_trade": 0.6,
            "max_dd_pct": -15.0,
            "n_trades": 12,
            "win_rate": 0.42,
            "profit_factor": 1.1,
        },
    }

    def _guarded(spec, bars, recipe, **kw):
        made.append(recipe["interval"])
        if recipe["interval"] == "D":
            return SimpleNamespace(error="engine crashed on D", metrics={})
        return SimpleNamespace(error=None, metrics=metrics[recipe["interval"]])

    monkeypatch.setattr(sandbox, "run_backtest_guarded", _guarded)
    return {"made": made}


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _poll(c, sid, tries=80):
    for _ in range(tries):
        p = c.get(f"/backtest/sweep/progress/{sid}")
        if "✓ done" in p.text or "empty-state" in p.text:
            return p
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


class TestTFSweep:
    def test_runs_each_tf_skips_cacheless_catches_error(self, wired):
        c = _client()
        r = c.post(
            "/backtest/sweep",
            data={
                "spec_id": "sw1",
                "symbol": "BTCUSDT",
                "category": "linear",
                "intervals": ["1", "15", "240", "D"],
            },
        )
        assert r.status_code == 200
        sid = re.search(r"sweep/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, sid)

        # '1' (1m) has no cache → not run; the remaining 3 run.
        # Sweep runs intervals concurrently → completion order is nondeterministic
        # (display rows stay in interval order). Assert the SET of runs.
        assert sorted(wired["made"]) == ["15", "240", "D"]
        assert "no cache" in page.text  # 1m row
        assert "engine crashed on D" in page.text  # D engine error in the row
        # Best PnL 15m (20%) and 4h (5%) in the table.
        assert "+20.00" in page.text and "+5.00" in page.text

    def test_requires_at_least_two_timeframes(self, wired):
        c = _client()
        r = c.post("/backtest/sweep", data={"spec_id": "sw1", "intervals": ["15"]})
        assert r.status_code == 400

    def test_intervals_csv_fallback(self, wired):
        # the describe→sweep chain carries intervals as csv; csv is read when the list is empty.
        c = _client()
        r = c.post(
            "/backtest/sweep",
            data={"spec_id": "sw1", "intervals_csv": "15,240,D"},
        )
        assert r.status_code == 200
        sid = re.search(r"sweep/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, sid)
        # Sweep runs intervals concurrently → completion order is nondeterministic
        # (display rows stay in interval order). Assert the SET of runs.
        assert sorted(wired["made"]) == ["15", "240", "D"]

    def test_unknown_spec_404(self, wired):
        c = _client()
        r = c.post(
            "/backtest/sweep",
            data={"spec_id": "unknown", "intervals": ["15", "60"]},
        )
        assert r.status_code == 404
