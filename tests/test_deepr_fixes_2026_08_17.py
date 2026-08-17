"""DeepR 2026-08-17 raporunun doğrulanan bulguları.

Hepsi "kod çalışıyor ama söz verdiğini yapmıyor" sınıfından — hiçbiri istisna
atmıyordu, hiçbiri kırmızı test üretmiyordu. Bu yüzden her birinin testi
DAVRANIŞI değil VAADİ sınıyor:

1. ``NAU_STUDIO_DB`` gerçekten depoyu taşıyor mu (test izolasyonu bu söze
   dayanıyor — ``tests/browser/conftest.py``).

Wiki References
---------------
Bkz: [[strategy_studio]], [[review_raporu_uretildigi_anda_bayatlar]]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 1. NAU_STUDIO_DB — store kendi yolunu uydurmasın
# ---------------------------------------------------------------------------


def test_studio_db_env_var_actually_moves_the_store(tmp_path):
    """Ayrı SÜREÇ: değişken import anında okunuyor, monkeypatch geç kalırdı.

    Bu, ``tests/browser/conftest.py``'nin dayandığı sözün ta kendisi. Store
    kendi ``parents[1] / "studio.db"`` yolunu kurduğu sürece o süit gerçek
    repo kökündeki DB'ye yazıyordu ve kimse fark etmiyordu.
    """
    target = tmp_path / "moved.db"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from strategy_studio.store import StrategyStore;"
            "print(StrategyStore().db_path)",
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=os.environ | {"NAU_STUDIO_DB": str(target)},
        check=True,
    )
    assert out.stdout.strip() == str(target)


def test_studio_db_default_is_unchanged_when_env_is_unset(monkeypatch):
    """Varsayılan TAŞINMADI — taşımak mevcut kurulumların stratejilerini
    görünmez kılardı (``app_constants`` bu kararı zaten yazıyor)."""
    monkeypatch.delenv("NAU_STUDIO_DB", raising=False)
    from app_constants import studio_db_path
    from strategy_studio.store import StrategyStore

    assert StrategyStore().db_path == str(studio_db_path())
    assert studio_db_path().name == "studio.db"
