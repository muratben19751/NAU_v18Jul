"""download_massive: ağ hatası MEVCUT katalog verisini yok etmemeli.

DeepR 2026-08-11 [ORTA]: `download_minute_bars` ilk API çağrısından ÖNCE tüm
hedef ticker'ların bar dizinlerini siliyordu. Sonrasında `_get` 6 denemede
yanıt alamazsa, yıl `NOT_AUTHORIZED` dönerse (ücretsiz plan penceresi dışında
— modülün KENDİ docstring'inde beklenen bir durum) ya da ticker
`MassiveError` ile atlanırsa, silme çoktan olmuştu: koşum "hiçbir ticker'da bar
yok" deyip çıkıyor ve önceden başarıyla ingest edilmiş veri KALICI olarak
kayboluyordu. Bu kök aynı zamanda web uygulamasının okuduğu
`data.EQUITY_CATALOG_DIR`; sonuç olarak enstrüman /data panelinden, Lab/Studio
picker'larından ve o sembolü kullanan kayıtlı stratejilerden düşüyordu. Yanlış
bir `--years` aralığı ya da süresi dolmuş bir anahtarla tek koşum yetiyordu.

Düzeltme kardeş modüllerin zaten kullandığı desen: yeni veri YAN TARAFA yazılır
(staging kökü) ve ancak ticker başarıyla, en az bir bar'la bittiğinde atomik
olarak takas edilir — `repair_massive_intraday._replace_day` `.bak` tutuyor,
`download_flatfiles` atomik `.part→replace` kullanıyor.

Wiki References
---------------
Bkz: [[us_equity_katalog_veri_butunlugu]], [[parquet_data_catalog]]
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import data as data_mod
import download_massive as dl

_DAY = "2026-07-06"
_NY = "America/New_York"


def _ms(day: str, hhmm: str) -> int:
    return int(
        pd.Timestamp(f"{day} {hhmm}", tz=_NY).tz_convert("UTC").value // 1_000_000
    )


def _row(hhmm: str, price: float) -> dict:
    return {
        "t": _ms(_DAY, hhmm),
        "o": price,
        "h": price,
        "l": price,
        "c": price,
        "v": 10,
    }


_ROWS = [_row("09:30", 10.0), _row("09:31", 10.2), _row("15:59", 10.4)]


@pytest.fixture()
def catalog(tmp_path: Path, monkeypatch) -> Path:
    cat = tmp_path / "equity_catalog"
    monkeypatch.setattr(data_mod, "EXTERNAL_CATALOGS", [cat])
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    return cat


def _seed_existing(catalog: Path, ticker: str = "NEWT") -> None:
    """Önceden başarıyla ingest edilmiş bir ticker — korunması gereken veri."""
    dl.download(
        {ticker},
        pd.Timestamp("2026-07-01").date(),
        pd.Timestamp("2026-07-31").date(),
        catalog_dir=catalog,
    )


def _minute_bars(catalog: Path, ticker: str = "NEWT"):
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    return ParquetDataCatalog(str(catalog)).query(
        data_cls=Bar, identifiers=[f"{ticker}.NASDAQ-1-MINUTE-LAST-EXTERNAL"]
    )


class TestExistingDataSurvivesAFailedRun:
    def test_network_failure_does_not_delete_the_previous_ingest(
        self, catalog, monkeypatch
    ):
        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {"status": "OK", "results": _ROWS},
        )
        _seed_existing(catalog)
        assert len(_minute_bars(catalog)) == 3

        def _dead(url, key, pacer, tries=6):
            raise dl.MassiveError("6 denemede yanıt alınamadı (timeout)")

        monkeypatch.setattr(dl, "_get", _dead)
        stats = dl.download(
            {"NEWT"},
            pd.Timestamp("2026-07-01").date(),
            pd.Timestamp("2026-07-31").date(),
            catalog_dir=catalog,
        )

        assert stats["NEWT"]["bars"] == 0
        assert len(_minute_bars(catalog)) == 3, "eski veri silinmiş"
        # /data paneli ve picker'lar hâlâ enstrümanı görüyor.
        ids = [r["instrument_id"] for r in data_mod.list_external_instruments()]
        assert "NEWT.NASDAQ" in ids

    def test_plan_window_miss_keeps_the_previous_ingest(self, catalog, monkeypatch):
        """`NOT_AUTHORIZED` hata DEĞİL, beklenen bir atlama — ama eski veriyi
        yine de siliyordu."""
        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {"status": "OK", "results": _ROWS},
        )
        _seed_existing(catalog)

        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {
                "status": "NOT_AUTHORIZED",
                "message": "Your plan doesn't include this timeframe",
            },
        )
        dl.download(
            {"NEWT"},
            pd.Timestamp("2019-01-01").date(),
            pd.Timestamp("2019-12-31").date(),
            catalog_dir=catalog,
        )

        assert len(_minute_bars(catalog)) == 3

    def test_one_failing_ticker_does_not_take_down_a_healthy_one(
        self, catalog, monkeypatch
    ):
        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {"status": "OK", "results": _ROWS},
        )
        _seed_existing(catalog, "NEWT")
        _seed_existing(catalog, "OKAY")

        def _newt_fails(url, key, pacer, tries=6):
            if "/NEWT/" in url:
                raise dl.MassiveError("429 kotası tükendi")
            return {"status": "OK", "results": _ROWS}

        monkeypatch.setattr(dl, "_get", _newt_fails)
        dl.download(
            {"NEWT", "OKAY"},
            pd.Timestamp("2026-07-01").date(),
            pd.Timestamp("2026-07-31").date(),
            catalog_dir=catalog,
        )

        assert len(_minute_bars(catalog, "NEWT")) == 3  # korundu
        assert len(_minute_bars(catalog, "OKAY")) == 3  # yenilendi

    def test_a_successful_rerun_still_replaces_stale_bars(self, catalog, monkeypatch):
        """Takas SİLMENİN yerini alır: başarılı koşum eski barları BIRAKMAZ,
        yoksa yeniden indirme idempotent olmaktan çıkar."""
        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {"status": "OK", "results": _ROWS},
        )
        _seed_existing(catalog)

        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {
                "status": "OK",
                "results": [_row("09:30", 99.0)],
            },
        )
        dl.download(
            {"NEWT"},
            pd.Timestamp("2026-07-01").date(),
            pd.Timestamp("2026-07-31").date(),
            catalog_dir=catalog,
        )

        bars = _minute_bars(catalog)
        assert len(bars) == 1
        assert float(bars[0].close) == 99.0

    def test_no_staging_leftovers_beside_the_catalog(self, catalog, monkeypatch):
        monkeypatch.setattr(
            dl,
            "_get",
            lambda url, key, pacer, tries=6: {"status": "OK", "results": _ROWS},
        )
        _seed_existing(catalog)

        leftovers = [p.name for p in catalog.parent.iterdir() if "staging" in p.name]
        assert leftovers == []
        # Katalog kökünde de artık yok: `list_external_instruments` bar dizini
        # adlarını rsplit("-", 4) ile ayrıştırıyor, yanlış yere bırakılmış bir
        # yedek/staging dizini SAHTE bir enstrüman olarak görünürdü.
        assert sorted(p.name for p in catalog.iterdir()) == ["_manifest.json", "data"]
