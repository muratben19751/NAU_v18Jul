"""POST /agent/run — dış katalog bütünlük kapısı event loop'u kilitlemesin.

DeepR 2026-08-11 [YÜKSEK]: `_validate_external_data` `async def run(...)`
gövdesinden DOĞRUDAN çağrılıyordu. Fonksiyon her interval için parquet'ten tam
seriyi okuyup (1-DAKİKA bir hisse serisinde milyonlarca satır) pandas ile
tarıyor; multi-TF'te üç kez. Sonuç: POST /agent/run saniyelerce TÜM sunucuyu
donduruyordu — açık her sekmedeki 1 saniyelik HTMX poll'ları dahil.

Buradaki testler iki şeyi çiviliyor:
  1. Kapı artık `asyncio.to_thread` ile iş parçacığında çalışıyor (koroutin
     gövdesinde DEĞİL) — çalıştığı yerde koşan bir event loop olmamalı.
  2. Tarih aralığı daraltması `load_external_bars`'a itildi (sonradan pandas
     maskesi yerine) ve sınır davranışı BİREBİR aynı kaldı:
     eski `index < range_end + 1 gün` ile yeni `index <= range_end + 1 gün − 1ns`
     nanosaniye çözünürlüklü indekste aynı satır kümesini seçer.

Wiki References
---------------
See: [[nau_performans_denetimi]], [[webapp_module_map]].
"""

from __future__ import annotations

import asyncio
import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient


def _bars(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2020-01-02 14:30", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )


def _client(monkeypatch):
    import web.routes.agent_backtest as ab
    from server import app

    got: dict = {}
    done = threading.Event()

    monkeypatch.setattr(ab, "_AUTO_RUN_SLOTS", threading.BoundedSemaphore(50))

    def fake_worker(**kw):
        got.update(kw)
        done.set()

    monkeypatch.setattr(ab, "_agent_worker", fake_worker)
    return TestClient(app), got, done


def _stub_catalog(monkeypatch, loader):
    import data

    monkeypatch.setattr(data, "_external_bar_dir", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a, **k: True)
    monkeypatch.setattr(data, "external_data_gap_report", lambda frame, **kw: None)
    monkeypatch.setattr(data, "load_external_bars", loader)


def test_validation_gate_runs_off_the_event_loop(monkeypatch):
    """Kapı bir worker thread'de koşmalı: orada koşan bir event loop olamaz."""
    import web.routes.agent_backtest as ab

    seen: dict = {}
    real = ab._validate_external_data

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        seen["thread"] = threading.current_thread().name
        return real(*args, **kwargs)

    monkeypatch.setattr(ab, "_validate_external_data", spy)
    _stub_catalog(monkeypatch, lambda *a, **k: _bars())

    client, got, done = _client(monkeypatch)
    r = client.post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2},
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert seen["on_loop"] is False, (
        "bütünlük kapısı hâlâ koroutin gövdesinde çalışıyor — event loop kilitli"
    )
    assert got["intervals"] == ["1-DAY"]


def test_refusal_response_still_reaches_the_client_from_the_thread(monkeypatch):
    """to_thread'e taşınan kapı reddederse 400 hâlâ istemciye dönmeli."""
    import data

    _stub_catalog(monkeypatch, lambda *a, **k: _bars())
    monkeypatch.setattr(
        data,
        "external_data_gap_report",
        lambda frame, **kw: {
            "kind": "calendar",
            "days": 900,
            "from": "2005-01-01",
            "to": "2007-06-01",
        },
    )
    client, _got, _done = _client(monkeypatch)
    r = client.post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2},
    )
    assert r.status_code == 400
    assert "900-day gap" in r.text


def test_range_is_pushed_into_the_loader_not_masked_afterwards(monkeypatch):
    """Aralık artık `load_external_bars(start=, end=)` ile daraltılıyor."""
    calls: list[dict] = []

    def loader(instrument_id, granularity, start=None, end=None):
        calls.append(
            {
                "instrument_id": instrument_id,
                "granularity": granularity,
                "start": start,
                "end": end,
            }
        )
        return _bars()

    _stub_catalog(monkeypatch, loader)
    client, _got, done = _client(monkeypatch)
    r = client.post(
        "/agent/run",
        data={
            "symbol": "QQQ.NASDAQ",
            "tfs": ["D"],
            "range_start": "2021-03-01",
            "range_end": "2021-09-30",
            "n_iterations": 2,
        },
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert len(calls) == 1
    call = calls[0]
    assert call["start"] == pd.Timestamp("2021-03-01", tz="UTC")
    # +1 gün − 1ns: eski `< 2021-10-01 00:00` maskesinin kapsayıcı karşılığı.
    assert call["end"] == pd.Timestamp("2021-10-01 00:00:00", tz="UTC") - pd.Timedelta(
        nanoseconds=1
    )


def test_open_ended_window_passes_no_bounds(monkeypatch):
    calls: list[tuple] = []

    def loader(instrument_id, granularity, start=None, end=None):
        calls.append((start, end))
        return _bars()

    _stub_catalog(monkeypatch, loader)
    client, _got, done = _client(monkeypatch)
    r = client.post(
        "/agent/run", data={"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2}
    )
    assert r.status_code == 200
    assert done.wait(5)
    assert calls == [(None, None)]


@pytest.mark.parametrize("freq", ["B", "1h", "1min", "1s", "1ms", "1us", "1ns"])
def test_inclusive_end_minus_one_ns_selects_the_same_rows(freq):
    """Sınır dönüşümünün BİREBİR kanıtı: `< X` ≡ `<= X − 1ns`.

    Nanosaniye çözünürlüklü bir DatetimeIndex'te — sınırın tam üstünde bir bar
    OLSA BİLE — iki maske aynı satırları seçer.
    """
    edge = pd.Timestamp("2021-10-01 00:00:00", tz="UTC")
    idx = pd.date_range(end=edge + pd.Timedelta(nanoseconds=5), periods=50, freq=freq)
    # Sınırın tam üstünde bir satır olduğundan emin ol (en zor durum).
    idx = idx.append(pd.DatetimeIndex([edge, edge - pd.Timedelta(nanoseconds=1)]))
    df = pd.DataFrame({"close": 1.0}, index=idx.sort_values())
    old_mask = df.index < edge
    new_mask = df.index <= edge - pd.Timedelta(nanoseconds=1)
    assert (old_mask == new_mask).all()
    assert old_mask.sum() >= 1  # maske gerçekten bir şey eliyor
