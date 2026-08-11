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
    # Tarih alanları boşken sweep yalnızca önbelleğin ilk/son bar zamanını
    # sorar; bunu parquet footer'ından okur (DeepR 2026-08-11 [ORTA] — eskiden
    # tüm dosyayı pd.read_parquet ile belleğe alıyordu).
    monkeypatch.setattr(
        data,
        "_read_parquet_stats",
        lambda p: {
            "rows": 100,
            "first": "2024-01-01T00:00:00",
            "last": "2024-01-05T03:00:00",
            "size_bytes": 4096,
        },
    )
    full_reads: list[object] = []
    _real_read_parquet = pd.read_parquet

    def _tracking_read_parquet(p, *a, **kw):
        full_reads.append(p)
        return _real_read_parquet(p, *a, **kw)

    monkeypatch.setattr(pd, "read_parquet", _tracking_read_parquet)

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
    return {"made": made, "full_reads": full_reads}


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _poll(c, sid, tries=80):
    for _ in range(tries):
        p = c.get(f"/backtest/sweep/progress/{sid}")
        if "✓ bitti" in p.text or "empty-state" in p.text:
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


class TestSweepReadsOnlyTheParquetFooter:
    """DeepR 2026-08-11 [ORTA]: tarih alanları boşken sweep, aynı parquet'i
    interval başına İKİ kez tam okuyordu — bir kez sadece ilk/son bar tarihini
    öğrenmek için (`pd.read_parquet`), hemen ardından `load_*_bars` içinde
    gerçek veri için. 8 timeframe'lik bir sweep bunları paralel koşturuyor,
    yani 1m BTCUSDT (1,1 M satır) için yüzlerce MB boşa decode ediliyordu.
    Gereken bilgi ~4 KB'lık parquet footer'ında zaten var.
    """

    def test_bybit_dateless_sweep_never_full_reads_the_cache(self, wired):
        c = _client()
        r = c.post(
            "/backtest/sweep",
            data={
                "spec_id": "sw1",
                "symbol": "BTCUSDT",
                "category": "linear",
                "intervals": ["15", "240"],
            },
        )
        assert r.status_code == 200
        sid = re.search(r"sweep/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, sid)

        assert sorted(wired["made"]) == ["15", "240"]
        assert wired["full_reads"] == [], (
            "sweep still full-reads the parquet just to learn the date range"
        )

    def test_index_dateless_sweep_never_full_reads_the_cache(self, wired, monkeypatch):
        import data
        import sandbox

        idx = pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC")
        monkeypatch.setattr(
            data,
            "load_index_bars",
            lambda t, s, e, g: pd.DataFrame(
                {c: [1.0] * 100 for c in ("open", "high", "low", "close", "volume")},
                index=idx,
            ),
        )
        # Cache dosyası "var" görünsün; içeriği hiç okunmamalı.
        monkeypatch.setattr(data, "_ticker_to_filename", lambda t: t)
        monkeypatch.setattr(
            data, "INDEX_CACHE_DIR", _AlwaysExistingDir(), raising=False
        )

        # Index tarafında recipe anahtarı 'granularity' (bybit'te 'interval').
        made = wired["made"]

        def _guarded_index(spec, bars, recipe, **kw):
            made.append(recipe["granularity"])
            return SimpleNamespace(
                error=None, metrics={"pnl_pct": 0.1, "n_trades": 3, "win_rate": 0.5}
            )

        monkeypatch.setattr(sandbox, "run_backtest_guarded", _guarded_index)

        c = _client()
        r = c.post(
            "/backtest/sweep",
            data={
                "spec_id": "sw1",
                "instrument_kind": "Index",
                "ticker": "SPY",
                "granularity_csv": "15m,60m",
            },
        )
        assert r.status_code == 200
        sid = re.search(r"sweep/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, sid)

        assert sorted(wired["made"]) == ["15m", "60m"]
        assert wired["full_reads"] == []


class _AlwaysExistingDir:
    """INDEX_CACHE_DIR yerine geçen minik sahte: `/` ile var olan bir yol üretir."""

    def __truediv__(self, other):
        return SimpleNamespace(exists=lambda: True)
