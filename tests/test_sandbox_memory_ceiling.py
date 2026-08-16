"""Backtest çocuğunun bellek tavanı — üç yüzeyin üçünde de var mı.

Duvar saati kaçak bir DÖNGÜYÜ keser; tek satırlık bir ayırmayı (`[0] * 10**9`
≈ 8 GB) kesemez, çünkü o deadline gelmeden makinenin RAM'ini yer. Bu yüzden
öldürülebilir her çocuğun bir bellek tavanı olmalı. Önizleme ve smoke
çocuklarında vardı; backtest çocuğu (`_child_entry`) tavansızdı — kod
incelemesi 2026-08-17.

Tavan DAVRANIŞINI burada uçtan uca sınamıyoruz (gerçek bir Job Object/rlimit
denemesi platforma bağlı ve pahalı); sınanan şey her çocuğun tavanı KURMASI ve
sabitin ölçümle tutarlı kalması. Ölçüm tablosu `sandbox.BACKTEST_MEMORY_MB`
yorumunda.
"""

from __future__ import annotations

import importlib

import pytest

import sandbox


class _Q:
    """`multiprocessing.Queue` yerine geçen en küçük şey."""

    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


@pytest.fixture
def installed(monkeypatch):
    """Çocuk hedefini süreç İÇİNDE koştur; tavanı kurmak yerine kaydet."""
    calls: list[int] = []
    monkeypatch.setattr(sandbox, "_install_memory_ceiling", calls.append)
    # Bunlar gerçek çocukta anlamlı, testte zararlı: watchdog süreç ölümünü
    # izler, stdio guard test çıktısını yeniden yapılandırır.
    monkeypatch.setattr(sandbox, "_start_parent_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(sandbox, "_child_stdio_guard", lambda *a, **k: None)
    return calls


def test_backtest_child_installs_a_memory_ceiling(installed):
    """`_child_entry` işe başlamadan ÖNCE tavanı kurmalı.

    Sıra önemli: tavan iş başladıktan sonra kurulursa, korumaya çalıştığı
    ayırma çoktan yapılmış olur.
    """
    q = _Q()

    # Bozuk payload → gövde hemen `except`e düşer; tavan çağrısı `try`'dan
    # ÖNCE olduğu için yine de kaydedilmiş olmalı.
    sandbox._child_entry(q, ("bozuk",))

    assert installed == [sandbox.BACKTEST_MEMORY_MB]
    assert q.items and q.items[0][0] == "error", "gövde hiç çalışmamış"


def test_the_backtest_ceiling_is_not_the_user_code_ceiling():
    """Ayrı sabit, çünkü ayrı iş yükü.

    `USER_CODE_MEMORY_MB` 150 barlık bir önizleme için kalibre edildi; backtest
    çocuğundan tam aralıklı bir Nautilus koşusu geçiyor. Aynı sayıyı yeniden
    kullanmak ölçülen tepeye (1.249 MB) yalnız 1,6 kat pay bırakırdı.
    """
    assert sandbox.BACKTEST_MEMORY_MB != sandbox.USER_CODE_MEMORY_MB
    assert sandbox.BACKTEST_MEMORY_MB > sandbox.USER_CODE_MEMORY_MB


def test_the_ceiling_keeps_a_real_margin_over_the_measured_peak():
    """Kalibrasyon çıpası — ölçüm 2026-08-17 (bkz. sabitin yorumu).

    En ağır ölçülen koşu (1,1M bar) 1.249 MB commit; işlem katkısıyla gerçekçi
    en kötü hâl ≈ 1,4 GB. Tavan bunun en az iki katı olmalı: altına inen bir
    değişiklik, koruma değil kesinti üretir. Üst sınır da var — tavan kaçak
    ayırmayı (≈8 GB) hâlâ yakalamalı, yoksa hiçbir şeyi korumaz.
    """
    measured_worst_mb = 1_249
    realistic_worst_mb = 1_400

    assert sandbox.BACKTEST_MEMORY_MB >= 2 * measured_worst_mb
    assert sandbox.BACKTEST_MEMORY_MB >= 2 * realistic_worst_mb
    assert sandbox.BACKTEST_MEMORY_MB < 8_192


def test_operators_can_retune_the_ceiling_without_a_code_change(monkeypatch):
    """Ölçüm bu kutuda yapıldı; başka bir makinede sayı değişebilir."""
    monkeypatch.setenv("NAU_BACKTEST_MEMORY_MB", "5120")
    reloaded = importlib.reload(sandbox)
    try:
        assert reloaded.BACKTEST_MEMORY_MB == 5120
    finally:
        monkeypatch.delenv("NAU_BACKTEST_MEMORY_MB", raising=False)
        importlib.reload(sandbox)


def test_every_killable_child_target_installs_a_ceiling():
    """Üç çocuk hedefi de tavanı KURMALI — biri unutulursa burası kırılır.

    Bu testin varlık sebebi: backtest çocuğu tam olarak böyle unutulmuştu.
    Kaynak-seviyesi tarama, çağrının hangi sabitle yapıldığını değil YAPILDIĞINI
    çiviler; yeni bir çocuk hedefi eklendiğinde de aynı soruyu sordurur.
    """
    import inspect

    src = inspect.getsource(sandbox)
    for target in ("_child_entry", "_block_smoke_child", "_block_preview_child"):
        start = src.index(f"def {target}(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]

        assert "_install_memory_ceiling(" in body, (
            f"{target} bellek tavanı kurmuyor — öldürülebilir bir çocuk, "
            "duvar saatiyle tek satırlık bir ayırmadan korunamaz"
        )
