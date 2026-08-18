"""Bozuk katalog kayıtları artık sessizce düşmüyor: log + karantina + arayüz.

DeepR 2026-08-11 [YÜKSEK]: `composer.load_catalog` her kayıt için
`except Exception: n_broken += 1` yapıyordu ve `n_broken` YALNIZCA yeniden-yazma
korumasında kullanılıyordu — hiçbir yere loglanmıyor, çağırana bildirilmiyor,
UI'a taşınmıyordu. Kullanıcı 30 stratejiden 28'ini görüyor ve bunu fark
edecek tek bir iz bile bulamıyordu. Asimetri çıplaktı: hemen üstteki
custom-block registry okuma hatası düzgün `logging.warning` basıyordu.

Üstelik kayıp kalıcı olabiliyordu: `append_to_catalog` = load→append→save,
yani bir kayıt parse edilemediği turda kaydetme yapılırsa o kayıt DİSKTEN de
siliniyordu. Karantina dosyası tam olarak bunun için var — ham JSON kaydı,
gerekçesiyle birlikte, `<katalog dizini>/quarantine/` altına yazılır.

Wiki References: [[nau_deepr_dorduncu_tur_2026_08_11]]
"""

from __future__ import annotations

import json
import logging

import pytest


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Kataloğu tmp_path'e taşı — gerçek ~/.cache dosyasına ASLA dokunma.

    `_CATALOG_RAW_CACHE` (mtime,size) ile memoize ediyor ve `_CATALOG_ISSUES`
    aynı anahtarla tekrar-yazmayı engelliyor; ikisi de test başına
    sıfırlanmalı, yoksa testler birbirine sızar.
    """
    import composer

    path = tmp_path / "strategy_catalog.json"
    monkeypatch.setattr(composer, "CATALOG_FILE", path)
    monkeypatch.setattr(composer, "_CATALOG_RAW_CACHE", None)
    monkeypatch.setattr(composer, "_CATALOG_ISSUES", None)
    return composer, path


_GOOD = {
    "id": "good-1",
    "name": "Good Strategy",
    "description": "",
    "blocks": [
        {
            "type": "ma_cross",
            "role": "entry",
            "params": {"fast": 5, "slow": 20, "direction": "up"},
        }
    ],
    "trade_size": 0.1,
    "created_at": "2026-01-01T00:00:00+00:00",
}
# Şema kayması: blok sözlüğünde bilinmeyen bir alan → `SignalBlock(**b)`
# TypeError atar → `from_dict` patlar. Bulgunun tarif ettiği senaryonun aynısı
# ("bilinmeyen alan/şema kayması yüzünden from_dict'te patlarsa").
_BROKEN = {
    "id": "broken-1",
    "name": "Broken Strategy",
    "description": "",
    "blocks": [
        {
            "type": "ma_cross",
            "role": "entry",
            "params": {},
            "a_field_from_a_future_schema": 42,
        }
    ],
    "created_at": "2026-01-01T00:00:00+00:00",
}


def _write(path, records):
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


class TestBrokenRecordsAreReported:
    def test_broken_record_is_logged_with_its_reason(self, catalog, caplog):
        """Sayı değil, GEREKÇE loglanmalı: "kaç tane" kullanıcıya yetmiyor."""
        composer, path = catalog
        _write(path, [_GOOD, _BROKEN])

        with caplog.at_level(logging.WARNING, logger="composer"):
            specs = composer.load_catalog()

        assert [s.id for s in specs] == ["good-1"], "sağlam kayıt yine yüklenmeli"
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "1 of 2 record(s) could not be loaded" in text
        assert "broken-1" in text
        assert "TypeError" in text  # from_dict'in gerçek hatası

    def test_broken_record_is_quarantined_with_its_raw_json(self, catalog):
        """Kayıt SİLİNMEZ, bir kenara yazılır.

        `append_to_catalog` bir sonraki kaydetmede bozuk kaydı diskten
        düşürebilir; karantina dosyası ham JSON'u koruduğu için kayıp geri
        alınabilir olur.
        """
        composer, path = catalog
        _write(path, [_GOOD, _BROKEN])
        composer.load_catalog()

        qdir = path.parent / "quarantine"
        files = list(qdir.glob("strategy_catalog.broken-records.*.json"))
        assert len(files) == 1, f"tek bir karantina dosyası bekleniyordu: {files}"
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["n_broken"] == 1
        assert payload["records"][0]["id"] == "broken-1"
        # Ham kayıt birebir korunmuş olmalı — kurtarmanın tek yolu bu.
        assert payload["records"][0]["record"] == _BROKEN
        assert "SKIPPED" in payload["reason"]

    def test_summary_is_exposed_to_the_ui(self, catalog):
        """`last_catalog_load_issues()` arayüzün okuduğu tek kaynak."""
        composer, path = catalog
        _write(path, [_GOOD, _BROKEN, dict(_BROKEN, id="broken-2")])
        composer.load_catalog()

        issues = composer.last_catalog_load_issues()
        assert issues is not None
        assert issues["n_broken"] == 2
        assert issues["n_total"] == 3
        assert issues["reasons"] and all(isinstance(r, str) for r in issues["reasons"])
        assert issues["quarantine_path"]

    def test_clean_catalog_reports_nothing(self, catalog):
        composer, path = catalog
        _write(path, [_GOOD])
        assert len(composer.load_catalog()) == 1
        assert composer.last_catalog_load_issues() is None
        assert not (path.parent / "quarantine").exists()

    def test_hot_path_does_not_spam_quarantine_files(self, catalog):
        """`load_catalog` ~18 sıcak yolda çağrılıyor.

        Aynı bozuk dosya için her çağrıda yeni bir karantina dosyası yazmak
        (ve aynı uyarıyı basmak) sinyali gürültüye çevirirdi; kayıt
        (mtime,size) anahtarıyla bir kez alınır.
        """
        composer, path = catalog
        _write(path, [_GOOD, _BROKEN])
        for _ in range(5):
            composer.load_catalog()
        files = list((path.parent / "quarantine").glob("*.json"))
        assert len(files) == 1

    def test_stale_summary_disappears_once_the_catalog_is_fixed(self, catalog):
        """Düzeltilmiş dosyanın üstünde eski uyarıyı göstermek de yanıltıcıdır."""
        composer, path = catalog
        _write(path, [_GOOD, _BROKEN])
        composer.load_catalog()
        assert composer.last_catalog_load_issues() is not None

        _write(path, [_GOOD])
        composer._CATALOG_RAW_CACHE = None
        composer.load_catalog()
        assert composer.last_catalog_load_issues() is None

    def test_broken_record_still_blocks_the_pruning_rewrite(self, catalog):
        """M1342 koruması bozulmadı: bozuk kayıt varken katalog YENİDEN YAZILMAZ.

        Aksi hâlde geçerli-ama-elenen kayıtların kaydedilmesi bozuk kaydı
        kalıcı olarak silerdi — düzeltmenin bu davranışı değiştirmediğini
        sabitliyoruz.
        """
        composer, path = catalog
        # Bilinmeyen blok tipi → parse OK ama _catalog_is_valid False (elenir).
        invalid_block = dict(
            _GOOD,
            id="unknown-block",
            blocks=[{"type": "no_such_block_xyz", "role": "entry", "params": {}}],
        )
        _write(path, [_GOOD, invalid_block, _BROKEN])
        before = path.read_text(encoding="utf-8")
        composer.load_catalog()
        assert path.read_text(encoding="utf-8") == before


class TestCatalogListBannerRenders:
    """Bilgi arayüze GERÇEKTEN ulaşıyor mu — şablon seviyesinde kanıt."""

    def _render(self, **ctx) -> str:
        from server import templates

        return templates.get_template("fragments/catalog_list.html").render(
            request=None, catalog=[], **ctx
        )

    def test_banner_shows_count_reason_and_quarantine_path(self, monkeypatch):
        import server

        monkeypatch.setitem(
            server.templates.env.globals,
            "catalog_issues",
            lambda: {
                "n_broken": 2,
                "n_total": 30,
                "reasons": ["TypeError: unexpected keyword argument 'future_field'"],
                "quarantine_path": r"C:\cache\quarantine\strategy_catalog.broken.json",
                "catalog_path": r"C:\cache\strategy_catalog.json",
            },
        )
        html = self._render()
        assert "2 of 30 saved strategies could not be loaded" in html
        assert "not deleted" in html
        assert "future_field" in html
        assert "quarantine" in html

    def test_no_banner_when_the_catalog_is_clean(self, monkeypatch):
        import server

        monkeypatch.setitem(
            server.templates.env.globals, "catalog_issues", lambda: None
        )
        html = self._render()
        assert "could not be loaded" not in html
