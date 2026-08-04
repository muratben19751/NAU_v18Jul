"""download_massive.py — sahte REST yanıtlarıyla uçtan uca.

Ağa çıkmaz: `_get` monkeypatch'lenir, böylece sayfalama (`next_url`), plan
penceresi dışı yıl (`NOT_AUTHORIZED`) ve ms→ns right-label dönüşümü gerçek
kod yolundan geçer. İddialar davranışsal — bar SAYISI değil, bar'ın KAPANIŞ
anına oturması, RTH filtresi ve manifest'in `adjusted: true` bayrağı.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import data as data_mod
import download_massive as dl

_DAY = "2026-07-06"  # Pazartesi; NY o tarihte EDT (UTC-4)
_NY = "America/New_York"


def _ms(day: str, hhmm: str) -> int:
    """Pencere BAŞI, Unix ms — Massive `t` alanının birimi."""
    return int(
        pd.Timestamp(f"{day} {hhmm}", tz=_NY).tz_convert("UTC").value // 1_000_000
    )


def _row(day: str, hhmm: str, o: float, h: float, lo: float, c: float, v: int) -> dict:
    return {"t": _ms(day, hhmm), "o": o, "h": h, "l": lo, "c": c, "v": v}


_ROWS = [
    _row(
        _DAY, "04:00", 9.0, 99.0, 1.0, 9.5, 7
    ),  # premarket: minute'a girer, TF'ye girmez
    _row(_DAY, "09:30", 10.0, 10.5, 9.9, 10.2, 100),
    _row(_DAY, "09:31", 10.2, 11.0, 10.1, 10.8, 50),
    _row(_DAY, "15:59", 10.8, 10.9, 10.0, 10.1, 25),
]


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch) -> Path:
    cat = tmp_path / "equity_catalog"
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [cat])
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    return cat


def _fake_get(pages: dict[str, dict]):
    """URL → yanıt sözlüğü; çağrılan URL'leri de kaydeder."""
    seen: list[str] = []

    def _get(url: str, key: str, pacer, *, tries: int = 6) -> dict:
        seen.append(url)
        for frag, body in pages.items():
            if frag in url:
                return body
        raise AssertionError(f"beklenmeyen URL: {url}")

    _get.seen = seen  # type: ignore[attr-defined]
    return _get


def _bars(cat_dir: Path, ident: str):
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    return ParquetDataCatalog(str(cat_dir)).query(data_cls=Bar, identifiers=[ident])


def test_roundtrip_right_label_rth_and_adjusted_manifest(catalog, monkeypatch):
    monkeypatch.setattr(
        dl, "_get", _fake_get({"/range/1/minute/": {"status": "OK", "results": _ROWS}})
    )

    stats = dl.download(
        {"NEWT"},
        pd.Timestamp("2026-07-01").date(),
        pd.Timestamp("2026-07-31").date(),
        catalog_dir=catalog,
    )
    assert stats["NEWT"]["bars"] == 4

    # ms→ns + right-label: 09:30 penceresinin bar'ı 09:31'de KAPANIR.
    mins = _bars(catalog, "NEWT.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    want = int(pd.Timestamp(f"{_DAY} 09:31", tz=_NY).tz_convert("UTC").value)
    assert [b.ts_event for b in mins][1] == want

    # 1-DAY yalnız RTH'den toplanır: premarket'in 99/1 uçları ve 7'lik hacmi yok.
    days = _bars(catalog, "NEWT.NASDAQ-1-DAY-LAST-EXTERNAL")
    assert len(days) == 1
    d = days[0]
    assert (float(d.open), float(d.high), float(d.low), float(d.close)) == (
        10.0,
        11.0,
        9.9,
        10.1,
    )
    assert float(d.volume) == 175

    # Uygulamanın kendi okuyucusu: REST yolu ADJUSTED rozetiyle görünür.
    inst = {r["instrument_id"]: r for r in data_mod.list_external_instruments()}
    assert inst["NEWT.NASDAQ"]["adjusted"] is True


def test_next_url_pages_are_concatenated(catalog, monkeypatch):
    page2 = f"{dl.API_BASE}/v2/aggs/cursor/PAGE2"
    fake = _fake_get(
        {
            "/range/1/minute/": {
                "status": "OK",
                "results": _ROWS[:2],
                "next_url": page2,
            },
            "/cursor/PAGE2": {"status": "OK", "results": _ROWS[2:]},
        }
    )
    monkeypatch.setattr(dl, "_get", fake)

    stats = dl.download(
        {"NEWT"},
        pd.Timestamp("2026-07-01").date(),
        pd.Timestamp("2026-07-31").date(),
        catalog_dir=catalog,
    )
    assert stats["NEWT"]["bars"] == 4  # iki sayfa tek seride birleşti
    assert len(fake.seen) == 2


def test_plan_window_year_is_skipped_not_fatal(catalog, monkeypatch):
    """Plan dışı yıl koşumu düşürmez; kotanın içindeki yıl yine de yazılır."""

    def _get(url: str, key: str, pacer, *, tries: int = 6) -> dict:
        if "/2023-" in url:
            return {
                "status": "NOT_AUTHORIZED",
                "message": "Your plan doesn't include this timeframe",
            }
        return {"status": "OK", "results": _ROWS}

    monkeypatch.setattr(dl, "_get", _get)

    stats = dl.download(
        {"NEWT"},
        pd.Timestamp("2023-01-01").date(),
        pd.Timestamp("2026-07-31").date(),
        catalog_dir=catalog,
    )
    # 2023 atlandı; 2024/2025/2026 pencerelerinin her biri aynı 4 satırı verdi.
    assert stats["NEWT"]["bars"] == 12
    assert stats["NEWT"]["first"] == _DAY  # manifest yalnız gerçekten ineni gösterir


def test_missing_key_is_an_actionable_error(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(dl.MassiveError, match="MASSIVE_API_KEY"):
        dl.api_key()
