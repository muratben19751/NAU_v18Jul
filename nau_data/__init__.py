"""`data.py`'nin durum taşımayan (pure) yardımcıları.

DeepR 2026-08-11 [ORTA]: `data.py` tek modülde dört veri kaynağı + önbellek +
kilitleme + sunum satırı üretimi taşıyor (3078 satır). Bölünmesi doğru, ama bu
modülde onu sessizce tehlikeli yapan bir kısıt var: test paketi **45 ayrı
``data.*`` adını** monkeypatch ediyor (depolama kökleri, TTL'ler,
``_fetch_bybit_page``, ``_cache_lock``, önbellek sözlükleri…).

Naif bölme — kodu alt modüllere taşıyıp ``data.py``'yi yeniden-dışa-verim
kabuğuna çevirmek — o yamaların çoğunu SESSİZCE etkisiz kılar:
``monkeypatch.setattr`` ile ``data.NAUTILUS_CATALOG_DIR``'ı yamalamak kabuktaki
adı değiştirir, değeri okuyan alt modül kendi bağını okumaya devam eder. Testler
yeşil kalır ve gerçek ``~/.cache/nautilus_web_app`` kataloğuna yazmaya
başlarlar — yani bölmenin bedeli, tam da bu turun en sert kuralının sessizce
ihlali olur.

## Bu paketin kuralı

Buraya yalnız **durum taşımayan** kod girer: girdileri argümanlarından alan,
`data`'nın modül-global'lerine (ve dolayısıyla yamalanabilir yüzeye) hiç
dokunmayan fonksiyonlar. Kural yorumla değil testle korunuyor:

- ``tests/test_data_layer_is_stateless.py`` — `nau_data/` altında hiçbir modül
  `data`'yı import edemez ve yamalanan adlardan hiçbirine çıplak referans
  veremez (AST),
- ``tests/test_data_module_surface_is_stable.py`` — yamalanan her ad hâlâ
  `data`'nın özniteliği ve yamalanan fonksiyonlar hâlâ `data.py`'de tanımlı.

Kalan bölme (dört kaynağın kendi modüllerine ayrılması) bu iki testin çizdiği
sınır içinde, kaynak kaynak yapılmalı: taşınan kod yamalanan bir ada
dokunuyorsa ona ``data.<ad>`` olarak, ÇAĞRI ANINDA erişmeli — o zaman yama
görünür kalır.

Wiki References: [[webapp_module_map]], [[import_aninda_yakalanan_referans]]
"""

from __future__ import annotations

from nau_data.atomic_io import (
    atomic_to_parquet,
    atomic_write_json,
    replace_with_retry,
    stat_sig,
    tmp_sibling,
)
from nau_data.bar_math import (
    EPOCH_MS_DIVISOR,
    aggregate_ohlc,
    bybit_gap_ranges,
    catalog_fname_ns,
    external_interval_ns,
    index_epoch_ms,
)

__all__ = [
    "EPOCH_MS_DIVISOR",
    "aggregate_ohlc",
    "atomic_to_parquet",
    "atomic_write_json",
    "bybit_gap_ranges",
    "catalog_fname_ns",
    "external_interval_ns",
    "index_epoch_ms",
    "replace_with_retry",
    "stat_sig",
    "tmp_sibling",
]
