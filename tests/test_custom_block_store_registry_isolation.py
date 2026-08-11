"""Memoize edilen registry görünümü çağıranlar arasında PAYLAŞILMAMALI.

DeepR 2026-08-11 [YÜKSEK]: `_read_registry()` parse edilmiş sözlüğü
`_REGISTRY_CACHE`'e koyup AYNI nesneyi döndürüyordu; `_registry_transaction()`,
`delete_custom()` ve `ensure_agent_role_metadata()` ise bu nesneyi YERİNDE
değiştiriyor. İki somut sonuç vardı:

  1. Diske yazmadan sonlanan bir read-modify-write (ör. `_write_registry`'de
     I/O hatası) dosyanın mtime/size'ını değiştirmediği için cache'i
     geçersizleştirmiyor, süreç ömrü boyunca DİSKLE ÇELİŞEN bir registry
     görünümü servis ediliyordu. `composer.load_catalog` bu görünümü katalog
     budamasında girdi olarak kullandığı için sonuç sessiz strateji kaybı —
     tam da modülün kendi docstring'inin önlemeye çalıştığı hata.
  2. `list_custom()` sözlüğü `_STORE_LOCK` DIŞINDA geziyordu; eşzamanlı bir
     yazıcı aynı nesneyi değiştirdiğinde `RuntimeError: dictionary changed
     size during iteration` alınırdı.

Bu dosya sızıntının her iki yüzünü de pinler. Testler gerçek dosya sistemine
(tmp_path) yazar; gerçek `~/.cache` store'una asla dokunulmaz.

Wiki References: [[nau_performans_denetimi]], [[deepr_skill]]
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """İzole bir store: STORE_DIR/REGISTRY_FILE tmp_path'e yönlendirilir.

    Cache anahtarı yolu da içerdiği için (bkz. `_REGISTRY_CACHE`) bu yönlendirme
    gerçek registry'nin cache'lenmiş içeriğini sızdıramaz.
    """
    import custom_block_store as cbs

    monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
    cbs.save_custom(
        "blk_one",
        {"label": "One", "role": "entry", "params": {"period": {"default": 14}}},
        "def evaluate(state, block, closes, indicators, portfolio):\n    return None\n",
    )
    return cbs


class TestReadRegistryReturnsAPrivateCopy:
    def test_two_reads_are_distinct_objects(self, store):
        """Aynı (değişmemiş) dosyadan iki okuma AYNI nesneyi döndürmemeli.

        Bu, cache'in yaşadığı ama paylaşılmadığı sözleşmesinin en kısa hâli:
        nesne kimliği eşitse çağıranlardan birinin mutasyonu diğerine sızar.
        """
        first = store._read_registry()
        second = store._read_registry()

        assert first == second, "cache aynı dosyadan farklı içerik verdi"
        assert first is not second, "paylaşılan registry nesnesi döndürüldü"

    def test_caller_mutation_does_not_leak_to_the_next_read(self, store):
        """Çağıranın yerinde mutasyonu bir sonraki okumada görünmemeli."""
        reg = store._read_registry()
        reg["ghost_block"] = {"meta": {}, "module_file": "ghost.py"}
        del reg["blk_one"]

        fresh = store._read_registry()
        assert "ghost_block" not in fresh, "diskte olmayan kayıt servis edildi"
        assert "blk_one" in fresh, "diskte var olan kayıt kaybedildi"

    def test_nested_meta_is_copied_too(self, store):
        """Kopya YÜZEYSEL değil derin olmalı.

        `get_custom()` sonucu `{"name": ..., **info}` ile yayılıyor; `meta`
        (ve içindeki `params`) yüzeysel kopyada hâlâ cache'teki nesneyi
        gösterirdi, yani bir çağıranın `meta["role"]` yazması diğerinin blok
        rolünü değiştirirdi — rol çelişkisi doğrulaması buna dayanıyor.
        """
        entry = store.get_custom("blk_one")
        entry["meta"]["role"] = "exit"
        entry["meta"]["params"]["period"]["default"] = 999

        fresh = store.get_custom("blk_one")
        assert fresh["meta"]["role"] == "entry"
        assert fresh["meta"]["params"]["period"]["default"] == 14

    def test_list_custom_iterates_a_private_copy(self, store):
        """`list_custom` kilit dışındaki döngüsünü kendi kopyası üzerinde döner.

        Eşzamanlı bir yazıcının elinde tuttuğu görünüme yaptığı ekleme burada
        ne sonuca sızmalı ne de iterasyonu bozmalı.
        """
        writer_view = store._read_registry()
        writer_view["mid_write_block"] = {"meta": {}, "module_file": "x.py"}

        names = [item["name"] for item in store.list_custom()]
        assert names == ["blk_one"]


class TestUnwrittenTransactionDoesNotPoisonTheCache:
    def test_failed_delete_keeps_serving_the_on_disk_registry(self, store, monkeypatch):
        """`_write_registry` patlarsa silme GERÇEKLEŞMEMİŞ sayılmalı.

        Eski davranış: `del reg[name]` cache'teki paylaşılan sözlüğü boşaltıyor,
        yazma hatası nedeniyle registry.json değişmiyor (mtime/size aynı), cache
        geçersizleşmiyor — süreç ömrü boyunca diskte DURAN bir blok "yok" olarak
        raporlanıyordu. `composer.load_catalog` bunu görüp o bloğa dayanan
        stratejileri katalogdan budardı.
        """
        on_disk_before = store.REGISTRY_FILE.read_text(encoding="utf-8")

        def _boom(_reg):
            raise OSError("disk full")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_write_registry", _boom)
            with pytest.raises(OSError):
                store.delete_custom("blk_one")

        assert store.REGISTRY_FILE.read_text(encoding="utf-8") == on_disk_before
        assert store.get_custom("blk_one") is not None, (
            "yazılmamış silme cache'i kirletti — diskteki blok kayıp göründü"
        )
        assert [b["name"] for b in store.list_custom()] == ["blk_one"]

    def test_failed_save_does_not_advertise_an_unwritten_block(
        self, store, monkeypatch
    ):
        """Yazılamayan bir kayıt registry görünümünde ASLA belirmemeli.

        Simetrik sızıntı: `reg[name] = {...}` paylaşılan sözlüğe yazıyor,
        `_write_registry` patlıyor, dosya değişmiyor → cache diskte olmayan bir
        bloğu servis ediyordu. Bu yönü daha sinsi: `composer` böyle bir bloğu
        import etmeye kalkar ve dosyayı bulamaz.
        """

        def _boom(_reg):
            raise OSError("disk full")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_write_registry", _boom)
            with pytest.raises(OSError):
                store.save_custom("blk_two", {"label": "Two", "params": {}}, "x = 1\n")

        assert store.get_custom("blk_two") is None, (
            "yazılmamış kayıt cache üzerinden servis edildi"
        )
        assert [b["name"] for b in store.list_custom()] == ["blk_one"]

    def test_failed_role_backfill_does_not_persist_in_memory(self, store, monkeypatch):
        """`ensure_agent_role_metadata` yazamazsa rol backfill'i görünmemeli."""
        store.save_custom(
            "agnt_e_abcd1234_0",
            {"label": "Agent entry", "params": {}},  # rol metadata'sı YOK
            "def evaluate(state, block, closes, indicators, portfolio):\n    return None\n",
        )

        def _boom(_reg):
            raise OSError("disk full")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_write_registry", _boom)
            with pytest.raises(OSError):
                store.ensure_agent_role_metadata("agnt_e_abcd1234_0")

        stored = store.get_custom("agnt_e_abcd1234_0")
        assert "role" not in (stored.get("meta") or {}), (
            "diske yazılamayan rol backfill'i bellekte kalıcı oldu"
        )


class TestCopyDoesNotDefeatTheCache:
    def test_unchanged_registry_is_still_read_from_disk_only_once(
        self, store, monkeypatch
    ):
        """Kopya döndürmek memoize'u iptal ETMEMELİ.

        Cache'in varlık sebebi diskten yeniden okuyup JSON parse etmemek
        (2026-08-08 DeepR [ORTA]); düzeltme bunu bozarsa custom bloklara
        dokunan her rota büyüyen bir dosyayı yeniden parse etmeye döner.
        """
        calls = {"n": 0}
        real_read_text = type(store.REGISTRY_FILE).read_text

        def _counting_read_text(self, *a, **k):
            calls["n"] += 1
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(type(store.REGISTRY_FILE), "read_text", _counting_read_text)

        store.list_custom()
        store.list_custom()
        store.list_custom()

        assert calls["n"] == 1, "değişmemiş registry.json diskten yeniden okundu"
