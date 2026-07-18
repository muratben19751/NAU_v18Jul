"""Bağımlılıksız ortak sabitler — döngüsel import olmadan her modül okuyabilir.

- ``STARTING_CASH``: backtest.py ile composer.py'nin ortak başlangıç nakiti
  (tek kaynaktan gelsin diye burada; kopya sabit sessizce ayrışabiliyordu).
- ``NO_WINDOW_FLAGS``: Windows'ta alt-süreçlerin konsol penceresi açmasını
  engelleyen ``creationflags`` değeri.
"""

from __future__ import annotations

import os
import subprocess

STARTING_CASH = 10_000.0

# Windows'ta bir KONSOL uygulaması (claude CLI, bash/gunzip/awk) başlatıldığında
# her çağrıda bir terminal penceresi açılıp kapanır — sunucu pythonw ile konsolsuz
# koşsa bile, çünkü konsolsuz bir parent, konsollu bir child için YENİ pencere
# yaratır. CREATE_NO_WINDOW konsolu hiç yaratmaz; `startupinfo`/`windowsHide` bu
# makinede Windows Terminal tarafından yok sayıldığından güvenilir tek yol budur.
# POSIX'te 0 OLMALI — subprocess orada sıfır-olmayan creationflags'i reddeder.
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
