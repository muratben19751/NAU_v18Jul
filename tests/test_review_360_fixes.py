"""360° inceleme turunun (2026-08-17) doğrulanan üç bulgusu.

Raporun 11 maddesinden 4'ü koda karşı doğrulandı; biri (`sandbox` bellek tavanı)
kendi dosyasında pinlendi (`test_sandbox_memory_ceiling.py`), kalan üçü burada:

1. **`compiler.compile_strategy`** — `[i for i in ... if i.active] or
   defn.instruments`: hiçbiri aktif değilken `or` TÜM enstrümanları (pasifleri
   dahil) derlemeye sokuyordu. Sessiz değil, TERS yönde konuşuyordu: `deploy.py`
   bu daraltmaya güvenerek artefaktı yazıyor, `graph.py` ise fallback'siz
   filtrelediği için Canvas BOŞ görünüyordu — ekran "hiçbir şey işlem görmüyor"
   derken artefakt üçünü birden listeliyordu.

2. **`store.live_deployments` + `runner.reconcile_orphans`** — devralma
   (`_runner_pickup`) yanıt gönderildikten SONRA koşuyor; o birkaç saniyede
   süreç ölürse satır sonsuza dek `pending` kalıyor ve açılış uzlaştırması onu
   hiç görmüyordu. Elle kurtarma vardı (Stop `pending`'i kabul ediyor), kendi
   kendine düzelme yoktu.

3. **`custom_block_store.delete_custom`** — `unlink()` sonra AYRI bir
   `_write_registry()`: arada bir I/O hatası dosyayı silinmiş, kaydı yerinde
   bırakıyordu. Kardeşi `delete_custom_batch` 2026-08-09'dan beri
   `_registry_transaction` kullanıyordu; tekil silme geride kalan son yoldu.

Testler tmp dizinlere yazar; gerçek `studio.db`'ye ve gerçek blok store'una
asla dokunulmaz.

Wiki References
---------------
See: [[strategy_studio]], [[webapp_module_map]], [[nau_soz_verip_yapmayan_yollar_2026_08_17]]
"""

from __future__ import annotations

import pytest

SID = "wt-funding-v3"


# ── 1. compile_strategy: pasif enstrüman pasif kalmalı ────────────────────


def _defn_with_active(flags: list[bool]):
    """Seed fixture'ı, enstrüman aktiflikleri verilen kalıba çekilmiş hâlde.

    Değişiklik `model_dump` → `model_validate` üzerinden yapılıyor, elle alan
    set ederek değil: gerçek vektör tam da bu — diskteki/AI'nin ürettiği JSON'ı
    `store` bu yolla nesneye çeviriyor ve `InstrumentConfig.active` VARSAYILANI
    False olduğu için alanı hiç yazmayan bir kayıt buradan hepsi-pasif çıkıyor.
    """
    from scripts.seed_studio import build_fixture
    from strategy_studio.schema import StrategyDefinition

    raw = build_fixture().model_dump(mode="json")
    for inst, flag in zip(raw["instruments"], flags, strict=True):
        inst["active"] = flag
    return StrategyDefinition.model_validate(raw)


class TestCompilerNarrowsToActiveInstruments:
    def test_all_inactive_is_refused_instead_of_silently_meaning_all(self):
        """Hiçbiri aktif değilse derleme DURMALI.

        Eski davranış: `or defn.instruments` devreye girer, üç pasif enstrüman
        derlenmiş artefakta işlem görecek diye yazılırdı.
        """
        from strategy_studio.compiler import CompileError, compile_strategy

        defn = _defn_with_active([False, False, False])

        with pytest.raises(CompileError) as err:
            compile_strategy(defn)

        assert "none is active" in str(err.value)

    def test_the_message_separates_empty_from_all_inactive(self):
        """İki durumun operatör eylemi farklı: enstrüman EKLE / var olanı AÇ."""
        from strategy_studio.compiler import CompileError, compile_strategy

        defn = _defn_with_active([False, False, False])
        defn.instruments = []

        with pytest.raises(CompileError) as err:
            compile_strategy(defn)

        assert "no instruments configured" in str(err.value)

    def test_the_active_subset_is_what_gets_compiled(self):
        """Karışık durumda daraltma hâlâ çalışmalı — düzeltme onu bozmasın."""
        from strategy_studio.compiler import compile_strategy

        defn = _defn_with_active([True, False, True])

        compiled = compile_strategy(defn)

        assert [i["symbol"] for i in compiled.instruments] == ["XAUUSD", "NAS100"]

    def test_compiler_and_graph_now_agree_on_the_traded_set(self):
        """Ekran ile artefakt aynı kümeyi göstermeli.

        Bulgunun asıl zararı buradaydı: `to_graph` fallback'siz filtreliyor,
        `compile_strategy` fallback'le genişletiyordu. İkisi ayrıştığında
        operatörün gördüğü şey dağıtılan şey olmaktan çıkıyor.
        """
        from strategy_studio import graph
        from strategy_studio.compiler import compile_strategy

        defn = _defn_with_active([True, False, True])

        compiled = {i["symbol"] for i in compile_strategy(defn).instruments}
        drawn = {
            node["label"].split()[0]
            for node in graph.to_graph(defn)["nodes"]
            if node.get("kind") == "instrument"
        }

        assert compiled == drawn


# ── 2. pending dağıtımlar uzlaştırmada görünmeli ──────────────────────────


@pytest.fixture()
def store(tmp_path):
    from scripts.seed_studio import build_fixture
    from strategy_studio.store import StrategyStore

    st = StrategyStore(tmp_path / "t.db")
    st.save(build_fixture())
    return st


class TestPendingDeploymentsAreReconcilable:
    def test_live_deployments_includes_pending(self, store):
        """`pending` de "veritabanının canlı sandığı" satırdır."""
        store.create_deployment("d1", SID, 1, "paper", "{}")

        assert [r["deploy_id"] for r in store.live_deployments()] == ["d1"]

    def test_a_stopped_row_is_not_live(self, store):
        """Kapsam genişledi diye ölü satırlar da dönmemeli."""
        store.create_deployment("d1", SID, 1, "paper", "{}")
        store.set_deployment_status("d1", "stopped")

        assert store.live_deployments() == []

    def test_reconcile_ignores_pending_by_default(self):
        """Varsayılan GÜVENLİ taraf: uçuşta bir devralma biçilmemeli.

        Bir `pending` satır iki farklı şeyin adı — az önce yaratılmış canlı bir
        kayıt ya da devralınamadan ölmüş bir kalıntı. Farkı satır değil ÇAĞIRAN
        bilir, o yüzden karar bayrağa gömülü.
        """
        from strategy_studio.runner import reconcile_orphans

        rows = [{"deploy_id": "d1", "status": "pending"}]

        assert reconcile_orphans(rows, set()) == []

    def test_startup_opts_in_and_gets_the_row(self):
        from strategy_studio.runner import reconcile_orphans

        rows = [{"deploy_id": "d1", "status": "pending"}]

        orphans = reconcile_orphans(rows, set(), include_pending=True)

        assert [d for d, _ in orphans] == ["d1"]

    def test_the_reason_says_pickup_never_happened(self):
        """Sebep ayrı olmalı: `running` düğüm KAYBETTİ, `pending` hiç görmedi.

        Operatörün eylemi buna bağlı — yeniden başlat ile yeniden dağıt farklı.
        """
        from strategy_studio.runner import reconcile_orphans

        rows = [
            {"deploy_id": "d1", "status": "pending"},
            {"deploy_id": "d2", "status": "running"},
        ]

        reasons = dict(reconcile_orphans(rows, set(), include_pending=True))

        assert "pickup never happened" in reasons["d1"]
        assert "restarted" in reasons["d2"]

    def test_a_pending_row_the_runner_already_owns_is_not_an_orphan(self):
        """Devralma başladıysa satır sahipsiz değildir."""
        from strategy_studio.runner import reconcile_orphans

        rows = [{"deploy_id": "d1", "status": "pending"}]

        assert reconcile_orphans(rows, {"d1"}, include_pending=True) == []

    def test_the_startup_path_actually_opts_in(self):
        """Sözleşmenin iki ucu: bayrak var ama çağıran kullanmıyorsa boş.

        Kaynak-seviyesi kontrol, çünkü `_reconcile_deployments` modül import'unda
        bir kez koşuyor ve onu testte yeniden tetiklemek gerçek uzlaştırmayı da
        tetiklerdi.
        """
        import inspect

        from web.routes import strategy_studio as main

        src = inspect.getsource(main._reconcile_deployments)

        assert "include_pending=True" in src


# ── 3. delete_custom atomik olmalı ────────────────────────────────────────


@pytest.fixture()
def blocks(tmp_path, monkeypatch):
    """İzole blok store'u: tek kayıtlı blokla başlar."""
    import custom_block_store as cbs

    monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
    monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
    cbs.save_custom(
        "blk_one",
        {"label": "One", "role": "entry", "params": {"period": {"default": 14}}},
        "def evaluate(state, block, closes, indicators, portfolio):\n    return None\n",
    )
    return cbs


class TestDeleteCustomIsAtomic:
    def test_a_successful_delete_removes_both_sides(self, blocks):
        assert blocks.delete_custom("blk_one") is True

        assert not blocks.module_path("blk_one").exists()
        assert "blk_one" not in blocks._read_registry()

    def test_a_failed_registry_write_restores_the_file(self, blocks, monkeypatch):
        """Eski kod bu testi geçemezdi: dosya silinmiş, kayıt yerinde kalırdı.

        `_write_registry` yalnız İLK çağrıda patlıyor — yani commit düşüyor,
        transaction'ın geri alma yazımı başarılı oluyor. Gerçek arıza sınıfı
        (disk dolu / izin) `OSError`.
        """
        real = blocks._write_registry
        calls = {"n": 0}

        def _fails_once(reg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            return real(reg)

        monkeypatch.setattr(blocks, "_write_registry", _fails_once)

        with pytest.raises(OSError):
            blocks.delete_custom("blk_one")

        assert blocks.module_path("blk_one").exists(), "silinen dosya geri gelmedi"
        assert "blk_one" in blocks._read_registry(), "kayıt uçtu"

    def test_the_restored_block_is_still_loadable(self, blocks, monkeypatch):
        """Geri gelen şey dosyanın ADI değil İÇERİĞİ olmalı."""
        original = blocks.module_path("blk_one").read_text(encoding="utf-8")
        real = blocks._write_registry
        calls = {"n": 0}

        def _fails_once(reg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(13, "Permission denied")
            return real(reg)

        monkeypatch.setattr(blocks, "_write_registry", _fails_once)
        with pytest.raises(OSError):
            blocks.delete_custom("blk_one")

        assert blocks.module_path("blk_one").read_text(encoding="utf-8") == original
        assert blocks.get_custom("blk_one") is not None

    def test_deleting_an_unknown_block_is_a_no_op(self, blocks):
        """Transaction'a geçiş bu erken çıkışı bozmamalı."""
        assert blocks.delete_custom("blk_missing") is False
        assert "blk_one" in blocks._read_registry()
