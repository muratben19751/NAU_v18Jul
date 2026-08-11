"""Harici katalog: geç bağlama + `(mtime, size)` geçersizleştirme + hata cache'lenmez.

DeepR 2026-08-11 [ORTA] — iki ayrı bulgu, tek konu:

1. **Import anında bağlama.** `EQUITY_CATALOG_DIR` listeye yalnız modül import
   edilirken ekleniyordu. `ingest_equities.py` / `download_massive.py` ayrı bir
   CLI süreci olarak kökü İLK KEZ oluşturursa, çalışan sunucu onu asla
   görmüyordu. Üstelik `_external_instrument_meta` / `_external_manifest`
   sonuçlarını SÜRESİZ tutuyordu ("read-only reference data, so it never
   changes" — bu varsayım projenin KENDİ ingest'inin yazdığı kök için yanlış).
   Sonuç tutarsız bir tabloydu: yeni ticker picker'da GÖRÜNÜYOR ama
   `split_adjusted`/`coverage_gaps` bayat manifest'ten okunuyor, yani UNADJUSTED
   uyarısı tam da yeni gelen veri için susuyordu.

2. **Geçici hata kalıcı cache'leniyor.** `_external_instrument_meta`'da
   `except Exception: meta = {}` sonucu cache'e giriyordu: açılıştaki tek bir
   dosya kilidi, süreç ömrü boyunca "bu enstrümanın metası yok" demeye
   dönüşüyordu. `composer.load_catalog`'un dersi — okunamıyorsa cevap
   "bilinmiyor", "boş" değil.

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[us_equity_katalog_veri_butunlugu]], [[instruments]]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import data


@pytest.fixture()
def isolated_roots(tmp_path, monkeypatch):
    """Gerçek katalogdan tamamen yalıtılmış kök listesi + boş önbellekler."""
    roots: list[Path] = []
    monkeypatch.setattr(data, "EXTERNAL_CATALOGS", roots)
    # Geç bağlama YALNIZ varsayılan liste yerindeyken çalışır; testte de aynı
    # nesneyi göstererek davranışı gerçekten çalıştırıyoruz (operatörün kendi
    # kataloğu değil, tmp kökü bağlanır).
    monkeypatch.setattr(data, "_EXTERNAL_CATALOGS_DEFAULT", roots)
    monkeypatch.setattr(data, "EQUITY_CATALOG_DIR", tmp_path / "equity_catalog")
    monkeypatch.setattr(data, "_EXT_MANIFEST", {})
    monkeypatch.setattr(data, "_EXT_INSTRUMENT_META", {})
    monkeypatch.setattr(data, "_EXT_META_WARNED", set())
    return tmp_path


def _make_bar_dir(root: Path, inst: str, gran: str = "1-DAY") -> Path:
    d = root / "data" / "bar" / f"{inst}-{gran}-LAST-EXTERNAL"
    d.mkdir(parents=True, exist_ok=True)
    (d / "part-0.parquet").write_bytes(b"x")
    return d


class TestLateBindingOfTheEquityCatalogRoot:
    def test_root_created_after_import_is_picked_up_without_restart(
        self, isolated_roots
    ):
        assert data.external_catalog_roots() == []
        assert data.list_external_instruments() == []

        # CLI ingest kökü ŞİMDİ oluşturuyor (ayrı süreç). Restart yok.
        _make_bar_dir(data.EQUITY_CATALOG_DIR, "NEWT.NASDAQ")

        assert data.external_catalog_roots() == [data.EQUITY_CATALOG_DIR]
        ids = [r["instrument_id"] for r in data.list_external_instruments()]
        assert ids == ["NEWT.NASDAQ"]

    def test_an_explicit_root_list_is_the_last_word(self, tmp_path, monkeypatch):
        """Çağıran listeyi bilinçli değiştirdiyse geç bağlama karışmaz —
        aksi hâlde her izole test operatörün gerçek kataloğunu okurdu."""
        other = tmp_path / "other"
        _make_bar_dir(other, "ZZ.NASDAQ")
        monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [other])
        monkeypatch.setattr(data, "EQUITY_CATALOG_DIR", tmp_path / "equity_catalog")
        (tmp_path / "equity_catalog").mkdir()

        assert data.external_catalog_roots() == [other]


class TestManifestCacheInvalidation:
    def test_a_rewritten_manifest_is_re_read(self, isolated_roots):
        root = data.EQUITY_CATALOG_DIR
        _make_bar_dir(root, "NEWT.NASDAQ")
        manifest = root / "_manifest.json"

        assert data._external_manifest(root) == {}  # henüz yok

        manifest.write_text(json.dumps({"NEWT": {"symbol": "NEWT", "adjusted": False}}))
        assert data._external_manifest(root)["NEWT"]["adjusted"] is False

        # write_manifest bir koşumun SONUNDA dosyayı yeniden yazar.
        time.sleep(0.01)
        manifest.write_text(json.dumps({"NEWT": {"symbol": "NEWT", "adjusted": True}}))
        assert data._external_manifest(root)["NEWT"]["adjusted"] is True

    def test_unadjusted_badge_follows_a_fresh_manifest(self, isolated_roots):
        """Bulgunun somut sonucu: UNADJUSTED rozeti yeni veride susuyordu."""
        root = data.EQUITY_CATALOG_DIR
        _make_bar_dir(root, "NEWT.NASDAQ")
        (root / "_manifest.json").write_text(json.dumps({}))
        assert data.list_external_instruments()[0]["split_adjusted"] is None

        time.sleep(0.01)
        (root / "_manifest.json").write_text(
            json.dumps({"NEWT": {"symbol": "NEWT", "split_adjusted": False}})
        )
        assert data.list_external_instruments()[0]["split_adjusted"] is False

    def test_transient_io_error_is_not_cached(self, isolated_roots, monkeypatch):
        root = data.EQUITY_CATALOG_DIR
        root.mkdir(parents=True)
        (root / "_manifest.json").write_text(json.dumps({"NEWT": {"symbol": "NEWT"}}))

        real_read_text = Path.read_text
        boom = {"on": True}

        def _flaky(self, *a, **k):
            if boom["on"] and self.name == "_manifest.json":
                raise PermissionError("locked by another process")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _flaky)
        assert data._external_manifest(root) == {}

        boom["on"] = False
        # Hata cache'lenmediği için ikinci çağrı gerçeği görür.
        assert "NEWT" in data._external_manifest(root)


class TestInstrumentMetaDoesNotCacheFailures:
    def test_a_one_off_read_error_is_retried_on_the_next_call(
        self, isolated_roots, monkeypatch
    ):
        root = data.EQUITY_CATALOG_DIR
        _make_bar_dir(root, "NEWT.NASDAQ")
        (root / "data" / "equity" / "NEWT.NASDAQ").mkdir(parents=True)

        calls = {"n": 0}

        class _Inst:
            id = "NEWT.NASDAQ"
            asset_class = 0
            price_precision = 2
            size_precision = 0
            price_increment = "0.01"
            quote_currency = "USD"

        class _Catalog:
            def __init__(self, _root):
                pass

            def instruments(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError("file locked at startup")
                return [_Inst()]

        import nautilus_trader.persistence.catalog as _cat_mod

        monkeypatch.setattr(_cat_mod, "ParquetDataCatalog", _Catalog)
        monkeypatch.setattr(
            "nautilus_trader.model.enums.asset_class_to_str", lambda _v: "EQUITY"
        )

        assert data._external_instrument_meta(root) == {}  # boş = "bilinmiyor"
        meta = data._external_instrument_meta(root)  # SÜREÇ ÖMRÜ boyunca değil
        assert meta["NEWT.NASDAQ"]["price_precision"] == 2
        assert calls["n"] == 2

    def test_a_successful_read_is_cached_until_the_catalog_changes(
        self, isolated_roots, monkeypatch
    ):
        root = data.EQUITY_CATALOG_DIR
        _make_bar_dir(root, "NEWT.NASDAQ")
        (root / "data" / "equity" / "NEWT.NASDAQ").mkdir(parents=True)

        calls = {"n": 0}

        class _Inst:
            def __init__(self, ident):
                self.id = ident
                self.asset_class = 0
                self.price_precision = 2
                self.size_precision = 0
                self.price_increment = "0.01"
                self.quote_currency = "USD"

        class _Catalog:
            def __init__(self, _root):
                pass

            def instruments(self):
                calls["n"] += 1
                return [_Inst(f"I{calls['n']}.NASDAQ")]

        import nautilus_trader.persistence.catalog as _cat_mod

        monkeypatch.setattr(_cat_mod, "ParquetDataCatalog", _Catalog)
        monkeypatch.setattr(
            "nautilus_trader.model.enums.asset_class_to_str", lambda _v: "EQUITY"
        )

        first = data._external_instrument_meta(root)
        assert data._external_instrument_meta(root) is first  # imza aynı → cache
        assert calls["n"] == 1

        # Yeni bir enstrüman ingest edildi: `data/equity/` mtime'ı değişir.
        time.sleep(0.01)
        (root / "data" / "equity" / "OTHER.NASDAQ").mkdir()
        second = data._external_instrument_meta(root)
        assert second is not first
        assert calls["n"] == 2
