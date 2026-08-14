"""Bar/zaman aritmetiği — ağ yok, disk yok, durum yok.

`data.py`'den çıkarıldı (DeepR 2026-08-11 [ORTA]). Bu fonksiyonların hiçbiri
`data`'nın modül-global'lerine dokunmaz, yani `nau_data` paketinin giriş
şartını sağlarlar (bkz. paket docstring'i): yamalanabilir yüzeyle temas yok,
dolayısıyla taşınmaları hiçbir monkeypatch'i sessizce etkisiz kılmaz.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd

Granularity = Literal["1d", "1m", "5m", "15m", "60m"]

# pandas resample rule (converts raw NFS ticks into OHLCV via aggregate_ohlc)
GRAN_RULE = {
    "1d": "1D",
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "60m": "60min",
}

EPOCH_MS_DIVISOR = {"ns": 1_000_000, "us": 1_000, "ms": 1}

EXT_GRAN_SECONDS = {"MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800}


def index_epoch_ms(index) -> np.ndarray:
    """DatetimeIndex → epoch MİLİSANİYE (int64 dizi), kopyasız.

    `index.as_unit("ns").asi8` doğru ama pahalı: 1M elemanlı bir indekste
    ölçüldü, tek başına 29 ms (indeksin birimi 'us' ise gerçekten yeniden
    ölçekleyip kopyalıyor). `asi8` ham sayacı bedelsiz verir; ondan ms'ye
    inmek yalnızca birime bağlı bir tamsayı bölmesi. Taban bölmesi zincirleme
    yapıldığında da aynı sonucu verir (us→ns→ms ile us→ms özdeş), yani sonuç
    ESKİ YOLLA BİT BİT AYNI — sadece 1M elemanlık bir ara dizi ayrılmıyor.
    Bilinmeyen/egzotik birimde eski (güvenli) yola düşülür.
    """
    unit = getattr(index, "unit", "ns")
    div = EPOCH_MS_DIVISOR.get(unit)
    if div is not None:
        return index.asi8 // div if div > 1 else index.asi8
    if unit == "s":
        return index.asi8 * 1000
    try:
        return index.as_unit("ns").asi8 // 1_000_000
    except AttributeError:  # old pandas: asi8 is already ns
        return index.asi8 // 1_000_000


def bybit_gap_ranges(cached: pd.DataFrame, step_ms: int) -> list[tuple[int, int]]:
    """Cache'in [ilk, son] aralığındaki DELİKLER — (başlangıç, bitiş) ms, kapalı.

    Ağa çıkmaz, dosyaya dokunmaz: adım sabit olduğu için beklenen bar sayısı
    aritmetiktir; satır sayısı eksikse ardışık açılışlar arasındaki > step_ms
    boşluklar deliklerdir. Saf tespit olarak ayrıldı ki okuma yolu "bu istek
    kilide girmeden karşılanır mı?" sorusunu ağa hiç çıkmadan sorabilsin.
    """
    if cached.empty or len(cached.index) < 2:
        return []
    ms = index_epoch_ms(cached.index)
    first_ms, last_ms = int(ms[0]), int(ms[-1])
    expected = (last_ms - first_ms) // step_ms + 1
    if len(ms) >= expected:
        return []

    # DeepR 2026-08-11 [DÜŞÜK]: burası eskiden `for cur in ms[1:]` ile numpy
    # int64 dizisini Python'da tek tek geziyor, her elemanı `int()` ile
    # kutuluyordu. 1M barlık bir 1m önbelleğinde ölçüldü: 85,0 ms → 1,6 ms
    # (bunun 29 ms'i taramada değil, `index_epoch_ms`'in kaldırdığı
    # `as_unit("ns")` kopyasındaydı). `load_bybit_bars` bu maliyeti HER çağrıda
    # (chart, sweep, robustness, agent Phase-0) ödüyordu. Delik SAYISI zaten
    # küçük olduğundan Python döngüsü yalnız gerçek deliklerin üzerinde döner;
    # taramanın kendisi numpy'de.
    hole_at = np.flatnonzero(np.diff(ms) > step_ms)
    return [(int(ms[i]) + step_ms, int(ms[i + 1]) - step_ms) for i in hole_at.tolist()]


def aggregate_ohlc(rows: pd.DataFrame, granularity: Granularity) -> pd.DataFrame:
    """Convert raw (ticker,value,timestamp) rows into OHLCV bars."""
    if rows.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(rows["timestamp"], unit="ns", utc=True)
    s = pd.Series(rows["value"].astype(float).values, index=ts, name="value")
    rule = GRAN_RULE[granularity]
    bars = s.resample(rule).agg(
        open="first",
        high="max",
        low="min",
        close="last",
    )
    bars = bars.dropna(subset=["open"])
    # M34: NAU convention — index series have no volume concept. Formerly the
    # tick count was written; that fake volume could mislead volume-based
    # blocks. Thanks to volume_spike's avg<=0 guard, 0 is a no-op.
    bars["volume"] = 0.0
    bars.index.name = "timestamp"
    return bars


def catalog_fname_ns(stamp: str) -> int | None:
    """Catalog parquet file name stamp → epoch ns.

    E.g. '2026-07-08T00-00-00-000000000Z' (Nautilus '{startZ}_{endZ}.parquet'
    naming, bar CLOSE times). None if unparseable.
    """
    try:
        body = stamp.removesuffix("Z")
        date_part, _, time_part = body.partition("T")
        hh, mm, ss, nanos = time_part.split("-")
        dt = datetime.fromisoformat(f"{date_part}T{hh}:{mm}:{ss}+00:00")
        return int(dt.timestamp()) * 1_000_000_000 + int(nanos)
    except Exception:
        return None


def external_interval_ns(granularity: str) -> int:
    """'1-DAY' → 86400e9. Raises ValueError on anything that isn't a
    time-aggregated catalog step (defensive against form tampering).

    Note (L34): for DAY/WEEK the derived duration is the CALENDAR NOMINAL
    interval (24 hours / 7 days), not the session length. The close→open shift
    uses this nominal step; shortened sessions / half days are not modeled
    separately.
    """
    step_s, _, unit = granularity.partition("-")
    if not step_s.isdigit() or unit not in EXT_GRAN_SECONDS:
        raise ValueError(f"unsupported external granularity {granularity!r}")
    return int(step_s) * EXT_GRAN_SECONDS[unit] * 1_000_000_000
