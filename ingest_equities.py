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

Kullanım (repo kökü, PYTHONUTF8=1):
  python ingest_equities.py --tickers HOOD,RIVN --years 2020-2026
  python ingest_equities.py --tickers-file tickers.txt --years 2003-2026

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]],
[[index_backtest_via_equity_proxy]], [[instruments]]

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

# TF → (pandas resample kuralı, Nautilus spec) — NAU_ev build_tf_bars.TFS portu
TFS = {
    "5": ("5min", "5-MINUTE"),
    "15": ("15min", "15-MINUTE"),
    "60": ("60min", "1-HOUR"),
    "240": ("240min", "4-HOUR"),
    "D": ("1D", "1-DAY"),
}
RTH_START, RTH_END, TZ = "09:30", "16:00", "America/New_York"


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


def tickers_in_other_roots(catalog_dir: Path) -> set[str]:
    """Bare ticker'lar (venue'suz) — DİĞER external kökler ne taşıyor.

    Venue'suz karşılaştırılır: NAU_ev AAPL'ı hangi venue etiketiyle tutarsa
    tutsun, AAPL'ı buraya ikinci kez ingest etmek yine çift kayıttır.
    """
    seen: set[str] = set()
    for root in data.EXTERNAL_CATALOGS:
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
        df["dt"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
        df = df.set_index("dt").sort_index().between_time(RTH_START, RTH_END)
        if df.empty:
            _log(f"{tk}: RTH penceresinde bar kalmadı — TF atlandı")
            continue
        out[tk] = {}
        for _tf, (rule, spec) in TFS.items():
            bt = BarType.from_str(f"{tk}.{venue}-{spec}-LAST-EXTERNAL")
            bar_dir = cdir / "data" / "bar" / str(bt)
            if bar_dir.exists():
                shutil.rmtree(bar_dir, ignore_errors=True)
            agg = (
                df.resample(rule, label="right", closed="right")
                .agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
                .dropna(subset=["c"])
            )
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


def write_manifest(
    stats: dict[str, dict], *, venue: str = "NASDAQ", catalog_dir: Path | None = None
) -> int:
    """Manifest EN SONDA (yarım koşum manifest'e yazmaz — NAU_ev sözleşmesi).

    data.py'nin M21 okuyucusu (`_external_manifest`) buradaki `adjusted: false`
    bayrağını /data rozetine çevirir.
    """
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    mpath = cdir / "_manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
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
        manifest[tk] = {
            "symbol": tk,
            "venue": venue,
            "bars": st["bars"],
            "first": st["first"],
            "last": st["last"],
            "years": round(span, 2),
            "ok": True,
            "smoke": False,
            "adjusted": False,
            "source": "flatfile:minute_aggs",
            "downloaded_at": now,
            "ingested_at": now,
            "note": "Flat-file (UNADJUSTED) ingest; split olan ticker'larda geçmiş fiyat sıçrar.",
        }
        written += 1
    tmp = mpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(mpath)
    return written


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
    total = sum(st["bars"] for st in stats.values())
    _log(f"=== {written} ticker · {total:,} minute bar · TF'ler türetildi · {cdir} ===")
    return stats


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
        "--force",
        action="store_true",
        help="başka external katalogda olan ticker'ı da ingest et",
    )
    args = ap.parse_args()

    targets: set[str] = set()
    if args.tickers_file:
        for line in Path(args.tickers_file).read_text(encoding="utf-8").splitlines():
            s = line.strip().upper()
            if s and not s.startswith("#"):
                targets.add(s)
    targets |= {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
    if not targets:
        _log("HATA: hedef ticker yok (--tickers veya --tickers-file)")
        sys.exit(1)
    try:
        ingest(
            targets,
            args.years,
            venue=args.venue,
            root=Path(args.root),
            force=args.force,
        )
    except FileNotFoundError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
