"""_external_catalog_rows() TTL önbelleği (DeepR 2026-08-11 [ORTA]).

Kardeş paneller `_bybit_rows` (2026-08-08) ve `_index_rows` (2026-08-09) tam
da bu maliyet için 30 sn'lik TTL'e kavuşturulmuştu; external paneli düzeltmenin
dışında kalmıştı. Her /data GET'inde (a) her katalog kökünün `data/bar` dizini
baştan `iterdir()` ediliyor, (b) gösterilen enstrümanların HER parquet dosyası
için `read_metadata` çağrılıyordu. Ölçüldü (16 enstrüman / 293 parquet):
çağrı başına 81 ms, tamamı tekrar eden disk I/O'su; gerçek masaüstü kataloğu
592 enstrüman.

Aynı desen uygulandı, iki katman tek pencerede: discovery (dizin taraması) +
TEMBEL satırlar (yalnız gösterilen enstrümanın footer'ları).

Wiki References
---------------
Bkz: [[nau_performans_denetimi]], [[parquet_data_catalog]]
"""

from __future__ import annotations

import pytest

import data


@pytest.fixture(autouse=True)
def _reset_cache():
    data._invalidate_external_rows_cache()
    yield
    data._invalidate_external_rows_cache()


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    """İki enstrümanlı sahte harici katalog — gerçek katalogdan yalıtık."""
    root = tmp_path / "cat"
    for inst in ("AAA.NASDAQ", "BBB.NASDAQ"):
        d = root / "data" / "bar" / f"{inst}-1-DAY-LAST-EXTERNAL"
        d.mkdir(parents=True)
        (d / "0_1.parquet").write_bytes(b"x")
    monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [root])
    monkeypatch.setattr(data, "_EXTERNAL_CATALOGS_DEFAULT", [root])
    monkeypatch.setattr(data, "EQUITY_CATALOG_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(data, "EXTERNAL_CACHE_DIR", tmp_path / "extcache")
    monkeypatch.setattr(data, "_external_instrument_meta", lambda r: {})
    monkeypatch.setattr(data, "_external_adjustment_flags", lambda r, i: (None, None))
    monkeypatch.setattr(data, "_external_coverage_gaps", lambda r, i: [])
    return root


def _count_row_builds(monkeypatch) -> list[str]:
    built: list[str] = []
    real = data._external_catalog_row

    def _tracking(inst_id, entry, used):
        built.append(inst_id)
        return real(inst_id, entry, used)

    monkeypatch.setattr(data, "_external_catalog_row", _tracking)
    return built


def _count_discoveries(monkeypatch) -> list[int]:
    scans: list[int] = []
    real = data._external_catalog_discovery

    def _tracking():
        scans.append(1)
        return real()

    monkeypatch.setattr(data, "_external_catalog_discovery", _tracking)
    return scans


class TestTtlCaching:
    def test_hit_a_second_page_load_rescans_nothing(self, catalog, monkeypatch):
        data._external_catalog_rows(limit=50)  # pencereyi aç
        scans = _count_discoveries(monkeypatch)
        built = _count_row_builds(monkeypatch)

        second = data._external_catalog_rows(limit=50)

        assert scans == [], "dizin taraması pencere içinde tekrarlandı"
        assert built == [], "parquet footer'ları pencere içinde yeniden okundu"
        assert [r["key"] for r in second["rows"]] == ["AAA.NASDAQ", "BBB.NASDAQ"]

    def test_miss_the_window_expires_and_everything_is_rescanned(
        self, catalog, monkeypatch
    ):
        data._external_catalog_rows(limit=50)
        monkeypatch.setattr(data, "_EXTERNAL_ROWS_TTL_S", 0.0)
        scans = _count_discoveries(monkeypatch)
        built = _count_row_builds(monkeypatch)

        data._external_catalog_rows(limit=50)

        assert len(scans) == 1
        assert sorted(built) == ["AAA.NASDAQ", "BBB.NASDAQ"]

    def test_invalidate_forces_an_immediate_rescan(self, catalog, monkeypatch):
        data._external_catalog_rows(limit=50)
        data._invalidate_external_rows_cache()
        scans = _count_discoveries(monkeypatch)

        data._external_catalog_rows(limit=50)

        assert len(scans) == 1

    def test_an_expired_window_shows_a_newly_ingested_ticker(
        self, catalog, monkeypatch
    ):
        """Bayatlama sözleşmesinin görünür yüzü: yeni veri bir pencere sonra gelir."""
        first = data._external_catalog_rows(limit=50)
        assert first["total"] == 2

        d = catalog / "data" / "bar" / "CCC.NASDAQ-1-DAY-LAST-EXTERNAL"
        d.mkdir(parents=True)
        (d / "0_1.parquet").write_bytes(b"x")

        assert data._external_catalog_rows(limit=50)["total"] == 2  # pencere içinde
        monkeypatch.setattr(data, "_EXTERNAL_ROWS_TTL_S", 0.0)
        assert data._external_catalog_rows(limit=50)["total"] == 3


class TestRootChangesBypassTheWindow:
    def test_a_root_that_appears_mid_window_is_not_hidden_for_30s(
        self, catalog, tmp_path, monkeypatch
    ):
        """`EQUITY_CATALOG_DIR` geç bağlanır: CLI ingest kökü İLK KEZ
        oluşturursa TTL'i beklemek onu 30 sn görünmez yapardı."""
        assert data._external_catalog_rows(limit=50)["total"] == 2

        late = tmp_path / "late"
        d = late / "data" / "bar" / "LATE.NASDAQ-1-DAY-LAST-EXTERNAL"
        d.mkdir(parents=True)
        (d / "0_1.parquet").write_bytes(b"x")
        monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [catalog, late])
        monkeypatch.setattr(data, "_EXTERNAL_CATALOGS_DEFAULT", [catalog, late])

        assert data._external_catalog_rows(limit=50)["total"] == 3


class TestLazyRowBuilding:
    def test_only_the_limited_page_pays_for_parquet_metadata(
        self, catalog, monkeypatch
    ):
        built = _count_row_builds(monkeypatch)

        out = data._external_catalog_rows(limit=1)

        assert built == ["AAA.NASDAQ"]
        assert out["total"] == 2 and out["matched"] == 2
        assert len(out["rows"]) == 1

    def test_paging_further_only_builds_the_newly_visible_instrument(
        self, catalog, monkeypatch
    ):
        data._external_catalog_rows(limit=1)
        built = _count_row_builds(monkeypatch)

        out = data._external_catalog_rows(limit=2)

        assert built == ["BBB.NASDAQ"], "zaten kurulmuş satır yeniden kuruldu"
        assert [r["key"] for r in out["rows"]] == ["AAA.NASDAQ", "BBB.NASDAQ"]

    def test_query_filters_before_the_io(self, catalog, monkeypatch):
        built = _count_row_builds(monkeypatch)

        out = data._external_catalog_rows(query="bbb")

        assert built == ["BBB.NASDAQ"]
        assert out["matched"] == 1 and out["total"] == 2


class TestNoCatalogAtAll:
    def test_missing_roots_are_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [tmp_path / "nope"])
        monkeypatch.setattr(data, "_EXTERNAL_CATALOGS_DEFAULT", [tmp_path / "nope"])
        monkeypatch.setattr(data, "EQUITY_CATALOG_DIR", tmp_path / "also-nope")

        out = data._external_catalog_rows(limit=50)

        assert out == {"rows": [], "total": 0, "matched": 0}
