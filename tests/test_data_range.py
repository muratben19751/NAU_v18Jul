"""coverage_range + GET /data/range — the date pickers' MAX button backend.

Verifies: bybit coverage comes from the pandas cache's parquet footer, external
coverage from catalog FILENAMES only (no bar decode — files here are empty),
missing series → None/404, and the endpoint serializes plain ISO dates.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import data as data_mod
from data import coverage_range


@pytest.fixture()
def bybit_cache(tmp_path, monkeypatch):
    p = tmp_path / "linear_BTCUSDT_60.parquet"
    df = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 1.0],
        },
        index=pd.DatetimeIndex(["2024-01-02 00:00", "2025-06-30 23:00"], tz="UTC"),
    )
    df.to_parquet(p)
    monkeypatch.setattr(
        data_mod,
        "_bybit_cache_path",
        lambda c, s, i: p if s == "BTCUSDT" else tmp_path / "yok.parquet",
    )
    return p


def test_bybit_coverage_from_parquet_footer(bybit_cache):
    rng = coverage_range("bybit", symbol="BTCUSDT", category="linear", interval="60")
    assert rng == {"start": "2024-01-02", "end": "2025-06-30"}
    # Missing cache → None (the endpoint turns this into a 404).
    assert (
        coverage_range("bybit", symbol="NOPE", category="linear", interval="60") is None
    )


def test_external_coverage_from_filenames(tmp_path, monkeypatch):
    d = tmp_path / "cat" / "data" / "bar" / "QQQ.NASDAQ-1-HOUR-LAST-EXTERNAL"
    d.mkdir(parents=True)
    # Only the NAMES matter — the reader never decodes these files.
    (
        d / "2003-09-10T13-30-00-000000000Z_2010-12-31T21-00-00-000000000Z.parquet"
    ).touch()
    (
        d / "2011-01-03T14-30-00-000000000Z_2026-07-01T20-00-00-000000000Z.parquet"
    ).touch()
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [tmp_path / "cat"])

    rng = coverage_range("external", instrument_id="QQQ.NASDAQ", granularity="1-HOUR")
    assert rng == {"start": "2003-09-10", "end": "2026-07-01"}
    # Different granularity of the same instrument → nothing ingested.
    assert (
        coverage_range("external", instrument_id="QQQ.NASDAQ", granularity="4-HOUR")
        is None
    )


def test_range_endpoint(bybit_cache):
    from server import app

    c = TestClient(app)
    r = c.get(
        "/data/range", params={"source": "bybit", "symbol": "btcusdt", "interval": "60"}
    )
    assert r.status_code == 200
    assert r.json() == {"start": "2024-01-02", "end": "2025-06-30"}
    assert (
        c.get(
            "/data/range",
            params={"source": "bybit", "symbol": "NOPE", "interval": "60"},
        ).status_code
        == 404
    )
