"""US-equity ingest: Massive/Polygon flat-file arşivi → bu projenin equity kataloğu.

NAU_ev'in `tools/ingest_flatfiles.py` + `tools/build_tf_bars.py` ikilisinin bu
repoya portu (kaynak: D:\\NAU_ev\\backend\\tools). Oradaki akış korunur —
minute bar'lar yazılır, TF'ler (5m/15m/1h/4h/1d) RTH resample ile türetilir,
manifest EN SONDA yazılır — ama üç bilinçli fark var:

* **Hedef katalog bu projenin kendi kökü** (`data.EQUITY_CATALOG_DIR`), NAU_ev'in
  kataloğu DEĞİL: başka projenin veri klasörüne buradan yazılmaz. `data.py`
  bu kökü var olduğunda `EXTERNAL_CATALOGS`'a ekler → /data paneli, Lab
  picker'ları ve backtest yükleri kendiliğinden görür.
* **Guard'ın karşılığı:** NAU_ev universe.yaml'daki 24 sembolü dışlıyordu
  (adjusted sürümleri korunur); burada DİĞER external köklerde (591'lik NAU_ev
  kataloğu) zaten olan ticker atlanır — aynı gerekçe, adjusted sürüm orada
  daha iyi ve /data panelinde çift kayıt oluşmaz. `--force` geçersiz kılar.
* TF üretimi ayrı bir CLI değil, aynı koşumun ikinci fazı — "minute yazıldı,
  TF unutuldu" ara durumu olmaz (NAU_ev'de manifest-tazeliği makinesi bu ara
  durumu yönetiyordu; burada gerek kalmıyor).

Veri UNADJUSTED'tır (flat-file agg ham): split olan ticker'larda geçmiş fiyat
sıçrar; manifest'e `adjusted: false` yazılır ve /data rozeti bunu gösterir.
Adjusted istenirse NAU_ev'in REST yolu (`tools/download_adjusted.py`) kullanılır.

Veri bütünlüğü (2026-08-10 denetimi sonrası) üç ek sözleşme taşır:

* **RTH sınırı right-label'a göre** — `RTH_FIRST_LABEL="09:31"`; "09:30" damgalı
  bar pre-market'tir (bkz. sabitin yanındaki not).
* **Bozuk OHLC sessizce geçmez** — `_sanitize_ohlc` hem ingest hem TF yolunda.
* **4-HOUR seans-göreli** — mutlak `240min` kovaları DST'de kırılıyordu
  (`resample_tf`).

Manifest artık `split_adjusted` / `dividend_adjusted` alanlarını AYRI tutar;
eski `adjusted` alanı `split_adjusted` ile eşitlenerek korunur. Ticker başına
`coverage_gaps` da yazılır (QQQ'nun 2004→2011 deliği gibi).

Kullanım (repo kökü, PYTHONUTF8=1):
  python ingest_equities.py --tickers HOOD,RIVN --years 2020-2026
  python ingest_equities.py --tickers-file tickers.txt --years 2003-2026
  python ingest_equities.py --rebuild-tf          # arşive dokunmaz
  python ingest_equities.py --migrate-manifest --coverage-gaps

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]],
[[index_backtest_via_equity_proxy]], [[instruments]],
[[ticker_kimlik_degil_o_gunun_etiketi]]

Right-label sözleşmesi data.py/backtest.py ile aynı: bar ts = KAPANIŞ
(`window_start + 60e9`); TF resample `label="right", closed="right"`.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import data

log = logging.getLogger(__name__)

DEFAULT_FLATFILE_ROOT = Path(r"E:\MarketData\massive-flatfiles\us_stocks_sip")
_MIN_NS = 60_000_000_000  # window_start bar BAŞI → +60e9 = KAPANIŞ (right-label)
_COLS = ["ticker", "volume", "open", "close", "high", "low", "window_start"]
_PRICE_PRECISION = 2  # NAU_ev _equity ile aynı; US hisse kuruş adımı

# TF → (pandas resample kuralı, Nautilus spec) — NAU_ev build_tf_bars.TFS portu.
# Kural `None` ise TF pandas resample ile DEĞİL, seans-göreli gruplanır
# (gerekçe: `resample_tf` docstring'i).
TFS = {
    "5": ("5min", "5-MINUTE"),
    "15": ("15min", "15-MINUTE"),
    "60": ("60min", "1-HOUR"),
    "240": (None, "4-HOUR"),
    "D": ("1D", "1-DAY"),
}
SESSION_RELATIVE_MINUTES = {"240": 240}  # seans-göreli TF'ler → kova uzunluğu

TZ = "America/New_York"

# ── RTH penceresi — DİKKAT: sınırlar RIGHT-LABEL damgalarıdır ────────────────
# Bu repoda bar ts'i KAPANIŞTIR (`window_start + 60s`, bkz. _MIN_NS). Dolayısıyla
# "09:30" damgalı bar 09:29–09:30 dilimidir, yani PRE-MARKET'tir. Seansın ilk
# barı 09:30–09:31 dilimi = "09:31" damgalı bardır, sonuncusu 15:59–16:00 =
# "16:00".
#
# GERİ ALMAYIN — "09:31" yazım hatası değil. Alt sınır "09:30" yapılırsa her
# seansa bir pre-market dakikası sızar (390 yerine 391 bar) ve o dakikanın
# fiyatı günlük/TF barın AÇILIŞI olur. Ölçülen etki (16 ticker, 5.738 seans):
# seansların %86,8'inde open hatalı, ortalama 3,2 bp, maksimum 116 bp.
RTH_FIRST_LABEL = "09:31"  # seansın İLK barının kapanış damgası
RTH_LAST_LABEL = "16:00"  # seansın SON barının kapanış damgası
RTH_OPEN_MINUTE = 9 * 60 + 30  # seans açılışı: yerel gece yarısından 570 dk
RTH_MINUTES = 390  # tam seans uzunluğu (09:30→16:00) — 4-HOUR kovalarının ölçeği
# Geriye uyumlu adlar: repair_massive_intraday.py bunları import ediyor.
RTH_START, RTH_END = RTH_FIRST_LABEL, RTH_LAST_LABEL

GAP_MIN_SESSIONS = 10  # bundan kısa boşluk tatil/veri gürültüsü sayılır

# source → dividend_adjusted. Sağlayıcı davranışı: Polygon/Massive
# `/v2/aggs adjusted=true` YALNIZ split ayarlar, temettü ayarlamaz. Flat-file
# agg'ler tamamen ham. Dikilmiş seriler parçalarının rejimini devralır.
_DIVIDEND_ADJUSTED_BY_SOURCE = {
    "massive:rest_aggs": False,
    "flatfile:minute_aggs": False,
    "stitch:": False,  # prefix eşleşmesi
}


def _log(m: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {m}", flush=True)


def _year_files(root: Path, year: int) -> list[Path]:
    ydir = root / "minute_aggs_v1" / str(year)
    if not ydir.is_dir():
        return []
    return [
        f
        for m in sorted(ydir.iterdir())
        if m.is_dir()
        for f in sorted(m.glob("*.csv.gz"))
    ]


def _equity(ticker: str, venue: str):
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Currency, Price, Quantity

    return Equity(
        instrument_id=InstrumentId(symbol=Symbol(ticker), venue=Venue(venue)),
        raw_symbol=Symbol(ticker),
        currency=Currency.from_str("USD"),
        price_precision=_PRICE_PRECISION,
        price_increment=Price(0.01, _PRICE_PRECISION),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def _sanitize_ohlc(
    df,
    *,
    o: str = "open",
    h: str = "high",
    lo: str = "low",
    c: str = "close",
    label: str = "",
):
    """Bozuk OHLC satırlarını onar/düşür → (temiz df, {"fixed", "dropped"}).

    Neden: ham kaynakta gerçek bir bar var — 2004-07-28 12:29 ET,
    `open=33.85 high=33.86 low=0.00 close=33.85`. Bu sıfır agregasyonla saatlik
    ve günlük bara da yayıldı (QQQ ve QQQC), `bar_adaptive_high_low_ordering`
    açıkken o seansta HER stop-loss'u tetikliyordu. Tek bir hücre bütün bir
    seansın backtest'ini çöpe atıyor.

    Onarılabilir: open ve close pozitifse high/low onlardan yeniden kurulur
    (`high = max(open, close, high)`, `low = min(open, close, low)`). Bu tek
    kural dört kusuru birden kapatır: pozitif-olmayan high/low, `high < low`,
    `high < max(open, close)`, `low > min(open, close)`.
    Onarılamaz: open ya da close pozitif değil/NaN — fiyat bilinmiyorsa
    uydurmaktansa satırı DÜŞÜRMEK doğru.

    Sessiz onarım yok: dokunulan satır sayısı `_log` ile basılır.
    """
    import numpy as np
    import pandas as pd

    n0 = len(df)
    if not n0:
        return df, {"fixed": 0, "dropped": 0}
    vals = {
        k: pd.to_numeric(df[k], errors="coerce").to_numpy(dtype=float)
        for k in (o, h, lo, c)
    }
    keep = np.isfinite(vals[o]) & (vals[o] > 0) & np.isfinite(vals[c]) & (vals[c] > 0)
    dropped = int((~keep).sum())
    if dropped:
        df = df[keep]
        vals = {k: v[keep] for k, v in vals.items()}

    hi_ref = np.maximum(vals[o], vals[c])
    lo_ref = np.minimum(vals[o], vals[c])
    new_h = np.maximum(
        np.where(np.isfinite(vals[h]) & (vals[h] > 0), vals[h], hi_ref), hi_ref
    )
    new_l = np.minimum(
        np.where(np.isfinite(vals[lo]) & (vals[lo] > 0), vals[lo], lo_ref), lo_ref
    )
    # NaN != x her zaman True → NaN'lı high/low da "onarıldı" sayılır.
    fixed = int(((new_h != vals[h]) | (new_l != vals[lo])).sum())
    if fixed:
        df = df.copy()
        df[h] = new_h
        df[lo] = new_l
    if fixed or dropped:
        _log(
            f"  {label}bozuk OHLC: {fixed:,} satır onarıldı, "
            f"{dropped:,} satır düşürüldü ({n0:,} satırda)"
        )
    return df, {"fixed": fixed, "dropped": dropped}


def _session_bucket_labels(idx, minutes: int):
    """Right-label bar damgaları → seans-göreli kova ETİKETLERİ (kova sonu)."""
    import pandas as pd

    day = idx.normalize()
    # Seans açılışı = yerel gece yarısı + 570 dk. DST geçişleri pazar 02:00'de
    # olur (işlem günü değil), bu yüzden yerel gece yarısına dakika eklemek
    # her işlem gününde tam olarak 09:30'u verir.
    session_open = day + pd.Timedelta(minutes=RTH_OPEN_MINUTE)
    elapsed = (idx - session_open) // pd.Timedelta(minutes=1)
    # Right-label: 09:31 barı seansın 1. dakikasıdır → 1..240 kova 0,
    # 241..390 kova 1. (elapsed-1)//minutes tam bunu verir.
    bucket = (elapsed - 1) // minutes
    end = session_open + pd.to_timedelta((bucket + 1) * minutes, unit="m")
    tmp = pd.DataFrame({"ts": idx, "day": day, "end": end})
    # Yarım günlerde (13:00 ET kapanış) kova sonu seansın son barını aşar:
    # etiket, o seansta GERÇEKTEN var olan son bara kırpılır — böylece damga
    # hep gerçek bir kapanış anıdır, borsa kapandıktan sonrası değil.
    tmp["last"] = tmp.groupby("day")["ts"].transform("max")
    return pd.DatetimeIndex(tmp["end"].where(tmp["end"] <= tmp["last"], tmp["last"]))


def resample_tf(df, tf: str, agg: dict[str, str]):
    """RTH-filtreli dakikalık frame'i (tz'li, right-label indeks) TF'e agregele.

    5/15/60 dakika ve 1-DAY pandas `resample`'da KALIR: bunlar bir saatin (ve
    günün) tam böleni oldukları için mutlak kovalar NY yerel saatinde hizalı
    kalır ve DST'den etkilenmez. Ayrıca "1-DAY = 1-HOUR'un tam agregası"
    özelliği doğrulanmış durumda — bozmamak gerekir.

    4-HOUR farklıdır: 240 dk ne saatin ne günün bölenidir, mutlak kovalar
    seansa oturmaz. Ölçülen sonuç: EDT'de seans başına 2 bar (12:00, 16:00),
    EST'de 3 bar (11:00, 15:00 VE 19:00) — 19:00 barı borsa kapandıktan 3 saat
    SONRA damgalı ve içinde yalnız 15:00–16:00 verisi var; ilk bar 1,5 saatlik.
    Bu yüzden 4-HOUR SEANS-GÖRELİ gruplanır: kova indeksi seans açılışından
    itibaren geçen dakikaya göre, etiket kova sonu (right-label sözleşmesi
    korunur). RTH 390 dk olduğu için seans başına tam 2 bar çıkar: 240 dk +
    150 dk. İkinci bar kısadır; bu kaçınılmaz ama her seansta AYNI — tutarlı.
    """
    rule, _spec = TFS[tf]
    if rule is not None:
        return df.resample(rule, label="right", closed="right").agg(agg)
    labels = _session_bucket_labels(df.index, SESSION_RELATIVE_MINUTES[tf])
    return df.groupby(labels).agg(agg)


def session_label_bounds_ns(day) -> tuple[int, int]:
    """`day` SEANSINA ait bar damgalarının [başlangıç, bitiş) ns sınırları (UTC).

    Bir seansın barlarını UTC takvim gününden türetmek YANLIŞTIR. Damga bu
    repoda kapanıştır (right-label, bkz. `_MIN_NS`) ve 1-DAY barı
    `resample("1D", label="right", closed="right")` ile NY yerel saatinde
    (D 00:00, D+1 00:00] kovasına düşüp **D+1 00:00 New York** ile damgalanır —
    DST'ye göre 04:00 ya da 05:00 UTC, yani D'nin UTC gününün DIŞINDA. Naif bir
    [D 00:00 UTC, D+1 00:00 UTC) filtresi bu yüzden D'nin kendi günlük barını
    ıskalar ve onun yerine BİR ÖNCEKİ seansın barını (D 00:00 NY damgalı)
    yakalar.

    Aralık kovanın tam kendisidir: solda AÇIK (D 00:00 NY, D-1 seansının günlük
    barının damgasıdır — ona dokunulmamalı), sağda KAPALI (D+1 00:00 NY, D'nin
    kendi barı). Yarı-açık ns aralığına çevirmek için iki uç da +1 ns kaydırılır.
    Gün içi TF'lerin damgaları (09:31..16:00 NY) zaten bu aralığın içindedir,
    yani tek pencere bütün TF'ler için doğrudur.

    Sınırlar yerel gece yarılarından üretilir, sabit bir UTC ofsetinden değil —
    DST-farkındalıklı olmasının tek sebebi bu. Ters yönü (damga → seans günü)
    `_daily_session_dates` yapar; iki yön aynı sözleşmenin iki yüzüdür.

    TEK KAYNAK: onarım aracı (`repair_massive_intraday.py`) bunu import eder.
    Sözleşme burada değişirse orası da kendiliğinden takip etsin diye; iki yer
    ıraksarsa onarılan gün katalogdaki komşularından farklı damgalanır.
    """
    from datetime import timedelta

    import pandas as pd

    # DST geçişleri pazar 02:00'de olur, gece yarısında değil: yerel gece yarısı
    # her zaman var ve tek anlamlıdır (`tz_localize` belirsizlikte patlamaz).
    # Ertesi gün MUTLAK 24 saat eklenerek değil, TAKVİM günü olarak bulunur.
    start = pd.Timestamp(day).tz_localize(TZ)
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(TZ)
    return int(start.value) + 1, int(end.value) + 1


def tickers_in_catalog(
    catalog_dir: Path, *, venue: str = "NASDAQ", spec: str = "1-MINUTE"
) -> set[str]:
    """Katalogda verilen TF'e sahip bare ticker'lar (arşive hiç dokunmadan)."""
    bar_root = Path(catalog_dir) / "data" / "bar"
    if not bar_root.exists():
        return set()
    suffix = f".{venue}-{spec}-LAST-EXTERNAL"
    return {
        d.name[: -len(suffix)]
        for d in bar_root.iterdir()
        if d.is_dir() and d.name.endswith(suffix)
    }


def tickers_in_other_roots(catalog_dir: Path) -> set[str]:
    """Bare ticker'lar (venue'suz) — DİĞER external kökler ne taşıyor.

    Venue'suz karşılaştırılır: NAU_ev AAPL'ı hangi venue etiketiyle tutarsa
    tutsun, AAPL'ı buraya ikinci kez ingest etmek yine çift kayıttır.
    """
    seen: set[str] = set()
    # `external_catalog_roots()`: EQUITY_CATALOG_DIR geç bağlanır — kökü ilk
    # kez BU koşum oluşturuyorsa bile listede olsun (import anına takılmasın).
    for root in data.external_catalog_roots():
        if Path(root) == catalog_dir:
            continue
        bar_root = Path(root) / "data" / "bar"
        if not bar_root.exists():
            continue
        for d in bar_root.iterdir():
            if d.is_dir():
                parts = d.name.rsplit("-", 4)
                if len(parts) == 5:
                    seen.add(parts[0].split(".")[0])
    return seen


def ingest_minute_bars(
    tickers: set[str],
    y0: int,
    y1: int,
    *,
    venue: str = "NASDAQ",
    root: Path = DEFAULT_FLATFILE_ROOT,
    catalog_dir: Path | None = None,
) -> dict[str, dict]:
    """Faz A — flat-file'lardan hedef ticker'ların 1-MINUTE bar'larını yaz.

    Yıl-parçalı akış (NAU_ev ile aynı): bir yılın günlük dosyaları okunur
    (yalnız hedef ticker satırları), o yılın bar'ları yazılır, bırakılır —
    pik RAM ~1 yıl × hedef sayısı. Hedeflerin mevcut bar dizinleri baştan
    temizlenir (taze ingest, idempotent tekrar koşum).
    """
    import pandas as pd
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    cdir.mkdir(parents=True, exist_ok=True)
    cat = ParquetDataCatalog(str(cdir))
    bar_root = cdir / "data" / "bar"

    for tk in tickers:
        for d in bar_root.glob(f"{tk}.{venue}-*"):
            shutil.rmtree(d, ignore_errors=True)
    cat.write_data([_equity(tk, venue) for tk in sorted(tickers)])

    stats: dict[str, dict] = {
        tk: {"bars": 0, "first": None, "last": None} for tk in tickers
    }
    t0 = time.time()
    for year in range(y0, y1 + 1):
        files = _year_files(root, year)
        if not files:
            continue
        frames = []
        for f in files:
            df = pd.read_csv(f, usecols=_COLS)
            df = df[df["ticker"].isin(tickers)]
            if len(df):
                frames.append(df)
        if not frames:
            _log(f"  {year}: hedef ticker verisi yok")
            continue
        ydf = pd.concat(frames, ignore_index=True)
        frames = None
        for tk, g in ydf.groupby("ticker", sort=False):
            g = g.sort_values("window_start")
            # Bozuk bar KAYNAKTA düzeltilir: buradan geçen bir low=0.00 hem
            # 1-MINUTE'a hem ondan türeyen bütün TF'lere yayılır.
            g, _ = _sanitize_ohlc(g, label=f"{tk} {year}: ")
            if g.empty:
                continue
            bt = BarType.from_str(f"{tk}.{venue}-1-MINUTE-LAST-EXTERNAL")
            ws = g["window_start"].to_numpy()
            o = g["open"].to_numpy()
            h = g["high"].to_numpy()
            lo = g["low"].to_numpy()
            c = g["close"].to_numpy()
            v = g["volume"].to_numpy()
            bars = []
            for i in range(len(ws)):
                ts = int(ws[i]) + _MIN_NS
                bars.append(
                    Bar(
                        bt,
                        Price(float(o[i]), _PRICE_PRECISION),
                        Price(float(h[i]), _PRICE_PRECISION),
                        Price(float(lo[i]), _PRICE_PRECISION),
                        Price(float(c[i]), _PRICE_PRECISION),
                        Quantity(int(v[i]), 0),
                        ts,
                        ts,
                    )
                )
            if bars:
                cat.write_data(bars)
            st = stats[tk]
            st["bars"] += len(bars)
            fd = datetime.fromtimestamp(int(ws[0]) / 1e9, UTC).date().isoformat()
            ld = datetime.fromtimestamp(int(ws[-1]) / 1e9, UTC).date().isoformat()
            if st["first"] is None or fd < st["first"]:
                st["first"] = fd
            if st["last"] is None or ld > st["last"]:
                st["last"] = ld
        ydf = None
        done = sum(1 for s in stats.values() if s["bars"])
        _log(
            f"  {year} bitti ({time.time() - t0:.0f}s, {done}/{len(tickers)} ticker'da veri var)"
        )
    return stats


def build_tf_bars(
    tickers: set[str], *, venue: str = "NASDAQ", catalog_dir: Path | None = None
) -> dict[str, dict]:
    """Faz B — kataloğdaki 1-MINUTE bar'lardan TF bar'ları türet (RTH, right-label).

    NAU_ev build_tf_bars.build_symbol portu. Kripto/manifest-tazeliği dalları
    yok: bu kök yalnız bu ingest'ten beslenir ve TF'ler minute'la aynı koşumda
    üretilir.
    """
    import pandas as pd
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    cat = ParquetDataCatalog(str(cdir))
    out: dict[str, dict] = {}
    for tk in sorted(tickers):
        mins = cat.query(
            data_cls=Bar, identifiers=[f"{tk}.{venue}-1-MINUTE-LAST-EXTERNAL"]
        )
        if not mins:
            _log(f"{tk}: dakikalık bar yok — TF atlandı")
            continue
        rows = [
            (
                b.ts_event,
                float(b.open),
                float(b.high),
                float(b.low),
                float(b.close),
                float(b.volume),
            )
            for b in mins
        ]
        df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
        # Katalogdaki dakikalıklar eski (temizlik öncesi) bir koşumdan gelmiş
        # olabilir — TF türetmeden önce burada da süzülür, yoksa bozuk bar
        # yeniden bütün TF'lere yayılır (`--rebuild-tf` yolu tam olarak bu).
        df, _ = _sanitize_ohlc(df, o="o", h="h", lo="l", c="c", label=f"{tk}: ")
        df["dt"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
        df = (
            df.set_index("dt")
            .sort_index()
            .between_time(RTH_FIRST_LABEL, RTH_LAST_LABEL)
        )
        if df.empty:
            _log(f"{tk}: RTH penceresinde bar kalmadı — TF atlandı")
            continue
        out[tk] = {}
        for tf, (_rule, spec) in TFS.items():
            bt = BarType.from_str(f"{tk}.{venue}-{spec}-LAST-EXTERNAL")
            bar_dir = cdir / "data" / "bar" / str(bt)
            if bar_dir.exists():
                shutil.rmtree(bar_dir, ignore_errors=True)
            agg = resample_tf(
                df,
                tf,
                {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"},
            ).dropna(subset=["c"])
            bars = []
            for ts, row in agg.iterrows():
                ts_ns = int(ts.tz_convert("UTC").value)  # sağ kenar = bar kapanışı
                bars.append(
                    Bar(
                        bt,
                        Price(row["o"], _PRICE_PRECISION),
                        Price(row["h"], _PRICE_PRECISION),
                        Price(row["l"], _PRICE_PRECISION),
                        Price(row["c"], _PRICE_PRECISION),
                        Quantity(int(row["v"]), 0),
                        ts_ns,
                        ts_ns,
                    )
                )
            if bars:
                cat.write_data(bars)
            out[tk][spec] = len(bars)
            _log(f"{tk} {spec}: {len(bars):,} bar yazıldı")
    return out


def _read_manifest(catalog_dir: Path) -> dict:
    mpath = catalog_dir / "_manifest.json"
    return json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}


def _write_manifest_file(catalog_dir: Path, manifest: dict) -> None:
    """Atomik yazım: yarım yazılmış manifest okuyucuyu (data.py) çökertmesin."""
    mpath = catalog_dir / "_manifest.json"
    tmp = mpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(mpath)


def write_manifest(
    stats: dict[str, dict],
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
    split_adjusted: bool = False,
    dividend_adjusted: bool = False,
    source: str = "flatfile:minute_aggs",
    note: str = "Flat-file (UNADJUSTED) ingest; split olan ticker'larda geçmiş fiyat sıçrar.",
) -> int:
    """Manifest EN SONDA (yarım koşum manifest'e yazmaz — NAU_ev sözleşmesi).

    İki AYRI bayrak yazılır. Tek bir `adjusted` alanı iki farklı gerçeği
    karıştırıyordu: Polygon/Massive `/v2/aggs adjusted=true` YALNIZ split
    ayarlar, temettüyü ayarlamaz — ama aynı bayrakla yazılan notlar bir partide
    "split/temettü ayarlı", diğerinde "yalnız split ayarlı" diyordu ve çelişki
    hiçbir yerden görünmüyordu. Eski `adjusted` alanı geriye uyumluluk için
    korunur ve `split_adjusted` ile EŞİTLENİR (okuyucular kırılmasın).

    `coverage_gaps` burada hesaplanmaz ama varsa KORUNUR — pahalı olmasa da
    ayrı bir geçiştir (`write_coverage_gaps`), manifest'i sıfırlamamalı.
    """
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    manifest = _read_manifest(cdir)
    now = datetime.now(UTC).isoformat()
    written = 0
    for tk in sorted(stats):
        st = stats[tk]
        if st["bars"] == 0:
            continue
        span = (
            datetime.fromisoformat(st["last"]).date()
            - datetime.fromisoformat(st["first"]).date()
        ).days / 365.25
        prev = manifest.get(tk) if isinstance(manifest.get(tk), dict) else {}
        manifest[tk] = {
            "symbol": tk,
            "venue": venue,
            "bars": st["bars"],
            "first": st["first"],
            "last": st["last"],
            "years": round(span, 2),
            "ok": True,
            "smoke": False,
            "adjusted": split_adjusted,  # eski alan = split_adjusted (geri uyum)
            "split_adjusted": split_adjusted,
            "dividend_adjusted": dividend_adjusted,
            "source": source,
            "downloaded_at": now,
            "ingested_at": now,
            "note": note,
            "coverage_gaps": prev.get("coverage_gaps", []),
        }
        written += 1
    _write_manifest_file(cdir, manifest)
    return written


def _dividend_adjusted_for_source(source: str) -> bool | None:
    """source → dividend_adjusted; bilinmeyen kaynakta None (UYDURMA YOK)."""
    for key, val in _DIVIDEND_ADJUSTED_BY_SOURCE.items():
        if source == key or (key.endswith(":") and source.startswith(key)):
            return val
    return None


def migrate_manifest(*, catalog_dir: Path | None = None, dry_run: bool = False) -> dict:
    """Eski tek-bayraklı manifest'i split/temettü ayrımına geçir.

    `split_adjusted` eski `adjusted` alanından okunur (kaynağa bakıp
    UYDURULMAZ: `--no-adjusted` ile çekilmiş bir REST partisi de olabilir).
    `dividend_adjusted` yalnız kaynağı BİLİNEN girdilerde doldurulur, diğerinde
    None kalır. Bir de çelişkili notlar gerçeğe uydurulur: 2026-08-03 partisi
    (HOOD/AMD/AMZN/GOOGL/META/TSLA) `adjusted=true` ile "split/temettü ayarlı"
    diyordu — Polygon/Massive `/v2/aggs` temettü ayarlamaz, o not yanlıştı.

    Dönen sözlük: ticker → değişen alanlar (idempotent; ikinci koşum boş döner).
    """
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    manifest = _read_manifest(cdir)
    changed: dict[str, dict] = {}
    for tk, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        diff: dict = {}
        old = entry.get("adjusted")
        split = old if isinstance(old, bool) else None
        if "split_adjusted" not in entry:
            diff["split_adjusted"] = split
        div = entry.get("dividend_adjusted")
        if "dividend_adjusted" not in entry:
            div = _dividend_adjusted_for_source(str(entry.get("source", "")))
            diff["dividend_adjusted"] = div
        note = str(entry.get("note", ""))
        if div is False and "split/temettü ayarlı" in note:
            diff["note"] = note.replace(
                "split/temettü ayarlı", "yalnız split ayarlı (temettü ayarlanmaz)"
            )
        if not diff:
            continue
        changed[tk] = diff
        if not dry_run:
            entry.update(diff)
            entry.setdefault("split_adjusted", split)
            entry.setdefault("dividend_adjusted", div)
    if changed and not dry_run:
        _write_manifest_file(cdir, manifest)
    for tk, diff in sorted(changed.items()):
        _log(f"  manifest göç {tk}: {', '.join(sorted(diff))}")
    _log(f"manifest göçü: {len(changed)}/{len(manifest)} girdi güncellendi")
    return changed


def _daily_session_dates(cat, tk: str, venue: str) -> list:
    """Bir ticker'ın 1-DAY bar'larının SEANS tarihleri (NY yerel)."""
    import pandas as pd
    from nautilus_trader.model.data import Bar

    bars = cat.query(data_cls=Bar, identifiers=[f"{tk}.{venue}-1-DAY-LAST-EXTERNAL"])
    if not bars:
        return []
    ts = pd.to_datetime([b.ts_event for b in bars], unit="ns", utc=True).tz_convert(TZ)
    # Right-label: 1-DAY damgası kovanın SONUDUR (ertesi günün yerel gece
    # yarısı). 1 ns geri gitmek kovanın son anını verir → seansın kendi tarihi,
    # DST'den bağımsız.
    return sorted({t.date() for t in (ts - pd.Timedelta(1, "ns"))})


def compute_coverage_gaps(
    tickers: set[str] | None = None,
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
    min_sessions: int = GAP_MIN_SESSIONS,
) -> dict[str, list[dict]]:
    """Ticker başına kapsam boşlukları — 1-DAY serisinden (ucuz).

    Referans takvim ayrı bir borsa takvimi DEĞİL, kataloğun kendisidir:
    bütün ticker'ların günlük bar tarihlerinin BİRLEŞİMİ zaten gerçek işlem
    günleri kümesidir (tatiller kendiliğinden dışarıda kalır), ek bağımlılık ve
    yanlış tatil listesi riski olmadan. Boşluk = ticker'ın KENDİ ilk/son tarihi
    ARASINDA referans takvimde olup kendisinde olmayan ardışık seanslar; serinin
    başından öncesi/sonundan sonrası boşluk değil, kapsam sınırıdır.

    Neden görünür kılınıyor: QQQ kendi serisinde 2004-12 → 2011-03 arası boş (o
    dönem ticker QQQQ'ydu) ve bu delik 2008 ayı piyasasının tamamını gizleyip
    al-tut maksimum düşüşünü %53,5 yerine %35,6 gösteriyor. Sembol listeden
    ÇIKARILMAZ — kullanıcı bilerek seçebilmeli, ama boşluğu GÖRMELİ.
    """
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    cat = ParquetDataCatalog(str(cdir))
    known = tickers_in_catalog(cdir, venue=venue, spec="1-DAY")
    dates = {tk: _daily_session_dates(cat, tk, venue) for tk in sorted(known)}
    calendar = sorted({d for ds in dates.values() for d in ds})
    targets = sorted(tickers) if tickers else sorted(dates)

    out: dict[str, list[dict]] = {}
    for tk in targets:
        own = set(dates.get(tk) or [])
        out[tk] = []
        if not own:
            continue
        first, last = min(own), max(own)
        run: list = []
        for d in calendar:
            if d < first or d > last:
                continue
            if d in own:
                if len(run) >= min_sessions:
                    out[tk].append(
                        {
                            "start": run[0].isoformat(),
                            "end": run[-1].isoformat(),
                            "missing_sessions": len(run),
                        }
                    )
                run = []
            else:
                run.append(d)
        # Son run kapanış barına ulaşmadan bitemez (last kendi serisinde var),
        # yine de savunmacı olarak boşaltılır.
        if len(run) >= min_sessions:
            out[tk].append(
                {
                    "start": run[0].isoformat(),
                    "end": run[-1].isoformat(),
                    "missing_sessions": len(run),
                }
            )
    return out


def write_coverage_gaps(
    tickers: set[str] | None = None,
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
    min_sessions: int = GAP_MIN_SESSIONS,
) -> dict[str, list[dict]]:
    """`compute_coverage_gaps` sonucunu manifest'e yaz (ticker başına alan)."""
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    gaps = compute_coverage_gaps(
        tickers, venue=venue, catalog_dir=cdir, min_sessions=min_sessions
    )
    manifest = _read_manifest(cdir)
    touched = 0
    for tk, g in gaps.items():
        entry = manifest.get(tk)
        if not isinstance(entry, dict):
            continue
        entry["coverage_gaps"] = g
        touched += 1
        if g:
            _log(
                f"  {tk}: {len(g)} kapsam boşluğu — "
                + "; ".join(
                    f"{x['start']}→{x['end']} ({x['missing_sessions']} seans)"
                    for x in g
                )
            )
    if touched:
        _write_manifest_file(cdir, manifest)
    _log(f"coverage_gaps: {touched} manifest girdisi güncellendi")
    return gaps


def ingest(
    tickers: set[str],
    years: str,
    *,
    venue: str = "NASDAQ",
    root: Path = DEFAULT_FLATFILE_ROOT,
    catalog_dir: Path | None = None,
    force: bool = False,
) -> dict[str, dict]:
    """Tam akış: guard → minute ingest → TF türetme → manifest. Stats döner."""
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    targets = {t.strip().upper() for t in tickers if t.strip()}
    if not force:
        elsewhere = targets & tickers_in_other_roots(cdir)
        if elsewhere:
            _log(
                f"{len(elsewhere)} ticker atlandı — başka external katalogda zaten var "
                f"(adjusted sürümü muhtemelen daha iyi; --force ile zorla): "
                f"{sorted(elsewhere)[:12]}"
            )
            targets -= elsewhere
    if not targets:
        _log("hedef ticker kalmadı")
        return {}
    yr = years.split("-")
    y0, y1 = int(yr[0]), int(yr[-1])
    if not (root / "minute_aggs_v1").is_dir():
        raise FileNotFoundError(f"{root / 'minute_aggs_v1'} yok")
    _log(
        f"{len(targets)} ticker ingest ({y0}-{y1}) venue={venue} · kaynak={root} · hedef={cdir}"
    )
    stats = ingest_minute_bars(
        targets, y0, y1, venue=venue, root=root, catalog_dir=cdir
    )
    with_bars = {tk for tk, st in stats.items() if st["bars"]}
    build_tf_bars(with_bars, venue=venue, catalog_dir=cdir)
    written = write_manifest(stats, venue=venue, catalog_dir=cdir)
    # Kapsam boşlukları manifest yazıldıktan SONRA hesaplanır: girdi yoksa
    # yazacak yer de yok. Ucuz (yalnız 1-DAY serileri okunur).
    write_coverage_gaps(venue=venue, catalog_dir=cdir)
    total = sum(st["bars"] for st in stats.values())
    _log(f"=== {written} ticker · {total:,} minute bar · TF'ler türetildi · {cdir} ===")
    return stats


def rebuild_tf(
    tickers: set[str] | None = None,
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
) -> dict[str, dict]:
    """Arşive HİÇ dokunmadan, katalogdaki dakikalıklardan TF'leri yeniden türet.

    RTH sınırı / bozuk-OHLC temizliği / seans-göreli 4-HOUR düzeltmeleri mevcut
    TF barlarını geçersiz kılıyor, ama 1-MINUTE barlar katalogda duruyor — 78
    GB'lık flat-file arşivini yeniden taramaya gerek yok. Hedef verilmezse
    katalogda 1-MINUTE'ü olan HER ticker yeniden türetilir.
    """
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    targets = tickers or tickers_in_catalog(cdir, venue=venue, spec="1-MINUTE")
    if not targets:
        _log(f"katalogda 1-MINUTE barı olan ticker yok: {cdir}")
        return {}
    _log(f"{len(targets)} ticker için TF yeniden türetiliyor · {cdir}")
    out = build_tf_bars(set(targets), venue=venue, catalog_dir=cdir)
    write_coverage_gaps(venue=venue, catalog_dir=cdir)
    _log(f"=== TF yeniden üretimi bitti ({len(out)} ticker) · {cdir} ===")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tickers", default="", help="virgüllü liste")
    ap.add_argument(
        "--tickers-file", default="", help="satır-başı-ticker dosyası (# yorum)"
    )
    ap.add_argument("--years", default="2003-2026", help="YYYY veya YYYY-YYYY")
    ap.add_argument("--venue", default="NASDAQ", help="Nautilus venue etiketi")
    ap.add_argument("--root", default=str(DEFAULT_FLATFILE_ROOT))
    ap.add_argument(
        "--catalog-dir", default="", help="hedef katalog kökü (varsayılan: proje kökü)"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="başka external katalogda olan ticker'ı da ingest et",
    )
    # Bakım modları: hiçbiri flat-file arşivine dokunmaz, --tickers istemez.
    ap.add_argument(
        "--rebuild-tf",
        action="store_true",
        help="arşive dokunmadan katalogdaki dakikalıklardan TF'leri yeniden türet",
    )
    ap.add_argument(
        "--migrate-manifest",
        action="store_true",
        help="manifest'i split_adjusted/dividend_adjusted şemasına geçir",
    )
    ap.add_argument(
        "--coverage-gaps",
        action="store_true",
        help="1-DAY serilerinden kapsam boşluklarını hesapla ve manifest'e yaz",
    )
    args = ap.parse_args()

    cdir = Path(args.catalog_dir) if args.catalog_dir else data.EQUITY_CATALOG_DIR
    targets: set[str] = set()
    if args.tickers_file:
        for line in Path(args.tickers_file).read_text(encoding="utf-8").splitlines():
            s = line.strip().upper()
            if s and not s.startswith("#"):
                targets.add(s)
    targets |= {t.strip().upper() for t in args.tickers.split(",") if t.strip()}

    if args.migrate_manifest or args.coverage_gaps or args.rebuild_tf:
        if args.migrate_manifest:
            migrate_manifest(catalog_dir=cdir)
        if args.rebuild_tf:
            rebuild_tf(targets or None, venue=args.venue, catalog_dir=cdir)
        elif args.coverage_gaps:
            write_coverage_gaps(targets or None, venue=args.venue, catalog_dir=cdir)
        return

    if not targets:
        _log("HATA: hedef ticker yok (--tickers veya --tickers-file)")
        sys.exit(1)
    try:
        ingest(
            targets,
            args.years,
            venue=args.venue,
            root=Path(args.root),
            catalog_dir=cdir,
            force=args.force,
        )
    except FileNotFoundError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
