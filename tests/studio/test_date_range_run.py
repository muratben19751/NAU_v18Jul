"""Builder date-range run: POST /backtest date_from/date_to + /data-range MAX.

Stub-adapter chain (TestClient runs background tasks synchronously): an
explicit range must label the run's Range chip, malformed input must 400
before a run is consumed, and the MAX endpoint must union instrument coverage.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from strategy_studio.backtest import _slice_dates


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    fx = build_fixture()
    for ic in fx.instruments:
        ic.active = True
    store.save(fx)
    monkeypatch.setattr(main, "store", store)
    return TestClient(_host)


def test_slice_dates_inclusive_days():
    df = pd.DataFrame(
        {"close": range(400)},
        index=pd.date_range("2025-01-01", periods=400, freq="h", tz="UTC"),
    )
    out = _slice_dates(df, ("2025-01-05", "2025-01-10"))
    assert out.index[0].date().isoformat() == "2025-01-05"
    assert out.index[-1].date().isoformat() == "2025-01-10"  # end day inclusive
    # Open-ended sides and non-datetime frames pass through sensibly.
    assert len(_slice_dates(df, ("", ""))) == 400
    plain = pd.DataFrame({"close": [1, 2, 3]})
    assert _slice_dates(plain, ("2025-01-01", "2025-01-02")) is plain


def test_run_with_range_labels_metrics(client):
    r = client.post(
        "/studio/wt-funding-v3/backtest",
        data={"date_from": "2024-01-01", "date_to": "2024-06-30"},
    )
    assert r.status_code == 200
    latest = client.get("/studio/wt-funding-v3/runs/latest").text
    assert "2024-01-01" in latest and "2024-06-30" in latest


def test_bad_dates_rejected_before_run(client):
    assert (
        client.post(
            "/studio/wt-funding-v3/backtest", data={"date_from": "kotu"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/studio/wt-funding-v3/backtest",
            data={"date_from": "2025-01-02", "date_to": "2025-01-01"},
        ).status_code
        == 400
    )


def test_data_range_unions_instrument_coverage(client, monkeypatch):
    import data as data_mod

    spans = {
        "XAUUSD": {"start": "2019-05-01", "end": "2026-01-01"},
        "EURUSD": {"start": "2015-03-01", "end": "2025-12-01"},
        "NAS100": {"start": "2018-01-01", "end": "2026-06-01"},
    }

    def fake(source, **kw):
        return spans.get(kw.get("symbol") or kw.get("instrument_id"))

    monkeypatch.setattr(data_mod, "coverage_range", fake)
    r = client.get("/studio/wt-funding-v3/data-range")
    assert r.status_code == 200
    assert r.json() == {"start": "2015-03-01", "end": "2026-06-01"}

    monkeypatch.setattr(data_mod, "coverage_range", lambda *a, **k: None)
    assert client.get("/studio/wt-funding-v3/data-range").status_code == 404
