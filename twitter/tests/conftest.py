"""`twitter/` klasörünü import yoluna alır — bu süit depo kökünden bağımsızdır.

Nautilus uygulamasının `tests/conftest.py`'si burayı hiç görmez ve görmemeli:
iki süit ayrı koşar (`pytest twitter/tests`), ayrı bağımlılık kümesine dayanır.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
