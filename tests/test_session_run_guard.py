"""Oturum başına tek aktif koşu koruması — sınıfın kendisi ve ÜÇ kablolaması.

DeepR 2026-08-11 [DÜŞÜK]: aynı 12 satır üç route modülüne elle kopyalanmıştı
(`backtest`, `lab`, `robustness`) ve üçünden yalnız biri test ediliyordu.
Kopyalar zaten sessizce ayrışmıştı: `backtest.py`'ninki terk edilmiş oturumlar
için bir tavan + budama taşıyordu, diğer ikisi taşımıyordu — o iki sözlük süreç
ömrü boyunca sınırsız büyüyordu. Kopyalanan bir korumanın çürümesi tam böyle
görünür: davranış farkı değil, yalnız birine yapılmış bir iyileştirme.

Ortak sınıf `web.shared.SessionRunGuard`. Bu dosya iki şeyi ayrı ayrı bağlar:

  1. sınıfın sözleşmesi (bayat kayıt temizliği, tavan, budama, kilit sözleşmesi),
  2. üç modülün gerçekten O SINIFI kullandığı — biri elle bir kopyaya geri
     dönerse test kırmızıya döner.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import threading

import pytest

from web.shared import ProgressStore, SessionRunGuard


class _FakeStore:
    """`.get(rid) -> dict | None` — ProgressStore'un guard'ın gördüğü yüzü."""

    def __init__(self) -> None:
        self.d: dict[str, dict] = {}

    def get(self, rid):
        return self.d.get(rid)


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def guard(store) -> SessionRunGuard:
    return SessionRunGuard(lambda _kind: store)


class TestTheContract:
    def test_a_fresh_session_is_not_busy(self, guard):
        assert guard.busy("sid-1") is False
        assert guard.active_kind("sid-1") is None

    def test_a_live_run_makes_the_session_busy(self, guard, store):
        store.d["r1"] = {"done": False}
        guard.set_active("sid-1", "r1")
        assert guard.busy("sid-1") is True

    def test_a_finished_run_releases_the_session(self, guard, store):
        store.d["r1"] = {"done": False}
        guard.set_active("sid-1", "r1")
        store.d["r1"]["done"] = True
        assert guard.busy("sid-1") is False
        # ...ve bayat kayıt gerçekten DÜŞÜRÜLDÜ (sızıntı olmasın).
        assert "sid-1" not in guard.raw()

    def test_an_evicted_run_releases_the_session(self, guard, store):
        """ProgressStore kaydı tahliye ederse oturum sonsuza dek kilitlenmesin."""
        store.d["r1"] = {"done": False}
        guard.set_active("sid-1", "r1")
        del store.d["r1"]  # tahliye / sunucu yeniden başladı
        assert guard.busy("sid-1") is False

    def test_sessions_do_not_block_each_other(self, guard, store):
        store.d["r1"] = {"done": False}
        guard.set_active("sid-1", "r1")
        assert guard.busy("sid-2") is False

    def test_kind_is_reported_back(self, guard, store):
        store.d["g1"] = {"done": False}
        guard.set_active("sid-1", "g1", "gen")
        assert guard.active_kind("sid-1") == "gen"

    def test_a_second_submit_replaces_the_entry(self, guard, store):
        store.d["r1"] = {"done": True}
        store.d["r2"] = {"done": False}
        guard.set_active("sid-1", "r1")
        guard.set_active("sid-1", "r2")
        assert guard.raw()["sid-1"] == ("run", "r2")


class TestTheCapThatOnlyOneCopyHad:
    """Terk edilmiş oturumlar sınırsız birikmesin — eksik olan buydu."""

    def test_abandoned_finished_sessions_are_pruned_at_the_cap(self, store):
        guard = SessionRunGuard(lambda _k: store, max_entries=5)
        for i in range(5):
            store.d[f"r{i}"] = {"done": True}  # hepsi bitmiş = terk edilmiş
            guard.set_active(f"sid-{i}", f"r{i}")
        assert len(guard.raw()) == 5

        store.d["live"] = {"done": False}
        guard.set_active("sid-new", "live")  # tavanı aşan giriş budamayı tetikler

        assert guard.raw() == {"sid-new": ("run", "live")}

    def test_pruning_never_drops_a_live_session(self, store):
        guard = SessionRunGuard(lambda _k: store, max_entries=3)
        store.d["live"] = {"done": False}
        guard.set_active("sid-live", "live")
        for i in range(2):
            store.d[f"r{i}"] = {"done": True}
            guard.set_active(f"sid-{i}", f"r{i}")

        store.d["live2"] = {"done": False}
        guard.set_active("sid-new", "live2")

        assert "sid-live" in guard.raw(), "canlı oturum budandı"
        assert guard.busy("sid-live") is True

    def test_a_repeat_submit_from_a_known_session_does_not_trigger_pruning(self, store):
        """Tavan doluyken ZATEN kayıtlı bir sid yeniden gönderirse giriş sayısı
        artmıyor; budama yalnız YENİ bir sid tavanı zorladığında çalışmalı."""
        guard = SessionRunGuard(lambda _k: store, max_entries=2)
        for i in range(2):
            store.d[f"r{i}"] = {"done": True}
            guard.set_active(f"sid-{i}", f"r{i}")
        store.d["again"] = {"done": False}
        guard.set_active("sid-0", "again")
        assert set(guard.raw()) == {"sid-0", "sid-1"}


class TestNoLockNesting:
    """Store araması kayıt kilidinin DIŞINDA yapılmalı.

    Kopyalarda bu bilinçli bir seçimdi ve yorumla korunuyordu. Yorum kilit
    sırasını korumaz; bu test korur: `get` çağrısı sırasında kilit tutuluyorsa
    başka bir thread onu alamaz ve test kilitlenme yerine kırmızıya döner.
    """

    def test_store_lookup_happens_without_the_registry_lock(self):
        seen: list[bool] = []
        guard: SessionRunGuard | None = None

        class _ProbingStore:
            def get(self, rid):
                # Guard'ın kilidi bu an serbest mi?
                acquired = guard.lock.acquire(blocking=False)
                seen.append(acquired)
                if acquired:
                    guard.lock.release()
                return {"done": False}

        probe = _ProbingStore()
        guard = SessionRunGuard(lambda _k: probe)
        guard.set_active("sid-1", "r1")
        guard.active_kind("sid-1")

        assert seen and all(seen), "store araması kayıt kilidi altında yapıldı"

    def test_concurrent_set_active_does_not_lose_entries(self, store):
        guard = SessionRunGuard(lambda _k: store, max_entries=1000)
        for i in range(50):
            store.d[f"r{i}"] = {"done": False}

        def _w(i):
            guard.set_active(f"sid-{i}", f"r{i}")

        threads = [threading.Thread(target=_w, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(guard.raw()) == 50


class TestAllThreeRoutesUseTheSharedGuard:
    """Kopyaya geri dönüş kırmızıya dönsün."""

    def test_backtest_route(self):
        from web.routes import backtest as mod

        assert isinstance(mod._RUN_GUARD, SessionRunGuard)
        assert mod._ACTIVE_RUNS is mod._RUN_GUARD.raw()
        assert mod._ACTIVE_RUNS_LOCK is mod._RUN_GUARD.lock

    def test_lab_route(self):
        from web.routes import lab as mod

        assert isinstance(mod._LAB_GUARD, SessionRunGuard)
        assert mod._ACTIVE_LAB_RUNS is mod._LAB_GUARD.raw()

    def test_robustness_route(self):
        from web.routes import robustness as mod

        assert isinstance(mod._ROBUSTNESS_GUARD, SessionRunGuard)
        assert mod._ACTIVE_ROBUSTNESS_RUNS is mod._ROBUSTNESS_GUARD.raw()

    @pytest.mark.parametrize(
        "module_name, guard_attr, busy_fn, set_fn, store_attr",
        [
            (
                "web.routes.lab",
                "_LAB_GUARD",
                "_session_lab_busy",
                "_session_lab_set_active",
                "_LAB_STORE",
            ),
            (
                "web.routes.robustness",
                "_ROBUSTNESS_GUARD",
                "_session_robustness_busy",
                "_session_robustness_set_active",
                "_STORE",
            ),
        ],
    )
    def test_each_wrapper_actually_guards(
        self, module_name, guard_attr, busy_fn, set_fn, store_attr
    ):
        """Sarmalayıcılar gerçekten koruyor mu — sınıfı test etmek yetmez."""
        import importlib

        mod = importlib.import_module(module_name)
        guard = getattr(mod, guard_attr)
        store: ProgressStore = getattr(mod, store_attr)
        sid, rid = "guard-test-sid", "guard-test-rid"
        try:
            assert getattr(mod, busy_fn)(sid) is False
            store.create_evicting(rid, {"done": False})
            getattr(mod, set_fn)(sid, rid)
            assert getattr(mod, busy_fn)(sid) is True
            store.raw()[rid]["done"] = True
            assert getattr(mod, busy_fn)(sid) is False
        finally:
            guard.raw().pop(sid, None)
            store.raw().pop(rid, None)
