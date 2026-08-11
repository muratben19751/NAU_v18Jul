"""download_grouped_daily.py — sahte grouped yanıtlarıyla uçtan uca.

Ağa çıkmaz: `_get` monkeypatch'lenir, böylece gün seçimi, dosya düzeni ve
CSV sözleşmesi gerçek kod yolundan geçer. İddialar davranışsal — satır SAYISI
değil, flat-file aynasıyla aynı yol/şemaya düşmesi, ms→ns `window_start`,
ayarlı/ham ağaçların ayrılması, tatil gününün hata sayılmaması ve yarım dosya
bırakılmaması.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import download_grouped_daily as dl

_DAY = date(2026, 8, 5)  # Çarşamba
_T_MS = 1_785_960_000_000  # o günün başı (ET), Massive `t` alanının birimi


def _row(
    ticker: str, o: float, h: float, lo: float, c: float, v: float, n: int
) -> dict:
    return {"T": ticker, "o": o, "h": h, "l": lo, "c": c, "v": v, "n": n, "t": _T_MS}


_ROWS = [
    _row("NVDA", 216.86, 222.22, 216.4, 219.22, 156_576_414.33, 2_881_959),
    _row("AAPL", 309.36, 311.71, 305.67, 311.0, 52_071_835.97, 954_713),
]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")


def _fake_get(body: dict):
    """Sabit yanıt; çağrılan URL'leri kaydeder."""
    seen: list[str] = []

    def _get(url: str, key: str, pacer, *, tries: int = 6) -> dict:
        seen.append(url)
        return body

    _get.seen = seen  # type: ignore[attr-defined]
    return _get


def _read(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_roundtrip_path_schema_and_ns_window_start(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_get", _fake_get({"status": "OK", "results": _ROWS}))

    stats = dl.download([_DAY], dest=tmp_path, rpm=0)
    assert stats == {"yazilan": 1, "atlanan": 0, "bos": 0, "ticker": 2}

    # Yol, flat-file aynasının key düzeniyle birebir (ingest tarafı değişmesin).
    out = tmp_path / "us_stocks_sip" / "day_aggs_adjusted_v1" / "2026" / "08"
    assert list(out.iterdir()) == [out / "2026-08-05.csv.gz"]

    rows = _read(out / "2026-08-05.csv.gz")
    assert list(rows[0]) == list(dl._CSV_HEADER)
    assert [r["ticker"] for r in rows] == ["AAPL", "NVDA"]  # ticker'a göre sıralı
    assert rows[0]["open"] == "309.36"
    assert rows[0]["transactions"] == "954713"
    assert int(rows[0]["window_start"]) == _T_MS * 1_000_000  # ms → ns


def test_adjusted_and_raw_land_in_separate_trees(tmp_path, monkeypatch):
    fake = _fake_get({"status": "OK", "results": _ROWS})
    monkeypatch.setattr(dl, "_get", fake)

    dl.download([_DAY], dest=tmp_path, rpm=0)
    dl.download([_DAY], dest=tmp_path, rpm=0, adjusted=False)

    stem = tmp_path / "us_stocks_sip"
    assert (
        stem / "day_aggs_adjusted_v1" / "2026" / "08" / "2026-08-05.csv.gz"
    ).exists()
    assert (stem / "day_aggs_v1" / "2026" / "08" / "2026-08-05.csv.gz").exists()
    assert [u.split("?")[-1] for u in fake.seen] == ["adjusted=true", "adjusted=false"]


def test_existing_day_is_skipped_until_overwrite(tmp_path, monkeypatch):
    fake = _fake_get({"status": "OK", "results": _ROWS})
    monkeypatch.setattr(dl, "_get", fake)
    dl.download([_DAY], dest=tmp_path, rpm=0)

    # İkinci koşum ağa hiç çıkmaz: var olan gün yeniden indirilmez.
    assert dl.download([_DAY], dest=tmp_path, rpm=0)["atlanan"] == 1
    assert len(fake.seen) == 1

    assert dl.download([_DAY], dest=tmp_path, rpm=0, overwrite=True)["yazilan"] == 1
    assert len(fake.seen) == 2


def test_holiday_is_empty_not_an_error(tmp_path, monkeypatch):
    """Tatil/hafta sonu `resultsCount=0` verir: dosya yazılmaz, koşum sürer."""
    monkeypatch.setattr(dl, "_get", _fake_get({"status": "OK", "resultsCount": 0}))

    stats = dl.download([date(2026, 8, 1)], dest=tmp_path, rpm=0)

    assert stats["bos"] == 1 and stats["yazilan"] == 0
    assert not (tmp_path / "us_stocks_sip").exists()


def test_not_authorized_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dl,
        "_get",
        _fake_get({"status": "NOT_AUTHORIZED", "message": "plan"}),
    )
    with pytest.raises(dl.GroupedDailyError, match="NOT_AUTHORIZED"):
        dl.download([_DAY], dest=tmp_path, rpm=0)


def test_failed_write_leaves_no_half_file(tmp_path, monkeypatch):
    """Yazım ortada patlarsa ne `.part` ne de hedef ad kalır."""
    path = dl.target_path(_DAY, dest=tmp_path)
    boom = [_ROWS[0], {"T": "BAD", "t": "ns-olamaz"}]  # int() patlar

    with pytest.raises(ValueError):
        dl.write_day(boom, path)

    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_missing_field_is_rejected_the_same_way_as_a_bad_type(tmp_path):
    """DeepR 2026-08-09 [ORTA]: geçersiz TİP ("t": "ns-olamaz", yukarıdaki
    test) zaten hata veriyordu, ama EKSİK anahtar sessizce epoch-0/"" ile
    kabul ediliyordu — tutarsız doğrulama sınırı. İkisi artık aynı: ne "T"
    ne "t" eksik geçebilir, ikisi de aynı yarım-dosya-bırakmama garantisiyle
    reddedilir."""
    path = dl.target_path(_DAY, dest=tmp_path)
    missing_t = [_ROWS[0], {"T": "NOPE"}]  # "t" hiç yok

    with pytest.raises(dl.GroupedDailyError, match=r"missing.*\['t'\]"):
        dl.write_day(missing_t, path)
    assert not path.exists()
    assert list(path.parent.iterdir()) == []

    missing_T = [_ROWS[0], {"t": _T_MS}]  # "T" hiç yok

    with pytest.raises(dl.GroupedDailyError, match=r"missing.*\['T'\]"):
        dl.write_day(missing_T, path)
    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_workers_write_every_day_exactly_once(tmp_path, monkeypatch):
    """Paralel yol seri yolla aynı sonucu vermeli — sayaçlar da dosyalar da."""
    fake = _fake_get({"status": "OK", "results": _ROWS})
    monkeypatch.setattr(dl, "_get", fake)
    days = [date(2026, 7, d) for d in (1, 2, 6, 7, 8, 9)]

    stats = dl.download(days, dest=tmp_path, rpm=0, workers=4)

    assert stats == {"yazilan": 6, "atlanan": 0, "bos": 0, "ticker": 12}
    out = tmp_path / "us_stocks_sip" / "day_aggs_adjusted_v1" / "2026" / "07"
    assert sorted(p.name for p in out.iterdir()) == [
        f"{d.isoformat()}.csv.gz" for d in days
    ]
    assert len(fake.seen) == 6  # gün başına tek çağrı; tekrar yok


def test_workers_propagate_plan_error(tmp_path, monkeypatch):
    """Paralelde de yetki hatası yutulmaz: koşum hata ile biter."""
    monkeypatch.setattr(dl, "_get", _fake_get({"status": "NOT_AUTHORIZED"}))
    days = [date(2026, 7, d) for d in (1, 2, 6, 7)]

    with pytest.raises(dl.GroupedDailyError, match="NOT_AUTHORIZED"):
        dl.download(days, dest=tmp_path, rpm=0, workers=4)


def test_rpm_ceiling_is_per_run_not_per_worker(monkeypatch):
    """Paylaşılan pacer: 4 işçi tavanı 4'e katlamaz."""
    pacer = dl._Pacer(rpm=600)  # 0,1 s aralık
    ticks: list[float] = []

    def _one() -> None:
        pacer.wait()
        ticks.append(time.monotonic())

    threads = [threading.Thread(target=_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ticks) == 4
    # İlk çağrı beklemez, kalan üçü 0,1 s'lik aralığı sırayla bekler.
    # Tavan işçi başına sayılsaydı dördü de aynı anda geçer, yayılım ~0 olurdu.
    assert max(ticks) - min(ticks) >= 0.25


class _FrozenDate(date):
    """`date.today()` sabit — varsayılan gün seçimi takvimden bağımsız sınansın."""

    @classmethod
    def today(cls) -> date:
        return date(2026, 8, 10)  # Pazartesi


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"date": "", "start": "", "end": "", "days": 1, **kw})


def test_default_is_last_weekday_not_literally_yesterday(monkeypatch):
    """Pazartesi koşulunca "dün" pazar değil, cuma olmalı."""
    monkeypatch.setattr(dl, "date", _FrozenDate)

    assert dl._parse_days(_args()) == [date(2026, 8, 7)]
    assert dl._parse_days(_args(days=5)) == [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
    ]


def test_range_drops_weekends_and_rejects_reversed_bounds():
    days = dl._parse_days(_args(start="2026-08-05", end="2026-08-11"))
    assert days == [
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
        date(2026, 8, 10),
        date(2026, 8, 11),
    ]
    assert dl._parse_days(_args(date="2026-08-08")) == [date(2026, 8, 8)]  # cumartesi:
    # --date açıkça isteneni indirir; hafta sonu süzgeci yalnız aralık/varsayılan için.

    with pytest.raises(dl.GroupedDailyError, match="önce"):
        dl._parse_days(_args(start="2026-08-11", end="2026-08-05"))


def test_missing_key_is_an_actionable_error(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(dl.GroupedDailyError, match="MASSIVE_API_KEY"):
        dl.api_key()
