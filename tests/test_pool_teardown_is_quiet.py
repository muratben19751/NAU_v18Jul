"""Kapanış yarışı, veri hatasından AYRI raporlanmalı.

ÖLÇÜLEN BELİRTİ (2026-08-21): robustluk suite'i çok-sembol kesin reddinde erken
dönüyor (`auto/robustness.py`), `finally` havuzu `shutdown(wait=False)` ile
kapatıp anlık görüntüyü SİLİYOR — ve o sırada hâlâ başlatılmakta olan işçiler
dosyayı bulamayıp `concurrent.futures`'a çok satırlı bir "Exception in
initializer" yığını bastırıyordu. Üç erken dönen koşuda 9 sahte yığın.

Ölçüm bozulmuyordu (suite sıralı yola düşüyor) ama log'daki gürültü GERÇEK bir
hatayı maskeler. Ve iki farklı arıza — "snapshot hiç yok" ile "havuz kapanıyor" —
aynı mesajı üretiyordu.

Çözüm ikisini AYIRMAK:
  * kurulumda bir kez, GÜRÜLTÜLÜ doğrulama → eksik snapshot erken ve net patlar
  * işçide yokluk artık YALNIZCA kapanış demek → sessiz çıkış

Wiki References
---------------
Bkz: [[webapp_module_map]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import os
import tempfile

import pytest


def test_a_missing_snapshot_fails_loudly_at_construction():
    """Gerçek arıza erken ve net patlamalı — işçide değil, kurulumda."""
    import parallel_exec

    missing = os.path.join(tempfile.gettempdir(), "nau_yok_dizin", "bars.parquet")
    with pytest.raises(FileNotFoundError, match="snapshot missing at construction"):
        parallel_exec.BacktestPool(
            missing,
            {"source": "external", "instrument_id": "X.NASDAQ", "granularity": "1-DAY"},
            max_workers=1,
        )


def test_the_worker_treats_a_vanished_snapshot_as_teardown(monkeypatch):
    """Kurulumda doğrulandığı için işçide yokluk yalnız kapanış olabilir.

    DİKKAT: `_worker_init` süreç-içi çağrılamaz — kurduğu ebeveyn-canlılık
    bekçisi `mp.parent_process()` None dönünce `os._exit(1)` çağırıyor ve TEST
    KOŞUCUSUNU öldürüyor (ölçüldü: pytest 1. testten sonra sessizce kapandı).
    Bekçi bu yüzden canlı bir ebeveyn görecek şekilde taklit ediliyor; testin
    konusu bekçi değil, eksik snapshot'ın nasıl sınıflandırıldığı.
    """
    import multiprocessing as mp

    import parallel_exec

    class _AliveParent:
        @staticmethod
        def is_alive() -> bool:
            return True

    monkeypatch.setattr(mp, "parent_process", lambda: _AliveParent())
    parallel_exec._G.clear()
    missing = os.path.join(tempfile.gettempdir(), "nau_silinmis", "bars.parquet")
    # Yükseltmemeli: yükseltmek `concurrent.futures`'ın yığın basmasına yol açıyor.
    parallel_exec._worker_init(missing, {"source": "external",
                                         "instrument_id": "X.NASDAQ",
                                         "granularity": "1-DAY"})
    assert parallel_exec._G.get("torn_down") is True
    assert parallel_exec._G.get("df") is None
    parallel_exec._G.clear()


def test_the_two_failures_are_not_the_same_message():
    """Ayrımın kaynağı tutulur: iki arıza tek mesaja geri dönmemeli."""
    import inspect

    import parallel_exec

    init_src = inspect.getsource(parallel_exec._worker_init)
    ctor_src = inspect.getsource(parallel_exec.BacktestPool.__init__)
    assert "torn_down" in init_src, "işçi kapanışı ayrı işaretlemiyor"
    assert "snapshot missing at construction" in ctor_src, "kurulum doğrulaması yok"
