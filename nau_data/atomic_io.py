"""Yarım dosya bırakmayan yazma ilkelleri — durum taşımaz.

`data.py`'den çıkarıldı (DeepR 2026-08-11 [ORTA]). Hepsi girdisini
argümanından alır; `data`'nın yamalanabilir modül-global'lerine dokunmaz —
`nau_data` paketinin giriş şartı bu (bkz. paket docstring'i).

Kilitleme (`_cache_lock`) BİLEREK burada değil: testler onu `data._cache_lock`
olarak monkeypatch ediyor ve çağrı yerlerinin `data.py`'de kalması gerekiyor,
yoksa yama sessizce etkisiz kalır.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pandas as pd

# Windows'ta `os.replace`, hedef dosya BAŞKA biri tarafından açıkken
# PermissionError verir (POSIX'te vermez). Okuma yolu artık kilitsiz olduğuna
# göre (DeepR 2026-08-11 [YÜKSEK]) yazarın tam o anda okuyan birine denk gelmesi
# normal bir olaydır ve bir parquet okuması milisaniyeler sürer: kısa bir yeniden
# deneme, "veri kaybettim" ile "50 ms bekledim" arasındaki fark.
REPLACE_RETRY_WAITS = (0.05, 0.15, 0.4)


def stat_sig(path: Path) -> tuple:
    """`(mtime_ns, size)` — dosya/dizin yoksa boş demet."""
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


def replace_with_retry(tmp: Path, path: Path) -> None:
    """`os.replace` + Windows paylaşım ihlaline karşı kısa yeniden deneme."""
    for wait in (*REPLACE_RETRY_WAITS, None):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def tmp_sibling(path: Path) -> Path:
    """Benzersiz kardeş temp adı — SÜREÇ + THREAD.

    DeepR 2026-08-11 [ORTA]: ad yalnız `os.getpid()` içeriyordu, yani aynı
    süreçteki iki THREAD için AYNIYDI: biri diğerinin yarım dosyasını
    `os.replace` edebilir ya da `finally` bloğunda silebilirdi. Yazma yolları
    artık per-key `_cache_lock` altında (aynı hedefe iki thread giremez), ama
    ad benzersizliği ucuz bir ikinci savunma — `custom_block_store._write_registry`
    de tam bu gerekçeyle benzersiz kardeş ad kullanıyor.
    """
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")


def atomic_to_parquet(df: pd.DataFrame, path: Path) -> None:
    """M3a: to_parquet → temp file + os.replace — no half-written parquet remains."""
    tmp = tmp_sibling(path)
    try:
        df.to_parquet(tmp)
        replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(obj, path: Path) -> None:
    """Write JSON sidecars atomically too (same pattern as M3a)."""
    tmp = tmp_sibling(path)
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
