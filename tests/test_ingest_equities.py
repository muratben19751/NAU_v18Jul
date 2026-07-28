"""ingest_equities.py — sentetik flat-file arşiviyle uçtan uca.

Gerçek E:\\MarketData arşivine dokunmaz: tmp_path altında Polygon şemasında
(`ticker,volume,open,close,high,low,window_start,transactions`) iki günlük
minik bir minute_aggs_v1 kurulur ve ingest o kökten koşar. İddialar yapısal
değil davranışsal — bar SAYISI değil, right-label sözleşmesi, RTH filtresi
ve OHLCV toplama doğruluğu.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pandas as pd
import pytest

import data as data_mod
import ingest_equities as ing

# 2026-07-06 Pazartesi; NY o tarihte EDT (UTC-4) → 09:30 NY = 13:30 UTC.
_DAY = "2026-07-06"
_NY = "America/New_York"


def _ns(day: str, hhmm: str) -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz=_NY).tz_convert("UTC").value)


def _write_day(root: Path, day: str, rows: list[dict]) -> None:
    y, m, _ = day.split("-")
    d = root / "minute_aggs_v1" / y / m
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "volume",
            "open",
            "close",
            "high",
            "low",
            "window_start",
            "transactions",
        ],
    )
    with gzip.open(d / f"{day}.csv.gz", "wt", newline="") as f:
        df.to_csv(f, index=False)


def _row(tk: str, day: str, hhmm: str, o: float, h: float, lo: float, c: float, v: int):
    return {
        "ticker": tk,
        "volume": v,
        "open": o,
        "close": c,
        "high": h,
        "low": lo,
        "window_start": _ns(day, hhmm),
        "transactions": 1,
    }


@pytest.fixture
def archive(tmp_path: Path) -> tuple[Path, Path]:
    """(flat-file kökü, hedef katalog kökü) — EXTERNAL_CATALOGS'a hedef bağlanır."""
    src = tmp_path / "flatfiles"
    cat = tmp_path / "equity_catalog"
    _write_day(
        src,
        _DAY,
        [
            # RTH içi üç dakika — 1-DAY bar bunlardan toplanmalı.
            _row("NEWT", _DAY, "09:30", 10.0, 10.5, 9.9, 10.2, 100),
            _row("NEWT", _DAY, "09:31", 10.2, 11.0, 10.1, 10.8, 50),
            _row("NEWT", _DAY, "15:59", 10.8, 10.9, 10.0, 10.1, 25),
            # Premarket 04:00 — 1-MINUTE'a girer, RTH TF'lerine GİRMEZ.
            _row("NEWT", _DAY, "04:00", 9.0, 99.0, 1.0, 9.5, 7),
        ],
    )
    return src, cat


def _minute_bars(cat_dir: Path, ident: str):
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    return ParquetDataCatalog(str(cat_dir)).query(data_cls=Bar, identifiers=[ident])


def test_ingest_roundtrip_right_label_and_rth(archive, monkeypatch):
    src, cat = archive
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [cat])

    stats = ing.ingest({"NEWT"}, "2026-2026", root=src, catalog_dir=cat)
    assert stats["NEWT"]["bars"] == 4
    assert stats["NEWT"]["first"] == _DAY

    # Right-label: 09:30 penceresinin bar'ı 09:31'de KAPANIR (+60e9).
    mins = _minute_bars(cat, "NEWT.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    assert [b.ts_event for b in mins][1] == _ns(_DAY, "09:30") + 60_000_000_000

    # 1-DAY: RTH içi üç bar'dan toplanır — premarket'in 99/1 uçları ve 7'lik
    # hacmi DIŞARIDA kalmalı. O=ilk açılış, H/L=RTH uçları, C=son kapanış.
    days = _minute_bars(cat, "NEWT.NASDAQ-1-DAY-LAST-EXTERNAL")
    assert len(days) == 1
    d = days[0]
    assert (float(d.open), float(d.high), float(d.low), float(d.close)) == (
        10.0,
        11.0,
        9.9,
        10.1,
    )
    assert float(d.volume) == 175  # 100+50+25; premarket 7 yok

    # Uygulamanın kendi okuyucusu aynı kökü görüyor: 6 granülerlik listelenir
    # ve manifest'in adjusted=false bayrağı taşınır.
    inst = {r["instrument_id"]: r for r in data_mod.list_external_instruments()}
    assert "NEWT.NASDAQ" in inst
    assert set(inst["NEWT.NASDAQ"]["granularities"]) == {
        "1-MINUTE",
        "5-MINUTE",
        "15-MINUTE",
        "1-HOUR",
        "4-HOUR",
        "1-DAY",
    }
    assert inst["NEWT.NASDAQ"]["adjusted"] is False


def test_guard_skips_tickers_already_in_other_roots(archive, tmp_path, monkeypatch):
    src, cat = archive
    other = tmp_path / "nau_ev_catalog"
    (other / "data" / "bar" / "NEWT.XNAS-1-DAY-LAST-EXTERNAL").mkdir(parents=True)
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [other, cat])

    # Venue farklı (XNAS) olsa da bare-ticker eşleşir: NEWT atlanır, hiç yazılmaz.
    stats = ing.ingest({"NEWT"}, "2026-2026", root=src, catalog_dir=cat)
    assert stats == {}
    assert not (cat / "data" / "bar").exists() or not list(
        (cat / "data" / "bar").glob("NEWT.*")
    )

    # --force karşılığı: aynı hedef force=True ile yazılır.
    stats = ing.ingest({"NEWT"}, "2026-2026", root=src, catalog_dir=cat, force=True)
    assert stats["NEWT"]["bars"] == 4


def test_reingest_is_fresh_not_additive(archive, monkeypatch):
    src, cat = archive
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [cat])

    ing.ingest({"NEWT"}, "2026-2026", root=src, catalog_dir=cat)
    ing.ingest({"NEWT"}, "2026-2026", root=src, catalog_dir=cat)
    # İkinci koşum baştan temizler: bar'lar birikmez, sayı aynı kalır.
    mins = _minute_bars(cat, "NEWT.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    assert len(mins) == 4
