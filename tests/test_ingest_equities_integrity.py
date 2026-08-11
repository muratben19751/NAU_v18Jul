"""ingest_equities.py — 2026-08-10 veri bütünlüğü denetiminin beş kusuru.

`test_ingest_equities.py` ile aynı üslup: gerçek arşive/kataloğa hiç dokunulmaz,
tmp_path altında minik sentetik bir flat-file arşivi (Polygon şeması) ve minik
bir katalog kurulur. İddialar davranışsal — "seansta kaç bar var", "bozuk barın
low'u ne oldu", "manifest ne diyor".

Kapsanan kusurlar:
  4) pre-market sızıntısı (RTH alt sınırı right-label'a göre 09:31)
  5) `low = 0.00` bozuk barı (`_sanitize_ohlc`)
  6) 4-HOUR'un DST'de kırılması (seans-göreli kovalar)
  7) manifest çelişkisi (`split_adjusted` / `dividend_adjusted` + göç)
  8) kapsam boşlukları (`coverage_gaps`)
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pandas as pd
import pytest

import ingest_equities as ing

_NY = "America/New_York"
# Aynı yılda bir EDT bir EST seansı: 4-HOUR kovalarının DST'den bağımsız
# olduğunu göstermek için ikisi de gerekli (ikisi de Pazartesi, tam gün).
_EDT_DAY = "2026-07-06"  # UTC-4
_EST_DAY = "2026-01-05"  # UTC-5


# ── sentetik arşiv yardımcıları ──────────────────────────────────────────────


def _write_day(root: Path, day: str, rows: list[dict]) -> None:
    y, m, _ = day.split("-")
    d = root / "minute_aggs_v1" / y / m
    d.mkdir(parents=True, exist_ok=True)
    cols = [
        "ticker",
        "volume",
        "open",
        "close",
        "high",
        "low",
        "window_start",
        "transactions",
    ]
    with gzip.open(d / f"{day}.csv.gz", "wt", newline="") as f:
        pd.DataFrame(rows, columns=cols).to_csv(f, index=False)


def _session_rows(tk: str, day: str, base: float = 100.0) -> list[dict]:
    """Bir tam RTH seansı — window_start 09:29..15:59, yani damga 09:30..16:00.

    391 satır BİLEREK: 09:29–09:30 dilimi pre-market'tir ve right-label yüzünden
    "09:30" damgasıyla RTH'ye sızmaya adaydır. Testin yakaladığı tam olarak bu.
    Her dakika bir kuruş yukarı → açılış/kapanış hangi bardan geldiği belli olur.
    """
    rows = []
    t = pd.Timestamp(f"{day} 09:29", tz=_NY)
    end = pd.Timestamp(f"{day} 15:59", tz=_NY)
    i = 0
    while t <= end:
        px = round(base + i * 0.01, 2)
        rows.append(
            {
                "ticker": tk,
                "volume": 10,
                "open": px,
                "close": round(px + 0.01, 2),
                "high": round(px + 0.02, 2),
                "low": round(px - 0.02, 2),
                "window_start": int(t.tz_convert("UTC").value),
                "transactions": 1,
            }
        )
        t += pd.Timedelta(minutes=1)
        i += 1
    return rows


def _bars(cat_dir: Path, ident: str):
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    return ParquetDataCatalog(str(cat_dir)).query(data_cls=Bar, identifiers=[ident])


def _local_labels(bars) -> list[pd.Timestamp]:
    return [pd.Timestamp(b.ts_event, unit="ns", tz="UTC").tz_convert(_NY) for b in bars]


@pytest.fixture
def full_sessions(tmp_path: Path) -> tuple[Path, Path]:
    """(arşiv, katalog) — bir EDT ve bir EST tam seansı, aynı ticker."""
    src, cat = tmp_path / "flatfiles", tmp_path / "catalog"
    _write_day(src, _EDT_DAY, _session_rows("FULL", _EDT_DAY, 100.0))
    _write_day(src, _EST_DAY, _session_rows("FULL", _EST_DAY, 50.0))
    return src, cat


# ── 4) pre-market sızıntısı ──────────────────────────────────────────────────


def test_rth_session_has_exactly_390_bars_not_391(full_sessions):
    """Sızıntı olsaydı seansta 391 dakika olurdu → 5-MINUTE 79 bar verirdi."""
    src, cat = full_sessions
    ing.ingest({"FULL"}, "2026-2026", root=src, catalog_dir=cat)

    # 390 RTH dakikası 5'in tam katı: seans başına tam 78 bar, iki seans 156.
    five = _bars(cat, "FULL.NASDAQ-5-MINUTE-LAST-EXTERNAL")
    per_session = pd.Series([t.date() for t in _local_labels(five)]).value_counts()
    assert set(per_session) == {78}, dict(per_session)
    assert len(five) == 156

    # 1-MINUTE ham veriyi olduğu gibi tutar (pre-market dahil) — süzme yalnız
    # TF türetmede olur; RTH dışı dakikalar başka analizler için durmalı.
    assert len(_bars(cat, "FULL.NASDAQ-1-MINUTE-LAST-EXTERNAL")) == 2 * 391


def test_daily_open_comes_from_0931_not_the_premarket_minute(full_sessions):
    """Seansın açılışı 09:30–09:31 diliminin (yani "09:31" damgalı barın) open'ı."""
    src, cat = full_sessions
    ing.ingest({"FULL"}, "2026-2026", root=src, catalog_dir=cat)

    days = sorted(
        _bars(cat, "FULL.NASDAQ-1-DAY-LAST-EXTERNAL"), key=lambda b: b.ts_event
    )
    assert len(days) == 2
    # Kronolojik: önce EST (Ocak, base=50), sonra EDT (Temmuz, base=100).
    # İlk satır 09:29 dilimidir ve base fiyatı taşır — sızsaydı açılış 50.00 /
    # 100.00 olurdu; doğru açılış bir dakika sonrası.
    assert float(days[0].open) == 50.01
    assert float(days[1].open) == 100.01
    # Kapanış son bardan: 390. dakika artışı + 0.01 kapanış payı.
    assert float(days[1].close) == round(100.0 + 390 * 0.01 + 0.01, 2)


# ── 6) 4-HOUR DST'de kırılıyordu ─────────────────────────────────────────────


def test_4hour_gives_exactly_two_bars_in_both_edt_and_est(full_sessions):
    """Mutlak 240 dk kovalarında EDT'de 2, EST'de 3 bar çıkıyordu (biri 19:00)."""
    src, cat = full_sessions
    ing.ingest({"FULL"}, "2026-2026", root=src, catalog_dir=cat)

    labels = sorted(_local_labels(_bars(cat, "FULL.NASDAQ-4-HOUR-LAST-EXTERNAL")))
    per_session = pd.Series([t.date() for t in labels]).value_counts()
    assert set(per_session) == {2}, dict(per_session)

    # Etiketler seans-göreli: açılış+240 dk = 13:30, sonra seans sonu = 16:00.
    # Borsa kapandıktan sonrasına damgalanmış bar YOK.
    hhmm = sorted({t.strftime("%H:%M") for t in labels})
    assert hhmm == ["13:30", "16:00"]

    # İlk kova 240 dakikayı, ikincisi kalan 150'yi toplar (10 hacim/dakika).
    first = sorted(
        _bars(cat, "FULL.NASDAQ-4-HOUR-LAST-EXTERNAL"), key=lambda b: b.ts_event
    )
    assert [float(first[0].volume), float(first[1].volume)] == [2400.0, 1500.0]


def test_hourly_and_daily_resampling_is_left_untouched(full_sessions):
    """1-DAY = 1-HOUR'un tam agregası özelliği korunmalı (bilinçli olarak)."""
    src, cat = full_sessions
    ing.ingest({"FULL"}, "2026-2026", root=src, catalog_dir=cat)

    hours = _bars(cat, "FULL.NASDAQ-1-HOUR-LAST-EXTERNAL")
    days = _bars(cat, "FULL.NASDAQ-1-DAY-LAST-EXTERNAL")
    per_day: dict = {}
    for b, t in zip(hours, _local_labels(hours)):
        per_day.setdefault(t.date(), []).append(b)
    for d in days:
        # Günlük hacim, o seansın saatlik hacimlerinin toplamı.
        key = (
            pd.Timestamp(d.ts_event, unit="ns", tz="UTC").tz_convert(_NY)
            - pd.Timedelta(1, "ns")
        ).date()
        assert float(d.volume) == sum(float(h.volume) for h in per_day[key])
        assert float(d.high) == max(float(h.high) for h in per_day[key])


# ── 5) bozuk OHLC ────────────────────────────────────────────────────────────


def test_corrupt_bars_are_repaired_dropped_and_counted(tmp_path, capsys):
    """Gerçek vaka: low=0.00 barı. Onarılır; close<=0 satırı düşer; sayılır."""
    src, cat = tmp_path / "flatfiles", tmp_path / "catalog"
    rows = _session_rows("BAD", _EDT_DAY, 100.0)
    # 12:00 damgalı bar (window_start 11:59) — QQQ'daki low=0.00 ile aynı biçim.
    bad = next(r for r in rows if r["window_start"] == _ns_of(_EDT_DAY, "11:59"))
    bad["low"] = 0.0
    # high < max(open, close): onarılabilir ikinci kusur biçimi.
    crossed = next(r for r in rows if r["window_start"] == _ns_of(_EDT_DAY, "12:00"))
    crossed["high"] = crossed["open"] - 1.0
    # close = 0 → fiyat bilinmiyor, onarılamaz: satır DÜŞER.
    doomed = next(r for r in rows if r["window_start"] == _ns_of(_EDT_DAY, "12:01"))
    doomed["close"] = 0.0
    _write_day(src, _EDT_DAY, rows)

    stats = ing.ingest({"BAD"}, "2026-2026", root=src, catalog_dir=cat)
    out = capsys.readouterr().out
    assert "bozuk OHLC" in out  # sessiz onarım yok
    assert "1 satır düşürüldü" in out
    assert "2 satır onarıldı" in out

    # Düşen satır kataloğa hiç girmedi.
    assert stats["BAD"]["bars"] == 391 - 1
    mins = {b.ts_event: b for b in _bars(cat, "BAD.NASDAQ-1-MINUTE-LAST-EXTERNAL")}
    assert _ns_of(_EDT_DAY, "12:01") + ing._MIN_NS not in mins

    fixed = mins[_ns_of(_EDT_DAY, "11:59") + ing._MIN_NS]
    assert float(fixed.low) == min(float(fixed.open), float(fixed.close))
    assert float(fixed.low) > 0

    crossed_bar = mins[_ns_of(_EDT_DAY, "12:00") + ing._MIN_NS]
    assert float(crossed_bar.high) == max(
        float(crossed_bar.open), float(crossed_bar.close)
    )

    # En önemlisi: sıfır günlük bara SIZMADI (stop-loss'ları tetikleyen buydu).
    day = _bars(cat, "BAD.NASDAQ-1-DAY-LAST-EXTERNAL")[0]
    assert float(day.low) > 90.0


def test_sanitize_is_applied_on_the_rebuild_tf_path_too(tmp_path, capsys):
    """Katalogdaki dakikalıklar kirliyse `--rebuild-tf` onları TF'e taşımamalı."""
    import numpy as np

    df = pd.DataFrame(
        {
            "o": [10.0, 10.0, 10.0],
            "h": [10.5, 9.0, 10.5],  # 2. satır: high < max(open, close)
            "l": [0.0, 9.5, 9.5],  # 1. satır: low = 0.00
            "c": [10.2, 10.2, 10.2],
            "v": [1.0, 1.0, 1.0],
        }
    )
    clean, counts = ing._sanitize_ohlc(df, o="o", h="h", lo="l", c="c")
    assert counts == {"fixed": 2, "dropped": 0}
    assert list(clean["l"]) == [10.0, 9.5, 9.5]
    assert list(clean["h"]) == [10.5, 10.2, 10.5]
    assert "bozuk OHLC" in capsys.readouterr().out

    # Onarılamaz satırlar: close<=0 ve open=NaN.
    df2 = pd.DataFrame(
        {
            "o": [10.0, np.nan, 10.0],
            "h": [10.5, 10.5, 10.5],
            "l": [9.5, 9.5, 9.5],
            "c": [0.0, 10.2, 10.2],
            "v": [1.0, 1.0, 1.0],
        }
    )
    clean2, counts2 = ing._sanitize_ohlc(df2, o="o", h="h", lo="l", c="c")
    assert counts2["dropped"] == 2
    assert len(clean2) == 1


def _ns_of(day: str, hhmm: str) -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz=_NY).tz_convert("UTC").value)


# ── 7) manifest çelişkisi + temettü ──────────────────────────────────────────


def test_write_manifest_records_split_and_dividend_separately(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir()
    stats = {"XYZ": {"bars": 10, "first": "2020-01-02", "last": "2020-12-31"}}
    ing.write_manifest(
        stats,
        catalog_dir=cat,
        split_adjusted=True,
        dividend_adjusted=False,
        source="massive:rest_aggs",
    )
    entry = json.loads((cat / "_manifest.json").read_text(encoding="utf-8"))["XYZ"]
    assert entry["split_adjusted"] is True
    assert entry["dividend_adjusted"] is False
    assert entry["adjusted"] is True  # eski alan split ile eşitlenir (geri uyum)


def test_migrate_manifest_fixes_the_contradictory_dividend_note(tmp_path):
    """2026-08-03 partisi `adjusted=true` ile "split/temettü ayarlı" diyordu."""
    cat = tmp_path / "catalog"
    cat.mkdir()
    legacy = {
        "HOOD": {
            "symbol": "HOOD",
            "adjusted": True,
            "source": "massive:rest_aggs",
            "note": "Massive REST /v2/aggs ingest; split/temettü ayarlı (adjusted=true).",
        },
        "AAPL": {
            "symbol": "AAPL",
            "adjusted": True,
            "source": "massive:rest_aggs",
            "note": "Massive REST /v2/aggs ingest; yalnız split ayarlı (adjusted=true; temettü ayarlanmaz).",
        },
        "AA": {
            "symbol": "AA",
            "adjusted": False,
            "source": "flatfile:minute_aggs",
            "note": "Flat-file (UNADJUSTED) ingest.",
        },
        "MYSTERY": {"symbol": "MYSTERY", "adjusted": True, "source": "elden:gelme"},
    }
    (cat / "_manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    changed = ing.migrate_manifest(catalog_dir=cat)
    m = json.loads((cat / "_manifest.json").read_text(encoding="utf-8"))

    # Çelişki gerçeğe uyduruldu — artık iki partinin notu aynı şeyi söylüyor.
    assert "split/temettü ayarlı" not in m["HOOD"]["note"]
    assert "yalnız split ayarlı" in m["HOOD"]["note"]
    assert m["HOOD"]["split_adjusted"] is True
    assert m["HOOD"]["dividend_adjusted"] is False
    assert m["AAPL"]["dividend_adjusted"] is False
    assert m["AA"]["split_adjusted"] is False
    assert m["AA"]["dividend_adjusted"] is False
    # Bilinmeyen kaynakta UYDURMA yok: None kalır.
    assert m["MYSTERY"]["dividend_adjusted"] is None
    assert m["MYSTERY"]["split_adjusted"] is True
    assert "HOOD" in changed

    # İdempotent: ikinci koşum hiçbir şey değiştirmez.
    assert ing.migrate_manifest(catalog_dir=cat) == {}


def test_data_reader_surfaces_dividend_flag_and_falls_back_for_old_manifests(
    tmp_path, monkeypatch
):
    import data as data_mod

    cat = tmp_path / "catalog"
    (cat / "data" / "bar" / "OLD.NASDAQ-1-DAY-LAST-EXTERNAL").mkdir(parents=True)
    (cat / "data" / "bar" / "NEW.NASDAQ-1-DAY-LAST-EXTERNAL").mkdir(parents=True)
    (cat / "_manifest.json").write_text(
        json.dumps(
            {
                "OLD": {"adjusted": True},  # göç edilmemiş: temettü BİLİNMİYOR
                "NEW": {
                    "adjusted": True,
                    "split_adjusted": True,
                    "dividend_adjusted": False,
                    "coverage_gaps": [
                        {
                            "start": "2004-12-01",
                            "end": "2011-03-23",
                            "missing_sessions": 1588,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [cat])
    monkeypatch.setattr(data_mod, "_EXT_MANIFEST", {})

    inst = {r["instrument_id"]: r for r in data_mod.list_external_instruments()}
    assert inst["OLD.NASDAQ"]["split_adjusted"] is True
    assert inst["OLD.NASDAQ"]["dividend_adjusted"] is None  # uydurulmaz
    assert inst["OLD.NASDAQ"]["coverage_gaps"] == []
    assert inst["NEW.NASDAQ"]["dividend_adjusted"] is False
    assert inst["NEW.NASDAQ"]["coverage_gaps"][0]["missing_sessions"] == 1588
    # Eski tek bayraklı okuyucu = split bayrağı (mevcut çağıranlar kırılmaz).
    assert inst["NEW.NASDAQ"]["adjusted"] is True


# ── 8) kapsam boşlukları ─────────────────────────────────────────────────────


def _write_daily(cat_dir: Path, tk: str, days: list[pd.Timestamp]) -> None:
    """Doğrudan 1-DAY bar yaz — boşluk hesabı yalnız günlük seriyi okur."""
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cat_dir.mkdir(parents=True, exist_ok=True)
    cat = ParquetDataCatalog(str(cat_dir))
    cat.write_data([ing._equity(tk, "NASDAQ")])
    bt = BarType.from_str(f"{tk}.NASDAQ-1-DAY-LAST-EXTERNAL")
    bars = []
    for d in days:
        # Right-label sözleşmesi: 1-DAY damgası ertesi günün yerel gece yarısı.
        ts = int((d.normalize() + pd.Timedelta(days=1)).tz_convert("UTC").value)
        bars.append(
            Bar(
                bt,
                Price(10, 2),
                Price(11, 2),
                Price(9, 2),
                Price(10, 2),
                Quantity(1, 0),
                ts,
                ts,
            )
        )
    cat.write_data(bars)


def test_coverage_gaps_finds_the_hole_and_leaves_the_clean_series_alone(tmp_path):
    """QQQ'nun deliği görünür olmalı; dikilmiş QQQC temiz kalmalı."""
    cat = tmp_path / "catalog"
    calendar = list(pd.bdate_range("2004-01-01", "2012-12-31", tz=_NY))
    hole = [
        d
        for d in calendar
        if pd.Timestamp("2004-12-01", tz=_NY) <= d <= pd.Timestamp("2011-03-22", tz=_NY)
    ]

    _write_daily(cat, "QQQC", calendar)  # dikilmiş sürekli seri: delik yok
    _write_daily(cat, "QQQ", [d for d in calendar if d not in set(hole)])

    gaps = ing.compute_coverage_gaps(catalog_dir=cat)
    assert gaps["QQQC"] == []
    assert len(gaps["QQQ"]) == 1
    g = gaps["QQQ"][0]
    assert g["start"] == "2004-12-01"
    assert g["end"] == "2011-03-22"
    assert g["missing_sessions"] == len(hole)
    # 2008 ayı piyasası bu deliğin İÇİNDE — al-tut maxDD'yi gizleyen buydu.
    assert g["start"] < "2008-01-01" < g["end"]


def test_short_gaps_are_not_reported_and_edges_are_not_gaps(tmp_path):
    """Tatil/veri gürültüsü (<10 seans) ve serinin kendi başlangıcı boşluk değil."""
    cat = tmp_path / "catalog"
    calendar = list(pd.bdate_range("2020-01-01", "2020-12-31", tz=_NY))
    _write_daily(cat, "REF", calendar)
    # LATE yılın ortasında başlıyor (kapsam SINIRI) + 5 günlük küçük bir kopukluk.
    late = calendar[120:]
    small = set(late[40:45])
    _write_daily(cat, "LATE", [d for d in late if d not in small])

    gaps = ing.compute_coverage_gaps(catalog_dir=cat)
    assert gaps["REF"] == []
    assert gaps["LATE"] == []  # 5 seans < GAP_MIN_SESSIONS

    # Eşik düşürülünce aynı kopukluk görünür olur — susturulmuş değil, elenmiş.
    loose = ing.compute_coverage_gaps(catalog_dir=cat, min_sessions=3)
    assert len(loose["LATE"]) == 1
    assert loose["LATE"][0]["missing_sessions"] == 5


def test_write_coverage_gaps_lands_in_the_manifest(tmp_path):
    cat = tmp_path / "catalog"
    calendar = list(pd.bdate_range("2020-01-01", "2020-12-31", tz=_NY))
    hole = set(calendar[100:150])
    _write_daily(cat, "REF", calendar)
    _write_daily(cat, "HOLEY", [d for d in calendar if d not in hole])
    ing.write_manifest(
        {
            "REF": {"bars": 1, "first": "2020-01-01", "last": "2020-12-31"},
            "HOLEY": {"bars": 1, "first": "2020-01-01", "last": "2020-12-31"},
        },
        catalog_dir=cat,
    )

    ing.write_coverage_gaps(catalog_dir=cat)
    m = json.loads((cat / "_manifest.json").read_text(encoding="utf-8"))
    assert m["REF"]["coverage_gaps"] == []
    assert m["HOLEY"]["coverage_gaps"][0]["missing_sessions"] == 50

    # write_manifest tekrar koşarsa boşluklar SIFIRLANMAMALI (ayrı geçiş).
    ing.write_manifest(
        {"HOLEY": {"bars": 2, "first": "2020-01-01", "last": "2020-12-31"}},
        catalog_dir=cat,
    )
    m2 = json.loads((cat / "_manifest.json").read_text(encoding="utf-8"))
    assert m2["HOLEY"]["coverage_gaps"][0]["missing_sessions"] == 50


# ── mevcut veriyi yeniden üretme ─────────────────────────────────────────────


def test_rebuild_tf_regenerates_from_the_catalog_without_the_archive(full_sessions):
    """`--rebuild-tf`: arşiv silinse bile TF'ler dakikalıklardan yeniden doğar."""
    import shutil

    src, cat = full_sessions
    ing.ingest({"FULL"}, "2026-2026", root=src, catalog_dir=cat)
    before = len(_bars(cat, "FULL.NASDAQ-4-HOUR-LAST-EXTERNAL"))

    # TF'leri sil ve arşivi tamamen kaldır — kaynak yalnız katalog.
    shutil.rmtree(cat / "data" / "bar" / "FULL.NASDAQ-4-HOUR-LAST-EXTERNAL")
    shutil.rmtree(src)

    out = ing.rebuild_tf(catalog_dir=cat)  # hedef verilmedi → katalogdaki hepsi
    assert out["FULL"]["4-HOUR"] == before
    assert len(_bars(cat, "FULL.NASDAQ-4-HOUR-LAST-EXTERNAL")) == before
    # 1-MINUTE'e dokunulmadı.
    assert len(_bars(cat, "FULL.NASDAQ-1-MINUTE-LAST-EXTERNAL")) == 2 * 391
