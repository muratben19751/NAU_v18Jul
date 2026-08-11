"""Data ingestion — Bybit v5 klines, US-index tick CSVs.

Provides the DataFrame surface consumed by ``backtest.py``. Every source
maintains its own on-disk parquet cache under ``~/.cache/nautilus_web_app/``.

Wiki References
---------------
Bkz: [[data_engine]], [[data_wranglers]], [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]], [[index_backtest_via_equity_proxy]], [[nau_performans_denetimi]], [[us_equity_katalog_veri_butunlugu]]

Eşzamanlılık (2026-08-11): `load_index_bars` ve `load_external_bars` de artık
`load_bybit_bars`'ın desenini izliyor — okuma kilitsiz, oku-değiştir-yaz dizisi
`_cache_lock` altında ve kilit alındıktan sonra disk tazeden okunuyor. Harici
katalog kökü GEÇ BAĞLANIR (`external_catalog_roots`) ve manifest/enstrüman-meta
önbellekleri `(mtime, size)` ile geçersizleşir; okuma HATASI önbelleğe yazılmaz.

Feeds raw OHLCV frames into `backtest.py`. Cache-tail-forward parquets are the
local analog of [[parquet_data_catalog]] — same nanosecond-timestamp Parquet idea,
smaller scope.

`_bybit_rows()` (2026-08-08 DeepR finding) is now TTL-cached (`_BYBIT_ROWS_TTL_S`,
30s) instead of rebuilding all 72 symbol×category×interval cells on every
`/data` GET; `refresh_row("bybit", ...)` invalidates it explicitly so a
forced refresh never hands back the pre-refresh row.

`_external_catalog_source_label()` (2026-08-08 DeepR finding) — the /data
external-catalog panel's "(NAU_ev)" title was hardcoded even when the
NAU_ev root was missing and `EQUITY_CATALOG_DIR` (this project's own
ingest) silently filled the panel instead; the title now reflects which
root(s) actually contributed data.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import requests

log = logging.getLogger(__name__)

# Thread-local HTTP session: reuses TCP+TLS connections within each thread
# (fetch workers, daemon threads). Created on first use per thread.
_bybit_session = threading.local()


def _get_bybit_session() -> requests.Session:
    if not hasattr(_bybit_session, "s"):
        _bybit_session.s = requests.Session()
    return _bybit_session.s


CACHE_DIR = Path.home() / ".cache" / "nautilus_web_app"
BYBIT_CACHE_DIR = CACHE_DIR / "bybit"
# Pandas cache for bars decoded from read-only EXTERNAL catalogs — lives here,
# never inside the external root (external catalogs are reference data).
EXTERNAL_CACHE_DIR = CACHE_DIR / "external"

# Nautilus ParquetDataCatalog root — stores Bar/Tick/Instrument objects in
# the native Arrow schema (nanosecond int64 timestamps, fixed-point prices).
# Distinct from the pandas caches above: those are ephemeral scratch space;
# this is the authoritative catalog consumed by BacktestNode and LiveNode.
NAUTILUS_CATALOG_DIR = CACHE_DIR / "nautilus_catalog"

# ---- US indices (Polygon-style tick CSVs on NFS) ----
# NFS mount is unreliable for large sequential reads. Set NAUTILUS_INDEX_ROOT
# to a local mirror when the remote path is not usable.
INDEX_ROOT = Path(
    os.environ.get(
        "NAUTILUS_INDEX_ROOT",
        "/Users/i034216/Z/us_indices/values_v1",
    )
)
INDEX_CACHE_DIR = CACHE_DIR / "indices"
TICKER_REGISTRY = INDEX_CACHE_DIR / "_tickers.json"

# ---- External read-only Nautilus catalogs surfaced on the /data page ----
# Other projects' ParquetDataCatalog roots that this app references in place
# (never copies). os.pathsep-separated paths, overridable via env var.
# Default: the NAU_ev backtest desk catalog (591 US-equity instruments).
# The desk moved drives at some point — E:\myAI_Projects\NAU_ev no longer
# exists anywhere; D:\NAU_ev is where it lives on this box (verified
# 2026-07-27: 3,511 bar series). With the old default the /data external
# panel, the Lab pickers and QQQ/SPX backtest loads all silently emptied.
EXTERNAL_CATALOGS: list[Path] = [
    Path(p.strip())
    for p in os.environ.get(
        "NAUTILUS_EXTERNAL_CATALOGS",
        r"D:\NAU_ev\backend\data\catalog",
    ).split(os.pathsep)
    if p.strip()
]

# Bu projenin KENDİ ingest'inin yazdığı kök (ingest_equities.py — Massive/Polygon
# flat-file arşivinden). NAUTILUS_CATALOG_DIR değil: `data.py rebuild` orayı
# siler; buradaki bar'lar 78 GB'lık arşiv taramasıyla üretiliyor, cache değil.
# Sona eklenir ki aynı enstrüman iki kökte varsa NAU_ev'inki (adjusted) kazansın;
# env var ayarlıyken de eklenir — kendi ingest ettiğin verinin panelden kaybolması
# en büyük sürpriz olurdu. Yalnız var olduğunda: L31 uyarısı taze kutuda susar.
EQUITY_CATALOG_DIR = CACHE_DIR / "equity_catalog"
if EQUITY_CATALOG_DIR.exists() and EQUITY_CATALOG_DIR not in EXTERNAL_CATALOGS:
    EXTERNAL_CATALOGS.append(EQUITY_CATALOG_DIR)

# Import anında kurulan varsayılan liste NESNESİ (kimlik karşılaştırması için
# saklanır, kopyası değil). `external_catalog_roots` yalnız bu nesne
# yerindeyken geç bağlama yapar: testler `EXTERNAL_CATALOGS`'u kendi tmp
# kökleriyle DEĞİŞTİRİYOR ve oraya operatörün gerçek kataloğunu sızdırmak,
# izole olması gereken testi kirletirdi.
_EXTERNAL_CATALOGS_DEFAULT = EXTERNAL_CATALOGS

# L31: a non-existent root silently turned into an empty panel — warn at module load.
for _ext_root in EXTERNAL_CATALOGS:
    if not _ext_root.exists():
        log.warning(
            "EXTERNAL_CATALOGS root does not exist: %s — the /data page's external "
            "catalog panel will be empty. This default points at the NAU_ev desk on "
            "another machine; set NAUTILUS_EXTERNAL_CATALOGS (%s-separated paths) to "
            "this box's catalog root, or ignore this if you only use Bybit/Index data.",
            _ext_root,
            os.pathsep,
        )

# Per-kök önbellekler: {kök: (imza, yük)}.
#
# DeepR 2026-08-11 [ORTA]: eskiden yalnız {kök: yük} idi ve gerekçe "the
# external catalog is read-only reference data, so it never changes" diyordu.
# Bu varsayım NAU_ev kökü için doğru olabilir ama projenin KENDİ ingest'inin
# yazdığı `EQUITY_CATALOG_DIR` için yanlış: `ingest_equities` /
# `download_massive` ayrı bir CLI süreci olarak yazıyor, çalışan sunucu
# değişikliği ASLA görmüyordu. Sonuç tutarsız bir tabloydu — yeni ticker
# picker'da GÖRÜNÜYOR (`_external_catalog_rows` her çağrıda diski tarıyor) ama
# `split_adjusted`/`coverage_gaps` bayat manifest'ten okunuyor, yani UNADJUSTED
# uyarısı tam da yeni gelen veri için susuyordu. İmza `(mtime, size)` tabanlı —
# `composer._read_catalog_raw` ve `custom_block_store._read_registry` ile aynı
# desen.
_EXT_INSTRUMENT_META: dict[str, tuple[tuple, dict]] = {}
_EXT_MANIFEST: dict[str, tuple[tuple, dict]] = {}

# Enstrüman meta'sı okunamayan kökler — HATA SONUCU CACHE'LENMEZ (her çağrıda
# yeniden denenir), bu küme yalnız aynı uyarıyı her çağrıda basmamak için var.
_EXT_META_WARNED: set[str] = set()


def external_catalog_roots() -> list[Path]:
    """Aktif harici katalog kökleri — `EQUITY_CATALOG_DIR` GEÇ BAĞLANIR.

    DeepR 2026-08-11 [ORTA]: bu kök yalnız MODÜL IMPORT ANINDA listeye
    ekleniyordu. `ingest_equities.py` / `download_massive.py` ayrı bir CLI
    süreci olarak kökü İLK KEZ oluşturursa, çalışan web sunucusu onu asla
    görmüyor, restart gerekiyordu. Var olup olmadığı artık her çağrıda
    sorulur (tek `stat()`).

    Geç bağlama YALNIZ varsayılan liste yerindeyken yapılır: çağıran
    `EXTERNAL_CATALOGS`'u bilinçli olarak değiştirdiyse (testler tmp köküyle
    tam olarak bunu yapıyor) o liste son sözdür.
    """
    roots = list(EXTERNAL_CATALOGS)
    if EXTERNAL_CATALOGS is _EXTERNAL_CATALOGS_DEFAULT:
        try:
            fresh = EQUITY_CATALOG_DIR.exists()
        except OSError:
            fresh = False
        if fresh and EQUITY_CATALOG_DIR not in roots:
            roots.append(EQUITY_CATALOG_DIR)
    return roots


def _stat_sig(path: Path) -> tuple:
    """`(mtime_ns, size)` — dosya/dizin yoksa boş demet."""
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


# ---- Concurrency + atomic write helpers (M3) --------------------------
#
# If two requests (thread or process) do a read-modify-write on the same
# cache file at the same time, the last one wins and the bars in between
# are lost. A two-layer lock:
#   1) in-process: a threading.Lock per key
#   2) inter-process: an O_CREAT|O_EXCL lock file next to the target file
_PROC_LOCKS: dict[str, threading.Lock] = {}
_PROC_LOCKS_GUARD = threading.Lock()


# Kilidi bekleme süresi. DeepR 2026-08-11 [YÜKSEK]: 120 s idi, oysa bu modülün
# kendi belgelediği GERÇEK tutma süresi ~10 dk (3 yıllık 1m backfill). Yani bir
# indirme sürerken aynı seriyi YAZMAK isteyen ikinci çağıran, işin bitmesine
# daha 8 dakika varken TimeoutError alıyordu — beklemenin tek anlamı olan şeyi
# (öbürünün getirdiği veriyi kullanmak) tam da kaçırarak. Şimdi bekleme gerçek
# tutma süresinin üstünde ama "terk edilmiş" eşiğinin (stale_after) altında:
# canlı bir iş beklenir, ölmüş bir kilit kırılır. Yalnız YAZMA yolu bekler —
# okuma yolu bu kilide hiç girmez (bkz. `load_bybit_bars`).
_CACHE_LOCK_TIMEOUT_S = 900.0


@contextmanager
def _cache_lock(
    lock_path: Path,
    timeout: float = _CACHE_LOCK_TIMEOUT_S,
    stale_after: float = 1800.0,
):
    """Simple in-process + inter-process lock.

    Raises TimeoutError if not acquired within `timeout` seconds. A lock file
    OLDER than `stale_after` (default 30 min) seconds is considered leftover
    from a crashed process and is broken once. M121: the stale threshold used
    to be THE SAME as the wait-timeout (60s), and long backfills (3 years of 1m
    hold the lock ~10 min) routinely exceeded 60s, so a second process would
    break a LIVE lock. The stale threshold is now much longer than any
    legitimate hold — only a truly abandoned lock is broken.
    """
    key = str(lock_path)
    with _PROC_LOCKS_GUARD:
        tlock = _PROC_LOCKS.setdefault(key, threading.Lock())
    if not tlock.acquire(timeout=timeout):
        raise TimeoutError(f"could not acquire in-process lock: {lock_path}")
    fd: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        broke_stale = False
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    try:
                        age = time.time() - lock_path.stat().st_mtime
                    except OSError:
                        continue  # file was deleted meanwhile — retry immediately
                    if age > stale_after and not broke_stale:
                        log.warning(
                            "breaking stale lock file (%.0fs): %s", age, lock_path
                        )
                        try:
                            lock_path.unlink()
                        except OSError:
                            pass
                        broke_stale = True
                        deadline = time.monotonic() + 5.0
                        continue
                    raise TimeoutError(f"lock timeout: {lock_path}") from None
                time.sleep(0.1)
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock_path.unlink()
            except OSError:
                pass
        tlock.release()


# Windows'ta `os.replace`, hedef dosya BAŞKA biri tarafından açıkken
# PermissionError verir (POSIX'te vermez). Okuma yolu artık kilitsiz olduğuna
# göre (DeepR 2026-08-11 [YÜKSEK]) yazarın tam o anda okuyan birine denk gelmesi
# normal bir olaydır ve bir parquet okuması milisaniyeler sürer: kısa bir yeniden
# deneme, "veri kaybettim" ile "50 ms bekledim" arasındaki fark.
_REPLACE_RETRY_WAITS = (0.05, 0.15, 0.4)


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """`os.replace` + Windows paylaşım ihlaline karşı kısa yeniden deneme."""
    for wait in (*_REPLACE_RETRY_WAITS, None):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def _tmp_sibling(path: Path) -> Path:
    """Benzersiz kardeş temp adı — SÜREÇ + THREAD.

    DeepR 2026-08-11 [ORTA]: ad yalnız `os.getpid()` içeriyordu, yani aynı
    süreçteki iki THREAD için AYNIYDI: biri diğerinin yarım dosyasını
    `os.replace` edebilir ya da `finally` bloğunda silebilirdi. Yazma yolları
    artık per-key `_cache_lock` altında (aynı hedefe iki thread giremez), ama
    ad benzersizliği ucuz bir ikinci savunma — `custom_block_store._write_registry`
    de tam bu gerekçeyle benzersiz kardeş ad kullanıyor.
    """
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")


def _atomic_to_parquet(df: pd.DataFrame, path: Path) -> None:
    """M3a: to_parquet → temp file + os.replace — no half-written parquet remains."""
    tmp = _tmp_sibling(path)
    try:
        df.to_parquet(tmp)
        _replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_json(obj, path: Path) -> None:
    """Write JSON sidecars atomically too (same pattern as M3a)."""
    tmp = _tmp_sibling(path)
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


Granularity = Literal["1d", "1m", "5m", "15m", "60m"]
# pandas resample rule (converts raw NFS ticks into OHLCV via _aggregate_ohlc)
_GRAN_RULE = {
    "1d": "1D",
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "60m": "60min",
}
# Nautilus BarType step/aggregation: granularity → (step, aggregation).
# _index_rows' DSL badges and _make_index_bar_type derive from here; single source.
_GRAN_BARSPEC: dict[str, tuple[int, str]] = {
    "1d": (1, "DAY"),
    "1m": (1, "MINUTE"),
    "5m": (5, "MINUTE"),
    "15m": (15, "MINUTE"),
    "60m": (1, "HOUR"),
}


def _ticker_to_filename(t: str) -> str:
    """`I:AAVE100` -> `I_AAVE100` (filesystem-safe; Nautilus Symbol keeps raw).

    Path-traversal guard, same contract as `_bybit_cache_path`: every caller
    builds `INDEX_CACHE_DIR / f"{this}_{granularity}.parquet"` from a `ticker`
    that arrives straight from the `/backtest/run` and `/backtest/sweep` form
    bodies. Replacing only ":" and "/" left "\\" and ".." intact, so
    `..\\..\\Users\\…\\evil` escaped the cache directory and could overwrite an
    arbitrary file. The ticker is reduced to a filesystem-safe charset (ASCII
    alnum plus `_-.`), which preserves the historical "I:SPX" -> "I_SPX" and
    "BRK.B" -> "BRK.B" filenames, and any ".." run is collapsed. A ticker with
    nothing usable left raises ValueError BEFORE any file is touched.
    """
    safe = "".join(
        c if ((c.isascii() and c.isalnum()) or c in "_-.") else "_" for c in t
    )
    while ".." in safe:
        safe = safe.replace("..", "_")
    if not safe.strip("._-"):
        raise ValueError(f"invalid ticker: {t!r}")
    return safe


def _index_file_for(d: date) -> Path:
    return INDEX_ROOT / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.isoformat()}.csv.gz"


def _latest_available_file() -> Path | None:
    if not INDEX_ROOT.exists():
        return None
    files = sorted(INDEX_ROOT.glob("*/*/*.csv.gz"), reverse=True)
    return files[0] if files else None


# DeepR 2026-08-09 [DÜŞÜK]: discover_index_tickers() and _index_rows_uncached()
# each parsed TICKER_REGISTRY independently. Shared here so there's one read
# path; a corrupt/empty registry is non-fatal in both callers (matches
# discover_index_tickers()'s pre-existing "fall through and rescan" handling).
def _read_ticker_registry() -> list[str] | None:
    """Return the cached ticker list, or None if missing/corrupt/empty."""
    if not TICKER_REGISTRY.exists():
        return None
    try:
        reg = json.loads(TICKER_REGISTRY.read_text())
    except Exception:
        return None
    return reg.get("tickers") or None


def discover_index_tickers(force: bool = False) -> list[str]:
    """Return sorted list of unique tickers seen in the newest daily file.

    Caches to `TICKER_REGISTRY` as JSON. First run scans a ~2 GB gzip via awk
    (streamed) and takes minutes; subsequent calls are instant.
    """
    if not force:
        cached = _read_ticker_registry()
        if cached is not None:
            return cached

    src = _latest_available_file()
    if src is None:
        # FileNotFoundError, not RuntimeError: the route maps the former to 404
        # ("no index data on this box") and everything else to a 500 stack.
        raise FileNotFoundError(f"no index files found under {INDEX_ROOT}")

    # Pure-Python streaming scan. This used to shell out to
    # `bash -c "gunzip -c … | awk …"`, which does not exist on a stock Windows
    # box (the app's primary platform): `bash` resolved to the WSL launcher and
    # the whole index pipeline failed with execvpe(/bin/bash). gzip+csv reads the
    # same file incrementally, so memory stays flat on the ~2 GB dailies.
    tickers_seen: set[str] = set()
    with gzip.open(src, mode="rt", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if row and row[0].strip():
                tickers_seen.add(row[0].strip())
    tickers = sorted(tickers_seen)

    INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TICKER_REGISTRY.write_text(
        json.dumps(
            {
                "discovered_at": datetime.now(UTC).isoformat(),
                "source_file": str(src),
                "tickers": tickers,
            },
            indent=2,
        )
    )
    return tickers


def _stream_ticker_rows(ticker: str, day: date) -> pd.DataFrame:
    """Filter one day's CSV.gz for `ticker` → DataFrame.

    Returns empty DataFrame with the same columns if the day is missing.

    Pure Python (gzip + csv), for the same reason as `discover_index_tickers`:
    the previous `bash -c "gunzip | awk"` pipe made the whole index path
    unusable on Windows. Rows are filtered while streaming, so only the matching
    ticker's rows are held in memory.
    """
    empty_cols = ["ticker", "value", "timestamp"]
    src = _index_file_for(day)
    if not src.exists():
        return pd.DataFrame(columns=empty_cols)

    try:
        with gzip.open(src, mode="rt", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                # Missing/corrupt/empty gzip — match the missing-day contract.
                return pd.DataFrame(columns=empty_cols)
            matched = [row for row in reader if row and row[0] == ticker]
    except (OSError, EOFError, csv.Error) as e:
        log.warning("unreadable index file %s: %s", src, e)
        return pd.DataFrame(columns=empty_cols)

    if not matched:
        return pd.DataFrame(columns=empty_cols)
    df = pd.DataFrame(matched, columns=header)
    # csv gives str; the awk|read_csv path used to infer these types.
    for col in ("value", "timestamp"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _aggregate_ohlc(rows: pd.DataFrame, granularity: Granularity) -> pd.DataFrame:
    """Convert raw (ticker,value,timestamp) rows into OHLCV bars."""
    if rows.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(rows["timestamp"], unit="ns", utc=True)
    s = pd.Series(rows["value"].astype(float).values, index=ts, name="value")
    rule = _GRAN_RULE[granularity]
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


def load_index_bars(
    ticker: str,
    start: date,
    end: date,
    granularity: Granularity = "1d",
    force: bool = False,
) -> pd.DataFrame:
    """Load OHLCV bars for a single index ticker over [start, end] (inclusive).

    Reads from a per-ticker parquet cache and incrementally fills missing
    days by streaming the NFS gzip file through awk. Returns a DataFrame
    with columns [open, high, low, close, volume] and a UTC DatetimeIndex
    named 'timestamp'. Volume is always 0 (M34 — no volume in index).

    ``force=True`` ignores the cached set of days and the 'scanned-but-empty'
    sidecar: every day in the requested range is re-scanned from source.

    H3: an unfinished day (today, UTC) is not written to the persistent
    parquet — it may remain in the in-memory return; once the day completes
    the next call re-fetches it.

    Eşzamanlılık (DeepR 2026-08-11 [ORTA]): oku→tara→concat→yaz dizisi
    `load_bybit_bars` ile AYNI deseni izler — okuma yolu kilitsiz (yazma
    atomik olduğu için okuyan ya eski ya yeni dosyanın TAMAMINI görür), yazma
    yolu `_cache_lock` altında ve kilidi aldıktan SONRA diski TAZE okur.
    Eskiden hiç kilit yoktu: iki eşzamanlı /data/refresh/index (ya da
    /backtest/sweep) birbirinin taradığı günleri kaybettiriyordu.
    """
    if granularity not in _GRAN_RULE:
        raise ValueError(f"granularity must be one of {list(_GRAN_RULE)}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = _ticker_to_filename(ticker)
    cache_path = INDEX_CACHE_DIR / f"{safe}_{granularity}.parquet"
    # M20: sidecar of PAST days that were scanned but produced no data — stops
    # every call from re-scanning the same empty days (holidays, before the
    # series start, …).
    scanned_path = INDEX_CACHE_DIR / f"{safe}_{granularity}_scanned.json"

    empty_cols = ["open", "high", "low", "close", "volume"]

    def _read_state() -> tuple[pd.DataFrame, set[date], set[str], set[date]]:
        """Diskteki güncel durum: (barlar, kapsanan günler, ham 'tarandı'
        listesi, boş olduğu bilinen günler). Kilit altında TEKRAR çağrılır."""
        existing = pd.DataFrame(columns=empty_cols)
        cached_days: set[date] = set()
        if cache_path.exists():
            try:
                existing = pd.read_parquet(cache_path)
            except Exception as read_err:
                # M3c: corrupt parquet — ignore the cache, days are re-scanned.
                log.warning(
                    "ignoring corrupt index cache (%s): %s", cache_path, read_err
                )
                existing = pd.DataFrame(columns=empty_cols)
            if not existing.empty and not force:
                cached_days = set(existing.index.normalize().date.tolist())

        prior_scanned: set[str] = set()
        if scanned_path.exists():
            try:
                prior_scanned = set(json.loads(scanned_path.read_text()))
            except Exception:
                prior_scanned = set()
        scanned_empty: set[date] = set()
        if not force:
            for _s in prior_scanned:
                try:
                    scanned_empty.add(date.fromisoformat(_s))
                except (ValueError, TypeError):
                    continue
        return existing, cached_days, prior_scanned, scanned_empty

    wanted_days = []
    d = start
    while d <= end:
        wanted_days.append(d)
        d += timedelta(days=1)

    def _missing(cached_days: set[date], scanned_empty: set[date]) -> list[date]:
        return [
            d for d in wanted_days if d not in cached_days and d not in scanned_empty
        ]

    today_utc = datetime.now(UTC).date()
    existing, cached_days, prior_scanned, scanned_empty = _read_state()
    combined = existing
    if _missing(cached_days, scanned_empty):
        # Yazma yolu: tarama + concat + yazma tek bir kilit altında.
        with _cache_lock(cache_path.with_suffix(".lock")):
            # Kilit altında TAZE oku — beklerken öbür yazar aynı günleri
            # getirmiş olabilir; o zaman aşağıdaki döngü hiç taramaz.
            existing, cached_days, prior_scanned, scanned_empty = _read_state()
            combined = existing
            new_frames: list[pd.DataFrame] = []
            newly_empty: list[date] = []
            for day in _missing(cached_days, scanned_empty):
                rows = _stream_ticker_rows(ticker, day)
                bars = (
                    _aggregate_ohlc(rows, granularity)
                    if not rows.empty
                    else pd.DataFrame(columns=empty_cols)
                )
                if bars.empty:
                    # M20: mark only PAST days as 'empty' — today (H3) may still
                    # be forming and should be retried on the next call.
                    # H371: IF THE SOURCE FILE EXISTS and the ticker is missing
                    # that day, it's 'scanned-empty' (permanent). But if the file
                    # is NOT THERE AT ALL (NFS outage / late-published daily
                    # file) don't poison the day — re-scan it later once the file
                    # arrives. Otherwise a transient outage permanently deletes
                    # past days.
                    if day < today_utc and _index_file_for(day).exists():
                        newly_empty.append(day)
                    continue
                new_frames.append(bars)

            if newly_empty:
                merged = sorted(prior_scanned | {d.isoformat() for d in newly_empty})
                _atomic_write_json(merged, scanned_path)

            if new_frames:
                combined = pd.concat([existing, *new_frames])
                combined = combined[
                    ~combined.index.duplicated(keep="last")
                ].sort_index()
                # H3: today's (UTC) bars aren't complete yet — don't write to
                # disk, persist only past days. combined stays complete in the
                # in-memory return.
                persist = combined[combined.index.normalize().date < today_utc]
                _atomic_to_parquet(persist, cache_path)  # M3a: atomic write

    if combined.empty:
        return combined

    # Return only the requested window.
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    mask = (combined.index >= start_ts) & (combined.index < end_ts)
    result = combined.loc[mask].copy()
    # M34: old caches may still hold count-based fake volume — return
    # guarantees volume=0 without needing a migration.
    result["volume"] = 0.0
    return result


# ------------------------------------------------------------------ bybit


BybitCategory = Literal["spot", "linear", "inverse"]
BybitInterval = Literal["1", "5", "15", "30", "60", "240", "720", "D"]
_BYBIT_URL = "https://api.bybit.com/v5/market/kline"
_BYBIT_LIMIT = 1000  # max rows per request
_BYBIT_MS = {
    "1": 60_000,  # 1 minute
    "5": 300_000,  # 5 minutes
    "15": 900_000,  # 15 minutes
    "30": 1_800_000,  # 30 minutes
    "60": 3_600_000,  # 1 hour
    "240": 14_400_000,  # 4 hours
    "720": 43_200_000,  # 12 hours
    "D": 86_400_000,  # 1 day
}


def _bybit_cache_path(category: str, symbol: str, interval: str) -> Path:
    # Path-traversal guard. `/lab`, `/backtest` and `/robustness` pass raw form
    # input here without whitelisting the symbol (unlike `/data`), so a crafted
    # value like "..\\..\\evil" must not escape BYBIT_CACHE_DIR. `category` and
    # `interval` are closed sets; `symbol` is reduced to a filesystem-safe
    # charset (ASCII alnum + underscore), which also preserves the old
    # "BTC/USDT" -> "BTC_USDT" behaviour. A crafted value raises ValueError
    # BEFORE any directory/lockfile/parquet is created (see load_bybit_bars).
    if category not in BYBIT_CATEGORIES:
        raise ValueError(f"unsupported bybit category: {category!r}")
    if interval not in _BYBIT_MS:
        raise ValueError(f"unsupported bybit interval: {interval!r}")
    safe = "".join(
        c if ((c.isascii() and c.isalnum()) or c == "_") else "_" for c in symbol
    )
    if not safe.strip("_"):
        raise ValueError(f"invalid bybit symbol: {symbol!r}")
    label = "1d" if interval == "D" else f"{interval}m"
    return BYBIT_CACHE_DIR / f"{category}_{safe}_{label}.parquet"


def _fetch_bybit_page(
    category: str, symbol: str, interval: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """One page of the v5/market/kline endpoint → DataFrame (oldest→newest).

    Retries up to 5 times with exponential backoff on rate-limit (10006) or
    transient errors, so large parallel fetches don't hard-fail.
    """
    params = {
        "category": category,
        "symbol": symbol,
        "interval": interval,
        "start": start_ms,
        "end": end_ms,
        "limit": _BYBIT_LIMIT,
    }
    for attempt in range(5):
        try:
            r = _get_bybit_session().get(_BYBIT_URL, params=params, timeout=30)
            r.raise_for_status()
        except requests.exceptions.ReadTimeout:
            wait = 10 * (attempt + 1)
            time.sleep(wait)
            continue
        except requests.exceptions.HTTPError as http_err:
            # Retry on 5xx or 429; propagate on 4xx (except 429).
            status = getattr(http_err.response, "status_code", 0)
            if status == 429 or status >= 500:
                wait = 2**attempt * 2
                time.sleep(wait)
                continue
            raise RuntimeError(f"bybit HTTP error: {http_err}") from http_err
        payload = r.json()
        ret_code = payload.get("retCode")
        if ret_code == 10006:  # rate limit
            wait = 2**attempt * 2  # 2, 4, 8, 16, 32 s
            time.sleep(wait)
            continue
        if ret_code != 0:
            raise RuntimeError(f"bybit error: {payload.get('retMsg')} ({ret_code})")
        rows = payload.get("result", {}).get("list", []) or []
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # Bybit returns newest→oldest: [start, open, high, low, close, volume, turnover]
        df = pd.DataFrame(
            rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"]
        )
        df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["ts", "open", "high", "low", "close"])
        df["ts"] = df["ts"].astype("int64")
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.index.name = "timestamp"
        return df[["open", "high", "low", "close", "volume"]].sort_index()
    raise RuntimeError(
        f"Bybit fetch failed after 5 retries for {symbol}/{interval} — "
        "possible causes: rate limit (10006), network timeout, or 5xx response."
    )


def _bybit_gap_ranges(cached: pd.DataFrame, step_ms: int) -> list[tuple[int, int]]:
    """Cache'in [ilk, son] aralığındaki DELİKLER — (başlangıç, bitiş) ms, kapalı.

    Ağa çıkmaz, dosyaya dokunmaz: adım sabit olduğu için beklenen bar sayısı
    aritmetiktir; satır sayısı eksikse ardışık açılışlar arasındaki > step_ms
    boşluklar deliklerdir. Saf tespit olarak ayrıldı ki okuma yolu "bu istek
    kilide girmeden karşılanır mı?" sorusunu ağa hiç çıkmadan sorabilsin.
    """
    if cached.empty or len(cached.index) < 2:
        return []
    try:
        idx_ns = cached.index.as_unit("ns").asi8
    except AttributeError:  # old pandas: asi8 is already ns
        idx_ns = cached.index.asi8
    ms = idx_ns // 1_000_000
    first_ms, last_ms = int(ms[0]), int(ms[-1])
    expected = (last_ms - first_ms) // step_ms + 1
    if len(ms) >= expected:
        return []

    gaps: list[tuple[int, int]] = []
    prev = int(ms[0])
    for cur in ms[1:]:
        cur = int(cur)
        if cur - prev > step_ms:
            gaps.append((prev + step_ms, cur - step_ms))
        prev = cur
    return gaps


def _bybit_gaps_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.stem}_gaps.json")


def _bybit_known_empty(gaps_path: Path) -> list[list[int]]:
    """'Bilinen boş' aralıklar (sidecar). Bozuk/eksik dosya = bilgi yok."""
    if not gaps_path.exists():
        return []
    try:
        return json.loads(gaps_path.read_text())
    except Exception:
        return []


def _bybit_pending_gaps(
    cached: pd.DataFrame, cache_path: Path, step_ms: int
) -> list[tuple[int, int]]:
    """Doldurulması denenecek delikler: tespit edilenler eksi 'bilinen boş'lar."""
    gaps = _bybit_gap_ranges(cached, step_ms)
    if not gaps:
        return []
    known = _bybit_known_empty(_bybit_gaps_path(cache_path))
    return [
        (gs, ge)
        for gs, ge in gaps
        if not any(ks <= gs and ge <= ke for ks, ke in known)
    ]


def _bybit_gap_frames(
    cached: pd.DataFrame, cache_path: Path, step_ms: int, fetch_segment
) -> list[pd.DataFrame]:
    """M2: find and targeted-fill the holes INSIDE the cached range.

    Since the interval is fixed-step, the expected bar count of the [first, last]
    range is arithmetic; if the actual row count is short, the > step_ms gaps
    between consecutive bar opens mean holes. If the fill returns empty, the
    range is marked 'known empty' in the '{cache_name}_gaps.json' sidecar so
    that every call doesn't re-try the same range (e.g. exchange outage,
    between listings).
    """
    pending = _bybit_pending_gaps(cached, cache_path, step_ms)
    if not pending:
        return []

    gaps_path = _bybit_gaps_path(cache_path)
    known = _bybit_known_empty(gaps_path)

    frames: list[pd.DataFrame] = []
    known_changed = False
    for gs, ge in pending:
        log.warning(
            "bybit cache hole (%s): %s → %s (%d bars missing)",
            cache_path.name,
            pd.Timestamp(gs, unit="ms", tz="UTC"),
            pd.Timestamp(ge, unit="ms", tz="UTC"),
            (ge - gs) // step_ms + 1,
        )
        got = fetch_segment(gs, ge + 1)
        lo = pd.Timestamp(gs, unit="ms", tz="UTC")
        hi = pd.Timestamp(ge, unit="ms", tz="UTC")
        rows_in_gap = sum(int(((f.index >= lo) & (f.index <= hi)).sum()) for f in got)
        if rows_in_gap:
            frames += got
            log.warning(
                "hole filled (%s): %d bars received", cache_path.name, rows_in_gap
            )
        else:
            known.append([gs, ge])
            known_changed = True
            log.warning(
                "hole returned empty — marked 'known empty' (%s): %s → %s",
                gaps_path.name,
                lo,
                hi,
            )
    if known_changed:
        _atomic_write_json(known, gaps_path)
    return frames


def load_bybit_bars(
    symbol: str = "BTCUSDT",
    interval: BybitInterval = "1",
    start: datetime | None = None,
    end: datetime | None = None,
    category: BybitCategory = "linear",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Bybit v5 kline bars → OHLCV DataFrame.

    `interval` is Bybit's raw value ("1" = 1m, "5" = 5m). `category` picks the
    market: "linear" (USDT perp) is the default because BTCUSDT spot pairs
    on Bybit lack deep history. `start`/`end` default to the last 7 days.
    Results are cached per (category, symbol, interval) as parquet and
    incrementally extended in BOTH directions on subsequent calls: a later
    `end` appends to the tail, an earlier `start` backfills older history.
    """
    if interval not in _BYBIT_MS:
        raise ValueError(f"interval must be one of {list(_BYBIT_MS)}")
    if end is None:
        end = datetime.now(UTC)
    if start is None:
        start = end - timedelta(days=7)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start >= end:
        raise ValueError(f"start {start} is not before end {end}")

    BYBIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _bybit_cache_path(category, symbol, interval)

    # H604: the REAL exchange symbol for inverse (coin-margined) contracts is
    # BTCUSD (not the canonical BTCUSDT). The fetch path did no conversion and
    # Bybit v5 silently answered inverse&symbol=BTCUSDT with the LINEAR series →
    # the inverse cache/catalog filled with the wrong (linear) data (validated
    # live). The cache KEY stays canonical (consistency); only the API call
    # takes the market symbol.
    market_symbol = symbol
    if (category or "").lower() == "inverse" and symbol.upper().endswith("USDT"):
        market_symbol = symbol[:-1]  # ...USDT → ...USD

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = _BYBIT_MS[interval]

    def _fetch_segment(from_ms: int, to_ms: int) -> list[pd.DataFrame]:
        """Page the kline endpoint over [from_ms, to_ms). Returns new frames."""
        frames: list[pd.DataFrame] = []
        cur = from_ms
        while cur < to_ms:
            page_end = min(cur + step_ms * _BYBIT_LIMIT - 1, to_ms)
            page = _fetch_bybit_page(category, market_symbol, interval, cur, page_end)
            if page.empty:
                # H2: an empty-window fetch does NOT END the loop — skip the
                # window and continue (a pre-listing gap / post-outage data may
                # follow). No rows streamed from the API in the empty window;
                # a pacing wait is unnecessary.
                cur = page_end + 1
                continue
            frames.append(page)
            last_ms = int(page.index[-1].timestamp() * 1000)
            next_cur = last_ms + step_ms
            # Guard against a stuck page (e.g., API returns same last row).
            if next_cur <= cur:
                break
            cur = next_cur
        return frames

    # OKUMA YOLU KİLİTSİZ (DeepR 2026-08-11 [YÜKSEK]). Bu blok eskiden KOŞULSUZ
    # `_cache_lock` alıyordu: cache tamamen yetse, ağa hiç çıkılmayacak olsa bile.
    # Kilit yazma boyunca (uzun bir backfill'de ~10 dk) tutulduğu için, o sırada
    # AYNI seriyi yalnızca OKUMAK isteyen herkes — /data sayfası, backtest,
    # loop_runner, parallel_exec işçileri — önce bir thread'i bekletiyor, sonra
    # TimeoutError yiyordu. Yazma atomik (`_atomic_to_parquet` → os.replace)
    # olduğu için okuyan ya eski ya yeni dosyanın TAMAMINI görür; yarım dosya
    # görmesi mümkün değil, yani okumanın kilide hiç ihtiyacı yok.
    cached = _read_bybit_cache(cache_path)
    new_frames: list[pd.DataFrame] = []
    if not force_refresh and _bybit_cache_covers(
        cached, cache_path, start_ms, end_ms, step_ms
    ):
        combined = cached
    else:
        # Yazma yolu: okuma-değiştirme-yazma hâlâ per-key kilit altında (M3b),
        # yoksa eşzamanlı iki indirme birbirinin barlarını ezer.
        with _cache_lock(cache_path.with_suffix(".lock")):
            # Kilit altında TAZE oku: beklerken öbür yazar zaten getirmiş
            # olabilir; o zaman aşağıdaki dallar hiçbir şey getirmez ve yazma
            # yapılmaz (ağ trafiği de boşa gitmez).
            #
            # H626: on force_refresh the existing cache is ALSO READ — formerly
            # it wasn't, and only [start,end) was written while ALL bars OUTSIDE
            # the window were deleted (the /data refresh button with days=7
            # shrank years of 1m history to 7 days). Now the fresh window is
            # MERGED with the cache (dedup keep='last' → the requested window is
            # refreshed, the rest preserved).
            cached = _read_bybit_cache(cache_path)
            combined = _extend_bybit_cache(
                cached,
                cache_path,
                start_ms,
                end_ms,
                step_ms,
                force_refresh,
                _fetch_segment,
                new_frames,
            )

    if combined.empty:
        return combined
    start_ts = pd.Timestamp(start).tz_convert("UTC")
    end_ts = pd.Timestamp(end).tz_convert("UTC")
    mask = (combined.index >= start_ts) & (combined.index < end_ts)
    result = combined.loc[mask]

    # Auto-write to Nautilus ParquetDataCatalog so BacktestNode can use this
    # data without a separate manual step. Runs after every successful fetch.
    if new_frames:
        try:
            _auto_write_bybit_catalog(symbol, interval, combined, category)
        except Exception as e:
            # Non-fatal: catalog write failure doesn't break the bar return.
            log.warning(
                "[catalog] auto-write failed for %s/%s: %s",
                symbol,
                interval,
                e,
                exc_info=True,
            )

    return result


def _read_bybit_cache(cache_path: Path) -> pd.DataFrame:
    """Cache parquet'ini KİLİTSİZ oku (yoksa/bozuksa boş çerçeve).

    Yazma `os.replace` ile atomik olduğundan okuyan hep bütün bir dosya görür.
    Yine de Windows'ta, yazar tam o anda dosyayı değiştiriyorsa açma denemesi
    geçici bir paylaşım ihlaliyle düşebilir: bunu "cache bozuk" sayıp yıllarca
    biriktirilmiş geçmişi yeniden indirmeye kalkmak yerine birkaç kez kısaca
    yeniden dene; gerçekten bozuk dosya zaten hepsinde düşer.
    """
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if not cache_path.exists():
        return empty
    last_err: Exception | None = None
    for wait in (*_REPLACE_RETRY_WAITS, None):
        try:
            return pd.read_parquet(cache_path)
        except OSError as e:  # paylaşım ihlali / yarıda kalan okuma
            last_err = e
            if wait is None:
                break
            time.sleep(wait)
        except Exception as e:  # M3c: gerçekten bozuk parquet
            last_err = e
            break
    log.warning("ignoring corrupt bybit cache (%s): %s", cache_path, last_err)
    return empty


def _bybit_cache_covers(
    cached: pd.DataFrame,
    cache_path: Path,
    start_ms: int,
    end_ms: int,
    step_ms: int,
) -> bool:
    """Bu istek yalnız cache'ten karşılanabilir mi? (ağ da kilit de gereksiz)

    `_extend_bybit_cache`'in "getirilecek bir şey var mı" dallarının aynadaki
    hâli: baş (start), kuyruk (end) ve içerideki delikler. Üçü de kapalıysa
    çağıran diske bakıp dönebilir — bir indirme sürerken bile.
    """
    if cached.empty:
        return False
    cached_start_ms = int(cached.index[0].timestamp() * 1000)
    cached_end_ms = int(cached.index[-1].timestamp() * 1000)
    if start_ms < cached_start_ms or cached_end_ms < end_ms:
        return False
    return not _bybit_pending_gaps(cached, cache_path, step_ms)


def _extend_bybit_cache(
    cached: pd.DataFrame,
    cache_path: Path,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    force_refresh: bool,
    _fetch_segment,
    new_frames: list[pd.DataFrame],
) -> pd.DataFrame:
    """Eksikleri getir, birleştir, atomik yaz → birleşik çerçeve.

    Çağıran kilidi tutar. `new_frames` çağıranın listesidir (yerinde doldurulur):
    katalog yazımı "bu çağrıda gerçekten yeni bar geldi mi" bilgisine bakıyor.
    """
    if force_refresh or cached.empty:
        # force_refresh: UNCONDITIONALLY re-fetch the requested window (refresh
        # stale/half bars); the rest of the cache is preserved via concat+dedup.
        new_frames += _fetch_segment(start_ms, end_ms)
    else:
        cached_start_ms = int(cached.index[0].timestamp() * 1000)
        cached_end_ms = int(cached.index[-1].timestamp() * 1000)
        # Backfill older history when the request predates the cache, so a
        # wider `start` actually widens the returned range.
        if start_ms < cached_start_ms:
            new_frames += _fetch_segment(start_ms, cached_start_ms)
        # M2: detect and targeted-fill holes inside the cache.
        new_frames += _bybit_gap_frames(cached, cache_path, step_ms, _fetch_segment)
        # H1a: the tail starts from the LAST cached bar (not cached_end_ms +
        # step_ms) — when the last bar was written it may have been a half
        # bar still forming; it's re-fetched and dedup keep='last' replaces
        # it with the fresh one.
        tail_from = cached_end_ms
        if tail_from < end_ms:
            new_frames += _fetch_segment(tail_from, end_ms)

    if not new_frames:
        return cached

    combined = pd.concat([cached, *new_frames])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    # H1b: crop the FORMING bar before writing to disk and the Nautilus
    # catalog — if its open + step hasn't passed yet, the bar isn't done.
    if not combined.empty:
        now_ms = int(time.time() * 1000)
        last_open_ms = int(combined.index[-1].timestamp() * 1000)
        if last_open_ms + step_ms > now_ms:
            combined = combined.iloc[:-1]
    _atomic_to_parquet(combined, cache_path)  # M3a: atomic write
    return combined


# ---------------------------------------------------------------------------
# Instrument catalog aggregator
# ---------------------------------------------------------------------------
#
# Public surface consumed by ``web/routes/data.py``. Reads Nautilus instrument
# metadata (venue, precision, min_quantity, …) directly from the
# ``backtest.py`` factory functions so we never duplicate precision fields.
#
# See wiki: [[parquet_data_catalog]] (stub), [[bar_aggregation_and_type_syntax]],
# [[index_backtest_via_equity_proxy]], [[precision_modes]].


BYBIT_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
BYBIT_CATEGORIES: tuple[str, ...] = ("spot", "linear", "inverse")
BYBIT_ALL_INTERVALS: tuple[tuple[str, str], ...] = (
    # (interval-code, display-label)
    ("1", "1m"),
    ("5", "5m"),
    ("15", "15m"),
    ("30", "30m"),
    ("60", "1h"),
    ("240", "4h"),
    ("720", "12h"),
    ("D", "1d"),
)

# Studio/Lab timeframes are Bybit interval codes; the external catalog speaks
# the granularity DSL. 30m and 12h have no external counterpart (the ingest TF
# set is 1m/5m/15m/1h/4h/1d) — callers reject those for dotted instrument ids.
EXTERNAL_GRAN_BY_BYBIT_CODE: dict[str, str] = {
    "1": "1-MINUTE",
    "5": "5-MINUTE",
    "15": "15-MINUTE",
    "60": "1-HOUR",
    "240": "4-HOUR",
    "D": "1-DAY",
}

_BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
_BYBIT_INSTRUMENTS_TTL_S = 12 * 3600.0


def _bybit_instruments_cache_path(category: str) -> Path:
    return BYBIT_CACHE_DIR / f"instruments_{category}.json"


def list_bybit_instruments(
    category: str = "linear", *, fetch: bool = True
) -> tuple[str, ...]:
    """Every symbol Bybit currently trades in ``category`` — the tradable universe.

    Distinct from `list_catalog_bybit_symbols`, which reports only what we have
    already downloaded. `load_bybit_bars` fetches any live symbol on first use,
    so a picker limited to the local catalog offers a handful of symbols when
    hundreds are loadable.

    Cached on disk for `_BYBIT_INSTRUMENTS_TTL_S`; the list changes on listing
    days, not minutes. Network failures return the stale cache (or an empty
    tuple offline) instead of raising: callers use this for suggestions, so a
    short list is a degraded picker, not a broken page.

    ``fetch=False`` never touches the network — it reports the disk cache even
    when stale. Callers on a request path use it so a cold or expired cache
    costs them nothing, and warm the cache from a background thread.
    """
    if category not in BYBIT_CATEGORIES:
        raise ValueError(f"unsupported bybit category: {category!r}")
    path = _bybit_instruments_cache_path(category)
    cached: list[str] = []
    if path.exists():
        try:
            blob = json.loads(path.read_text())
            cached = blob.get("symbols") or []
            if (
                time.time() - float(blob.get("fetched_at", 0))
                < _BYBIT_INSTRUMENTS_TTL_S
            ):
                return tuple(cached)
        except Exception:
            pass
    if not fetch:
        return tuple(cached)

    symbols: set[str] = set()
    cursor = ""
    try:
        # Paginated: linear alone is ~600 instruments against a 1000 page cap,
        # so one page is enough today — the cursor loop keeps it correct when
        # it isn't.
        for _ in range(10):
            params = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = _get_bybit_session().get(
                _BYBIT_INSTRUMENTS_URL, params=params, timeout=15
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(payload.get("retMsg", "bybit error"))
            result = payload.get("result") or {}
            for item in result.get("list") or []:
                sym = (item.get("symbol") or "").strip().upper()
                # Only currently-listed contracts, and only names the studio's
                # symbol validator accepts (PreLaunch/Delivering names and the
                # dated futures' "BTC-27JUN25" form would be dead options).
                if item.get("status") != "Trading" or not sym.isalnum():
                    continue
                symbols.add(sym)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
    except Exception as e:
        log.warning("bybit instruments-info failed (%s); serving cached list", e)
        return tuple(cached)

    out = sorted(symbols)
    try:
        BYBIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "symbols": out}))
    except Exception:
        pass
    return tuple(out)


def _read_parquet_stats(path: Path) -> dict | None:
    """Return {rows, first, last, size_bytes} for an existing OHLCV parquet.

    Uses pyarrow footer metadata (pq.read_metadata) instead of a full
    pd.read_parquet decode — reads ~4 KB of footer, not the entire file.
    The pandas DatetimeIndex serializes as a trailing timestamp column
    (usually '__index_level_0__'); we locate it by its logical type rather
    than a magic column name so a renamed index doesn't silently disable the
    fast path. Falls back to a full read when stats are absent or the schema
    doesn't match (a debug log makes that fallback observable).
    """
    if not path.exists():
        return None
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        md = pq.read_metadata(str(path))
        size_bytes = path.stat().st_size
        if md.num_rows == 0:
            return {"rows": 0, "first": None, "last": None, "size_bytes": size_bytes}

        # Locate the timestamp index column by logical type (timestamp[*]),
        # scanning from the last column (where the pandas index lives).
        schema = md.schema.to_arrow_schema()
        ts_col_idx = None
        for i in range(len(schema) - 1, -1, -1):
            if pa.types.is_timestamp(schema.field(i).type):
                ts_col_idx = i
                break

        if ts_col_idx is not None:
            rg0 = md.row_group(0)
            stats0 = rg0.column(ts_col_idx).statistics
            stats_last = (
                md.row_group(md.num_row_groups - 1).column(ts_col_idx).statistics
            )
            if stats0 and stats0.has_min_max and stats_last and stats_last.has_min_max:
                return {
                    "rows": md.num_rows,
                    "first": pd.Timestamp(stats0.min).isoformat(),
                    "last": pd.Timestamp(stats_last.max).isoformat(),
                    "size_bytes": size_bytes,
                }
        log.debug(
            "parquet footer fast-path missed (%s): no timestamp stats, full decode",
            path,
        )
    except Exception as e:
        log.debug("parquet footer read failed (%s): %s; full decode", path, e)

    # Fallback: full decode (missing statistics or unexpected schema)
    try:
        df = pd.read_parquet(path)
    except Exception:  # pragma: no cover
        return None
    if df.empty:
        return {
            "rows": 0,
            "first": None,
            "last": None,
            "size_bytes": path.stat().st_size,
        }
    return {
        "rows": int(len(df)),
        "first": df.index[0].isoformat(),
        "last": df.index[-1].isoformat(),
        "size_bytes": path.stat().st_size,
    }


def _base_ccy(symbol: str, default: str = "BTC") -> str:
    """Extract the base currency from the ticker suffix (BTCUSDT→BTC, ETHUSDC→ETH).

    The crude ``symbol[:-4]``/``symbol[:3]`` mislabeled USDC (5 letters) and 4+
    letter bases. Also used by _instrument_meta's bybit branch below (DeepR
    2026-08-09 [DÜŞÜK]: that branch used to hand-roll the same loop).
    """
    s = symbol.upper()
    for suffix in ("USDT", "USDC", "USD"):
        if s.endswith(suffix):
            return symbol[: -len(suffix)] or default
    return symbol[:3] or default


def _instrument_meta(kind: str, **kw) -> dict:
    """Pull Nautilus instrument fields via the ``backtest.py`` factory functions.

    Lazy import to avoid the FastAPI import chain paying the nautilus_trader
    module load-cost when the catalog isn't being queried.
    """
    from nautilus_trader.model.enums import asset_class_to_str

    from backtest import (
        _make_bybit_instrument,
        _make_index_instrument,
    )

    def _common(inst) -> dict:
        return {
            "instrument_id": str(inst.id),
            "raw_symbol": str(inst.raw_symbol),
            "venue": str(inst.id.venue).split(" ")[0],
            "asset_class": asset_class_to_str(inst.asset_class),
            "price_precision": int(inst.price_precision),
            "size_precision": int(inst.size_precision),
            "price_increment": str(inst.price_increment),
        }

    if kind == "bybit":
        symbol = kw["symbol"]
        category = kw.get("category", "linear")
        # Infer base currency from the ticker suffix (e.g. ETHUSDT → base=ETH).
        # DeepR 2026-08-09 [DÜŞÜK]: this used to hand-roll the same
        # suffix-stripping loop _base_ccy already does one function above —
        # _base_ccy's own docstring already called out the duplication.
        # Only observable difference for a symbol matching none of
        # USDT/USDC/USD: the old inline version silently fell back to the
        # hardcoded "BTC", _base_ccy falls back to the ticker's first 3
        # characters — moot in practice, every real Bybit spot/linear/inverse
        # symbol in this app's universe is USDT- or USD-quoted.
        base_guess = _base_ccy(symbol)
        inst = _make_bybit_instrument(symbol=symbol, base=base_guess, category=category)
        meta = _common(inst)
        meta.update(
            {
                "base_currency": str(inst.base_currency.code),
                "quote_currency": str(inst.quote_currency.code),
                "size_increment": str(inst.size_increment),
                "min_quantity": str(inst.min_quantity),
            }
        )
        return meta

    if kind == "index":
        inst = _make_index_instrument(kw["ticker"])
        meta = _common(inst)
        meta.update(
            {
                "base_currency": None,
                "quote_currency": str(inst.quote_currency.code),
                # Equity has lot_size, not size_increment.
                "size_increment": str(getattr(inst, "size_increment", "1")),
                "lot_size": str(getattr(inst, "lot_size", "1")),
                "min_quantity": None,
            }
        )
        return meta

    raise ValueError(kind)


def _bar_type_dsl(
    instrument_id: str,
    step: int,
    aggregation: str,
    price_source: str = "LAST",
    origin: str = "EXTERNAL",
    category: str | None = None,
) -> dict:
    return {
        "dsl": f"{instrument_id}-{step}-{aggregation}-{price_source}-{origin}",
        "step": step,
        "aggregation": aggregation,
        "price_source": price_source,
        "origin": origin,
        "category": category,
    }


def _size_precision_warning(size_precision: int, asset_class: str) -> list[dict]:
    """Return the wiki-flagged size_precision=0 warning if applicable."""
    if size_precision == 0:
        return [
            {
                "slug": "index_backtest_via_equity_proxy",
                "text": "size_precision=0 — fractional trade_size is silently truncated to 0; the RiskEngine drops the order.",
            }
        ]
    return []


# ---- Per-source row builders ----------------------------------------------

# DeepR 2026-08-08 [DÜŞÜK]: every /data GET rebuilt all 3 symbols × 3
# categories × 8 intervals = 72 cells from scratch — a parquet-footer stat
# (_read_parquet_stats) and a catalog dir-glob + per-file metadata read
# (nautilus_catalog_bar_state) per cell. Already off the event loop
# (web/routes/data.py runs this via asyncio.to_thread), so this was never a
# blocking-request bug, just repeated disk I/O on every page visit/poll. A
# short TTL — same shape as strategy_studio's `_SYMBOLS_TTL_S`/`BARS_TTL_S` —
# is enough: a fresh download becomes visible within one TTL window without
# a page reload, and `refresh_row()`'s force-refresh path is untouched.
_BYBIT_ROWS_TTL_S = 30.0
_bybit_rows_cache: tuple[float, list[dict]] | None = None
_bybit_rows_lock = threading.Lock()


def _bybit_rows() -> list[dict]:
    global _bybit_rows_cache
    with _bybit_rows_lock:
        cached = _bybit_rows_cache
        if cached is not None and time.monotonic() - cached[0] < _BYBIT_ROWS_TTL_S:
            return cached[1]
    rows = _bybit_rows_uncached()
    with _bybit_rows_lock:
        _bybit_rows_cache = (time.monotonic(), rows)
    return rows


def _invalidate_bybit_rows_cache() -> None:
    global _bybit_rows_cache
    with _bybit_rows_lock:
        _bybit_rows_cache = None


def _bybit_rows_uncached() -> list[dict]:
    rows: list[dict] = []
    for symbol in BYBIT_SYMBOLS:
        # Top-level meta uses the linear (default) identity for precision display;
        # precision is category-independent for these pairs.
        meta = _instrument_meta("bybit", symbol=symbol)
        # Every category × interval cell becomes a bar_type entry. Each category
        # has a distinct InstrumentId/venue, so resolve its identity separately —
        # otherwise all three cells would share one DSL and one catalog lookup.
        bar_types: list[dict] = []
        for category in BYBIT_CATEGORIES:
            cat_iid = _instrument_meta("bybit", symbol=symbol, category=category)[
                "instrument_id"
            ]
            for interval_code, label in BYBIT_ALL_INTERVALS:
                cache_path = _bybit_cache_path(category, symbol, interval_code)
                stats = _read_parquet_stats(cache_path)
                # Bar aggregation label — TIME family; "D" maps to DAY, else MINUTE.
                if interval_code == "D":
                    step, aggregation = 1, "DAY"
                else:
                    minutes = int(interval_code)
                    if minutes % 60 == 0 and minutes >= 60:
                        step, aggregation = minutes // 60, "HOUR"
                    else:
                        step, aggregation = minutes, "MINUTE"
                supported = interval_code in _BYBIT_MS
                bt = _bar_type_dsl(
                    cat_iid, step=step, aggregation=aggregation, category=category
                )
                cat_state = nautilus_catalog_bar_state(bt["dsl"]) if supported else None
                bt.update(
                    {
                        "interval_code": interval_code,
                        "interval_label": label,
                        "supported": supported,
                        "state": (
                            "ingested"
                            if stats
                            else "available"
                            if supported
                            else "not-yet-ingested"
                        ),
                        "rows": stats["rows"] if stats else 0,
                        "first": stats["first"] if stats else None,
                        "last": stats["last"] if stats else None,
                        "cache_path": str(cache_path),
                        "in_nautilus_catalog": cat_state is not None,
                        "catalog_rows": cat_state["rows"] if cat_state else 0,
                    }
                )
                bar_types.append(bt)
        rows.append(
            {
                "source": "bybit",
                "key": symbol,
                **meta,
                "bar_types": bar_types,
                "warnings": _size_precision_warning(
                    meta["size_precision"], meta["asset_class"]
                ),
            }
        )
    return rows


# DeepR 2026-08-09 [ORTA] → 2026-08-11 [ORTA]: same shape as _bybit_rows' TTL
# cache above, but the 08-09 turn cached the WRONG THING. It built every
# ticker's rows and applied `limit`/`query` to the finished list, so /data —
# which asks for 50 rows — paid for the whole universe. Ölçek hesaba
# katılmamıştı: `discover_index_tickers` kaynağı ~2 GB'lık Polygon
# grouped-daily dosyası, yani ~10.000 ticker × 5 granularity = 50.000 hücre,
# her biri bir `_read_parquet_stats` (path.exists) + `nautilus_catalog_bar_state`
# (is_dir + glob). Ölçüldü: 10k ticker için 50 satır göstermek 1826 ms sürüyor
# ve 65,8 MB'ı sunucu ömrü boyunca modül global'inde tutuyordu.
#
# Artık limit/query önce TICKER LİSTESİNE uygulanıyor (kardeşi
# `_external_catalog_rows` zaten böyle yapıyor) ve önbellek TEMBEL: yalnız
# gerçekten gösterilen tickerların satırları kurulur ve saklanır.
#
# GEÇERSİZLEŞME SÖZLEŞMESİ:
#   • Pencere 30 sn. Yeni bir indirme en geç bir TTL penceresi içinde,
#     sayfa yenilemeden görünür. Pencere dolunca sözlük tamamen atılır —
#     yani bayat satır taşınmaz, tembellik yalnız PENCERE İÇİNDE geçerlidir.
#   • `refresh_row()` zorla tazelerken `_invalidate_index_rows_cache()`
#     çağırır; yoksa force-refresh kullanıcıya refresh ÖNCESİ satırı verirdi.
#   • Ticker registry'nin kendisi önbelleklenmez (`_read_ticker_registry`
#     her çağrıda okur), yani Discover sonrası yeni tickerlar anında listede.
_INDEX_ROWS_TTL_S = 30.0
# (pencere başlangıcı, {ticker: row}) — tembel doldurulur.
_index_rows_cache: tuple[float, dict[str, dict]] | None = None
_index_rows_lock = threading.Lock()


def _index_rows(limit: int | None = None, query: str | None = None) -> list[dict]:
    """List US-index tickers, optionally filtered.

    ``limit``/``query`` narrow the TICKER LIST first; only the surviving
    tickers pay the per-ticker I/O (see ``_index_rows_uncached``).
    """
    tickers: list[str] = _read_ticker_registry() or []
    if query:
        q = query.lower()
        tickers = [t for t in tickers if q in t.lower()]
    if limit:
        tickers = tickers[:limit]
    return _index_rows_for(tickers)


def _index_rows_for(tickers: list[str]) -> list[dict]:
    """Rows for exactly ``tickers``, reusing anything already built this window."""
    global _index_rows_cache
    if not tickers:
        return []
    now = time.monotonic()
    with _index_rows_lock:
        cached = _index_rows_cache
        if cached is None or now - cached[0] >= _INDEX_ROWS_TTL_S:
            cached = (now, {})
            _index_rows_cache = cached
        stamp, by_ticker = cached
        hits = {t: by_ticker[t] for t in tickers if t in by_ticker}

    missing = [t for t in tickers if t not in hits]
    # Ağır I/O kilit DIŞINDA: bir /data isteği diğerini beklemesin.
    fresh = {r["key"]: r for r in _index_rows_uncached(missing)} if missing else {}
    if fresh:
        with _index_rows_lock:
            current = _index_rows_cache
            # Bu arada TTL döndüyse/geçersizleştiyse yeni pencereye yazma:
            # satırlar o pencereden önce okunmuştu.
            if current is not None and current[0] == stamp:
                current[1].update(fresh)

    merged = {**hits, **fresh}
    return [merged[t] for t in tickers if t in merged]


def _invalidate_index_rows_cache() -> None:
    global _index_rows_cache
    with _index_rows_lock:
        _index_rows_cache = None


def _index_rows_uncached(tickers: list[str] | None = None) -> list[dict]:
    """Build rows for ``tickers`` (default: every ticker in the registry).

    If _tickers.json is missing (or corrupt) and no explicit list is given,
    return an empty list; the UI shows a Discover call-to-action in that case.
    """
    if tickers is None:
        tickers = _read_ticker_registry() or []

    rows: list[dict] = []
    for ticker in tickers:
        meta = _instrument_meta("index", ticker=ticker)
        bar_types = []
        for gran, (step, aggregation) in _GRAN_BARSPEC.items():
            safe = _ticker_to_filename(ticker)
            cache_path = INDEX_CACHE_DIR / f"{safe}_{gran}.parquet"
            stats = _read_parquet_stats(cache_path)
            bt = _bar_type_dsl(
                meta["instrument_id"], step=step, aggregation=aggregation
            )
            # Parity with the Bybit branch: alongside the pandas cache state,
            # also report whether it's in the Nautilus catalog — so a "→ Catalog"
            # write shows up in the UI with a "✓ catalog N bars" badge and the
            # button disappears.
            cat_state = nautilus_catalog_bar_state(bt["dsl"])
            bt.update(
                {
                    "granularity": gran,
                    "state": "ingested" if stats else "available",
                    "rows": stats["rows"] if stats else 0,
                    "first": stats["first"] if stats else None,
                    "last": stats["last"] if stats else None,
                    "cache_path": str(cache_path),
                    "in_nautilus_catalog": cat_state is not None,
                    "catalog_rows": cat_state["rows"] if cat_state else 0,
                }
            )
            bar_types.append(bt)
        rows.append(
            {
                "source": "index",
                "key": ticker,
                **meta,
                "bar_types": bar_types,
                "warnings": _size_precision_warning(
                    meta["size_precision"], meta["asset_class"]
                ),
            }
        )
    return rows


# ---- Public API ------------------------------------------------------------

# ---- Public API ------------------------------------------------------------


def _auto_write_bybit_catalog(
    symbol: str, interval: str, df: pd.DataFrame, category: str = "linear"
) -> None:
    """Write a Bybit DataFrame into the Nautilus ParquetDataCatalog.

    Called automatically by ``load_bybit_bars`` after every successful fetch
    that produces new data.

    ``category`` (spot/linear/inverse) is threaded into the instrument so each
    category writes under a distinct venue/bar_type key.

    L20: the default path is APPEND-TO-TAIL ONLY — the last ts_event in the
    catalog (last_ns, the cheap M19 state) is read and only bars that close
    AFTER it are added via ``write_data`` (Nautilus writes the append as a new
    parquet file). A full delete+rewrite runs only on backfill (df starts
    before the catalog start) or repair (missing row within the catalog range /
    state unreadable). The tail write strictly starts AFTER last_ns — no
    duplicate-bar risk.

    M3b: the delete+write pair and the append path are under a per-bar_type lock.
    """
    from nautilus_trader.model.data import Bar

    from backtest import (
        _bars_from_df,
        _make_bybit_bar_type,
        _make_bybit_instrument,
        price_scale_hint,
    )

    # Infer base from the canonical symbol (BTCUSDT → BTC); quote is derived from
    # the category by _make_bybit_instrument (USDT for spot/linear, USD for inverse).
    base = _base_ccy(symbol)
    # price_hint: an unlisted sub-cent symbol would otherwise be written into
    # the Nautilus catalog with a 2-decimal precision, i.e. as all-zero bars
    # (DeepR 2026-08-11 [YÜKSEK]). The frame we are about to write IS the
    # authority on this symbol's real price scale.
    instrument = _make_bybit_instrument(
        symbol=symbol,
        base=base,
        category=category,
        price_hint=price_scale_hint(df),
    )
    bar_type = _make_bybit_bar_type(instrument.id, interval)
    if df.empty:
        return
    interval_ns = _BYBIT_MS[interval] * 1_000_000

    NAUTILUS_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    # The lock name is derived from plain string inputs (the bar_type object
    # can be mocked in tests; it can't safely go into a file name).
    lock_path = NAUTILUS_CATALOG_DIR / f"{category}_{symbol}_{interval}.lock"
    with _cache_lock(lock_path):
        catalog = get_nautilus_catalog()
        catalog.write_data([instrument])

        state = nautilus_catalog_bar_state(str(bar_type))
        first_ns = state["first_ns"] if state else None
        last_ns = state["last_ns"] if state else None
        # df index is bar OPEN time; catalog ts_event = open + interval (CLOSE).
        # The pd.Timestamp() wrapper defends against the index type (tests can
        # pass a df with a plain RangeIndex).
        df_first_close_ns = int(pd.Timestamp(df.index[0]).value) + interval_ns

        append_only = False
        if state is not None and first_ns is not None and last_ns is not None:
            # Repair detection: if the catalog carries missing rows within its
            # own [first, last] range (e.g. an M2 hole-fill arrived) a full
            # rewrite is needed.
            expected_rows = (last_ns - first_ns) // interval_ns + 1
            catalog_has_gaps = state["rows"] < expected_rows
            append_only = df_first_close_ns >= first_ns and not catalog_has_gaps

        if append_only:
            # ts_event = open + interval > last_ns  ⇔  open > last_ns - interval
            cut = pd.Timestamp(last_ns - interval_ns, unit="ns", tz="UTC")
            tail = df[df.index > cut]
            if tail.empty:
                return  # no new bars to append — don't touch anything
            bars = _bars_from_df(bar_type, instrument, tail)
            catalog.write_data(bars)
            print(f"[catalog] appended {len(bars):,} bars → {bar_type}", flush=True)
            return

        # Backfill / first write / repair: full delete + rewrite.
        bars = _bars_from_df(bar_type, instrument, df)
        try:
            catalog.delete_data_range(data_cls=Bar, identifier=str(bar_type))
        except Exception as _del_err:
            # If delete_data_range is 'no data/not found' that's fine (first write).
            _msg = str(_del_err).lower()
            if "no data" not in _msg and "not found" not in _msg:
                # M1030: if the delete GENUINELY fails (PermissionError, partial
                # delete) and write_data is called UNCONDITIONALLY, the old +
                # new files create overlapping (non-disjoint) ranges and the
                # next read blows up. Cancel the write — the next fetch from
                # cache repairs it.
                log.warning(
                    "[catalog] delete_data_range ERROR, skipping write → %s: %s",
                    bar_type,
                    _del_err,
                    exc_info=True,
                )
                return
        catalog.write_data(bars)
        print(f"[catalog] wrote {len(bars):,} bars → {bar_type}", flush=True)


def get_nautilus_catalog():
    """Return a ParquetDataCatalog opened at NAUTILUS_CATALOG_DIR (lazy import)."""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    NAUTILUS_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(str(NAUTILUS_CATALOG_DIR))


def list_catalog_bybit_symbols() -> list[dict]:
    """Return distinct Bybit symbols in the catalog as [{symbol, category}] sorted.

    Reads the bar/ subdirectory names (e.g. 'BTCUSDT.BYBIT_LINEAR-1-MINUTE-…')
    to extract unique symbol+category pairs without importing nautilus.
    """
    bar_dir = NAUTILUS_CATALOG_DIR / "data" / "bar"
    if not bar_dir.exists():
        return []
    seen: set[tuple[str, str]] = set()
    _cat_map = {
        "BYBIT_LINEAR": "linear",
        "BYBIT_SPOT": "spot",
        "BYBIT_INVERSE": "inverse",
    }
    for entry in bar_dir.iterdir():
        # name format: BTCUSDT.BYBIT_LINEAR-1-MINUTE-LAST-EXTERNAL
        name = entry.name
        dot = name.find(".")
        if dot < 0:
            continue
        symbol = name[:dot]
        rest = name[dot + 1 :]
        venue = rest.split("-")[0]
        if venue not in _cat_map:
            continue
        seen.add((symbol, _cat_map[venue]))
    return sorted(
        [{"symbol": s, "category": c} for s, c in seen], key=lambda x: x["symbol"]
    )


def _catalog_fname_ns(stamp: str) -> int | None:
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


def nautilus_catalog_bar_state(bar_type_str: str) -> dict | None:
    """Check whether a BarType is already in the Nautilus catalog.

    Returns dict {rows, first_ns, last_ns} if present, None if absent.

    M19: the old version decoded all bars (seconds on large series). Now a
    cheap filesystem scan: the row count from the parquet footer metadata
    (pyarrow.parquet.read_metadata), first/last ns from the file name
    ('{startZ}_{endZ}.parquet'). The return signature is preserved.
    """
    bar_dir = NAUTILUS_CATALOG_DIR / "data" / "bar" / bar_type_str
    try:
        if not bar_dir.is_dir():
            return None
        files = sorted(bar_dir.glob("*.parquet"))
    except OSError:  # invalid dir name (e.g. ':' on Windows) etc.
        return None
    if not files:
        return None

    import pyarrow.parquet as _pq

    rows = 0
    for f in files:
        try:
            rows += _pq.read_metadata(str(f)).num_rows
        except Exception:
            pass  # corrupt/half file — don't count it
    if rows == 0:
        return None
    first_stamp = files[0].name.split("_", 1)[0]
    last_stamp = files[-1].name.split("_", 1)[-1].removesuffix(".parquet")
    return {
        "rows": rows,
        "first_ns": _catalog_fname_ns(first_stamp),
        "last_ns": _catalog_fname_ns(last_stamp),
    }


def write_to_nautilus_catalog(source: str, **kw) -> dict:
    """Ingest a cached pandas parquet into the Nautilus ParquetDataCatalog.

    Reads the pandas cache (which must already exist — call load_*_bars first),
    converts to Bar objects via ``_bars_from_df``, writes instrument + bars to
    ``NAUTILUS_CATALOG_DIR``.

    kwargs by source:
      bybit:  symbol, category, interval
      index:  ticker, granularity

    Returns {instrument_id, bar_type, rows_written, catalog_path}.
    """
    from nautilus_trader.model.data import Bar

    from backtest import (
        _bars_from_df,
        _make_bybit_bar_type,
        _make_bybit_instrument,
        _make_index_bar_type,
        _make_index_instrument,
        price_scale_hint,
    )

    catalog = get_nautilus_catalog()

    if source == "bybit":
        symbol = kw["symbol"]
        category = kw.get("category", "linear")
        interval = kw["interval"]
        # M1174: formerly load_bybit_bars(days=7) took the MASKED 7-day window,
        # deleted the ENTIRE catalog bar_type and wrote only 7 days — the 'write
        # to catalog' button shrank catalog history to 7 days. load_bybit_bars
        # already auto-writes the FULL combined; here too read and write the FULL
        # cache (parquet), not the 7-day window.
        _cp = _bybit_cache_path(category, symbol, interval)
        if not _cp.exists():
            raise RuntimeError(
                f"No cached data for {symbol}/{category}/{interval}. "
                "Fetch it first from the Data catalog screen."
            )
        try:
            df = pd.read_parquet(_cp)
        except Exception as _e:
            raise RuntimeError(f"could not read bybit cache ({_cp}): {_e}") from _e
        if df.empty:
            raise RuntimeError(
                f"No cached data for {symbol}/{category}/{interval}. "
                "Fetch it first from the Data catalog screen."
            )
        instrument = _make_bybit_instrument(
            symbol=symbol,
            base=_base_ccy(symbol),
            category=category,
            # Same reason as _auto_write_bybit_catalog: derive the precision
            # from the real price scale so sub-cent symbols aren't written as
            # zeros. Both writers derive it from the same cached frame, so the
            # catalog identity stays consistent between them.
            price_hint=price_scale_hint(df),
        )
        bar_type = _make_bybit_bar_type(instrument.id, interval)
        bars = _bars_from_df(bar_type, instrument, df)
        # #5: the SAME lock key as _auto_write_bybit_catalog — if two catalog
        # writers collide on the same bar_type, a bare write_data gives
        # 'non-disjoint intervals' (HTTP 500) / leaves overlapping parquet. The
        # M1030 delete→write guard only holds if ALL writers take the lock.
        lock_path = NAUTILUS_CATALOG_DIR / f"{category}_{symbol}_{interval}.lock"

    elif source == "index":
        ticker = kw["ticker"]
        granularity = kw.get("granularity", "1d")
        safe = _ticker_to_filename(ticker)
        cache_path = INDEX_CACHE_DIR / f"{safe}_{granularity}.parquet"
        if not cache_path.exists():
            raise RuntimeError(
                f"No cached data for {ticker}/{granularity}. "
                "Fetch it first from the Data catalog screen."
            )
        df = pd.read_parquet(cache_path)
        instrument = _make_index_instrument(ticker)
        bar_type = _make_index_bar_type(instrument.id, granularity)
        bars = _bars_from_df(bar_type, instrument, df)
        # #5: a stable, self-specific lock key for the index source too.
        lock_path = NAUTILUS_CATALOG_DIR / f"index_{safe}_{granularity}.lock"

    else:
        raise ValueError(f"unknown source {source!r}")

    # #5: make instrument + delete_data_range + write_data atomic under ONE
    # lock — same key as _auto_write_bybit_catalog (mutual exclusion).
    with _cache_lock(lock_path):
        catalog.write_data([instrument])
        # Clear any existing data for this bar_type before writing to avoid
        # non-disjoint interval errors on re-ingestion.
        try:
            catalog.delete_data_range(
                data_cls=Bar,
                identifier=str(bar_type),
            )
        except Exception as _del_err:
            _msg = str(_del_err).lower()
            if "no data" not in _msg and "not found" not in _msg:
                # M1030: if the delete genuinely fails, don't write (non-disjoint prevention).
                raise RuntimeError(
                    f"delete_data_range failed, aborting write: {_del_err}"
                ) from _del_err
        catalog.write_data(bars)

    return {
        "instrument_id": str(instrument.id),
        "bar_type": str(bar_type),
        "rows_written": len(bars),
        "catalog_path": str(NAUTILUS_CATALOG_DIR),
    }


def _external_meta_signature(root: Path) -> tuple:
    """Enstrüman tanımları için ucuz `(mtime, size)` imzası.

    Tek tek enstrüman parquet'leri stat'lanmaz (591 enstrümanlı bir kökte bu
    her /data isteğinde binlerce syscall demekti): yeni bir enstrüman ingest
    edilince `data/<sınıf>/` dizinine yeni bir alt dizin girer ve o dizinin
    mtime'ı değişir — geçersizleştirme için gereken tam olarak budur.
    """
    data_dir = root / "data"
    parts: list = [_stat_sig(data_dir)]
    try:
        children = sorted(p.name for p in data_dir.iterdir() if p.is_dir())
    except OSError:
        children = []
    for name in children:
        parts.append((name, _stat_sig(data_dir / name)))
    return tuple(parts)


def _external_instrument_meta(root: Path) -> dict:
    """Return {instrument_id: {asset_class, price_precision, size_precision,
    price_increment, quote_currency}} for one external catalog root.

    Sonuç `(mtime, size)` imzasıyla önbelleklenir: yeni ingest edilen bir
    enstrüman sunucu yeniden başlatılmadan görünür.

    DeepR 2026-08-11 [ORTA]: okuma HATASI artık ÖNBELLEĞE YAZILMAZ. Eskiden
    `except Exception: meta = {}` sonucu doğrudan cache'e giriyordu, yani
    açılışta yaşanan tek bir dosya kilidi/geçici I/O hatası SÜREÇ ÖMRÜ BOYUNCA
    "bu kökte enstrüman metası yok" anlamına geliyor, satırlar `asset_class`/
    `price_precision`/`quote_currency` olmadan ("—") görünüyor ve bu "katalogda
    bu bilgi yok" ile ayırt edilemiyordu. `composer.load_catalog`'un dersi:
    okunamıyorsa cevap "bilinmiyor"dur, "boş"tur değil — burada da boş sözlük
    dönülür ama SAKLANMAZ, bir sonraki çağrı yeniden dener.
    """
    key = str(root)
    sig = _external_meta_signature(root)
    cached = _EXT_INSTRUMENT_META.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    meta: dict = {}
    skipped = 0
    try:
        from nautilus_trader.model.enums import asset_class_to_str
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        cat = ParquetDataCatalog(str(root))
        for inst in cat.instruments():
            try:
                meta[str(inst.id)] = {
                    "asset_class": asset_class_to_str(inst.asset_class),
                    "price_precision": int(inst.price_precision),
                    "size_precision": int(inst.size_precision),
                    "price_increment": str(inst.price_increment),
                    "quote_currency": str(
                        getattr(inst, "quote_currency", "") or ""
                    ).split(" ")[0],
                }
            except Exception:
                skipped += 1
                continue
    except Exception as e:
        if key not in _EXT_META_WARNED:
            _EXT_META_WARNED.add(key)
            log.warning(
                "could not read instrument definitions from external catalog "
                "%s: %s — meta shown as unknown; NOT cached, retried on the "
                "next call",
                root,
                e,
            )
        return {}

    _EXT_META_WARNED.discard(key)
    if skipped:
        log.warning(
            "%d instrument definition(s) in %s could not be decoded — their "
            "rows will show unknown precision/asset class",
            skipped,
            root,
        )
    _EXT_INSTRUMENT_META[key] = (sig, meta)
    return meta


def _external_manifest(root: Path) -> dict:
    """M21: read and cache the OPTIONAL ``_manifest.json`` at the root.

    Returns a symbol → meta dict (NAU_ev output: bars/first/last/ok/…, plus
    'adjusted' if present). If the file is missing or corrupt, an empty dict —
    'adjusted' info stays 'unknown'.

    Önbellek `(mtime, size)` ile geçersizleşir: `write_manifest` bir koşumun
    SONUNDA dosyayı yeniden yazar, sunucunun bunu görmek için restart'a
    ihtiyacı yoktur. GEÇİCİ bir I/O hatası (dosya kilidi) sonucu saklanmaz —
    bozuk JSON ise deterministik bir durumdur ve saklanır (dosya düzelince
    imza zaten değişir); `custom_block_store._read_registry` ile aynı ayrım.
    """
    key = str(root)
    p = root / "_manifest.json"
    sig = _stat_sig(p)
    cached = _EXT_MANIFEST.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    manifest: dict = {}
    try:
        raw = p.read_text()
    except FileNotFoundError:
        raw = None  # manifest opsiyonel — yokluğu bir hata değil
    except OSError as e:
        log.warning(
            "could not read external catalog manifest (%s): %s — treated as "
            "unknown, NOT cached",
            p,
            e,
        )
        return {}
    if raw is not None:
        try:
            loaded = json.loads(raw)
        except Exception as e:
            log.warning("corrupt external catalog manifest (%s): %s", p, e)
            loaded = None
        if isinstance(loaded, dict):
            manifest = loaded
    _EXT_MANIFEST[key] = (sig, manifest)
    return manifest


def external_manifest() -> dict:
    """Tüm harici katalog köklerinin manifestlerini tek sözlükte birleştir.

    Kök-başına okuyucu (`_external_manifest`) çağıranın hangi kökte olduğunu
    bilmesini şart koşuyor; enstrümanlar arası bir soru soran çağıran (ör.
    "bu ticker hangi dikişin bileşeni?") bunu bilemez. Öncelik listedeki ilk
    köktedir — aynı ticker iki kökte varsa /data panelinde de ilki kazanıyor.
    """
    merged: dict = {}
    for root in external_catalog_roots():
        for symbol, entry in (_external_manifest(Path(root)) or {}).items():
            merged.setdefault(symbol, entry)
    return merged


def _external_manifest_entry(root: Path, instrument_id: str) -> dict:
    """Manifest entry for an instrument id, or {}.

    The manifest is keyed by bare symbol (e.g. 'NVDA', 'BRK.A'), while
    instrument_id is like 'NVDA.NASDAQ' / 'BRK.A.NASDAQ' — only the LAST dot
    (venue) is dropped. M1260: formerly the first dot was split ('BRK.A.NASDAQ'
    → 'BRK') and the UNADJUSTED warning for dotted symbols (BRK.A really is
    adjusted=False!) was lost.
    """
    symbol = instrument_id.rsplit(".", 1)[0]
    entry = _external_manifest(root).get(symbol)
    return entry if isinstance(entry, dict) else {}


def _external_adjustment_flags(
    root: Path, instrument_id: str
) -> tuple[bool | None, bool | None]:
    """(split_adjusted, dividend_adjusted) from the manifest; None = unknown.

    M21 read a single 'adjusted' field, which conflated two different facts:
    Polygon/Massive `/v2/aggs adjusted=true` adjusts for SPLITS ONLY and never
    for dividends. Two batches in the same catalog carried that one flag with
    contradictory notes ("split/dividend adjusted" vs "split only") and the
    contradiction never surfaced in the UI, because nothing read the note.

    The new schema keeps the two apart. For a pre-migration manifest,
    `split_adjusted` falls back to the legacy `adjusted` field (they mean the
    same thing), while `dividend_adjusted` is NOT guessed — it stays None
    ('unknown') rather than being invented.
    """
    entry = _external_manifest_entry(root, instrument_id)
    split = entry.get("split_adjusted")
    if not isinstance(split, bool):
        split = entry.get("adjusted")
    div = entry.get("dividend_adjusted")
    return (
        split if isinstance(split, bool) else None,
        div if isinstance(div, bool) else None,
    )


def _external_adjusted_flag(root: Path, instrument_id: str) -> bool | None:
    """Legacy single-flag view = SPLIT adjustment. True/False, None if unknown."""
    return _external_adjustment_flags(root, instrument_id)[0]


def _external_coverage_gaps(root: Path, instrument_id: str) -> list[dict]:
    """Per-ticker coverage gaps recorded in the manifest ([] if none/not computed).

    A gap is a stretch of sessions the rest of the catalog trades through but
    this symbol has no bars for — QQQ's own series is empty from 2004-12 to
    2011-03 (the ticker was QQQQ then), which hides the whole 2008 bear market
    and makes buy-and-hold maxDD look like 35.6% instead of 53.5%. The symbol
    is deliberately NOT hidden from the pickers; the gap is surfaced next to it.
    """
    gaps = _external_manifest_entry(root, instrument_id).get("coverage_gaps")
    return gaps if isinstance(gaps, list) else []


# Order the timeframe buckets shortest→longest for stable display.
_EXT_TF_ORDER = {
    "1-MINUTE": 0,
    "5-MINUTE": 1,
    "15-MINUTE": 2,
    "30-MINUTE": 3,
    "1-HOUR": 4,
    "4-HOUR": 5,
    "1-DAY": 6,
    "1-WEEK": 7,
}

# DeepR 2026-08-11 [ORTA]: bu panel her /data GET'inde sıfırdan çalışıyordu —
# kardeşleri `_bybit_rows` (2026-08-08) ve `_index_rows` (2026-08-09) tam da bu
# maliyet için 30 sn'lik TTL'e kavuşturulmuştu, external dışarıda kalmıştı.
# Ölçüldü (bu kutu, 16 enstrüman / 293 parquet): çağrı başına 72–95 ms, tamamı
# tekrar eden disk I/O'su. Kod içindeki not gerçek masaüstü kataloğunun 592
# enstrüman olduğunu söylüyor → 592 × 7 timeframe ≈ 4000 dizin girişi taraması
# + yüzlerce parquet footer okuması, sayfa başına saniyeler mertebesinde.
#
# İki katman, ikisi de aynı 30 sn'lik pencerede:
#   1) discovery — katalog köklerinin `data/bar` dizin taraması + "used" kümesi,
#   2) satırlar  — TEMBEL: yalnız gösterilen enstrümanın parquet footer'ları.
#
# GEÇERSİZLEŞME SÖZLEŞMESİ:
#   • Pencere 30 sn; yeni ingest edilen bir ticker en geç bir pencere içinde
#     görünür (kardeş panellerle aynı gecikme). Pencere dolunca hem discovery
#     hem satırlar tamamen atılır — bayat satır bir sonraki pencereye taşınmaz.
#   • Kök listesi değişirse (env override ya da `EQUITY_CATALOG_DIR`'in geç
#     bağlanması) anahtar da değişir → TTL beklenmeden yeniden taranır. Bu
#     olmasaydı ingest'in İLK KEZ oluşturduğu kök 30 sn boyunca görünmezdi.
#   • Satır içindeki `split_adjusted`/`coverage_gaps` alanları
#     `_external_manifest`'ten gelir; onun kendi `(mtime, size)` imzası var,
#     yani pencere yenilendiğinde taze manifest okunur.
#   • `_invalidate_external_rows_cache()` zorla tazeleme için.
_EXTERNAL_ROWS_TTL_S = 30.0
# (pencere başlangıcı, kök imzası, discovery, {instrument_id: row})
_external_rows_cache: tuple[float, tuple, dict, dict[str, dict]] | None = None
_external_rows_lock = threading.Lock()


def _invalidate_external_rows_cache() -> None:
    global _external_rows_cache
    with _external_rows_lock:
        _external_rows_cache = None


def _external_catalog_discovery() -> dict:
    """Which instruments exist in the external catalogs, and which were used.

    Directory listing only — no parquet file is opened here.
    """
    # instrument_id -> {"root": Path, "bar_dirs": [(dir_name, interval, Path)]}
    catalog_map: dict[str, dict] = {}
    for root in external_catalog_roots():
        bar_dir = root / "data" / "bar"
        if not bar_dir.exists():
            continue
        for d in bar_dir.iterdir():
            if not d.is_dir():
                continue
            # BarType DSL: {instrument}-{step}-{aggregation}-{price}-{source}
            parts = d.name.rsplit("-", 4)
            if len(parts) != 5:
                continue
            inst_id = parts[0]
            interval = f"{parts[1]}-{parts[2]}"
            entry = catalog_map.setdefault(inst_id, {"root": root, "bar_dirs": []})
            entry["bar_dirs"].append((d.name, interval, d))

    # Used-first ordering: an alphabetical first-page of a 592-instrument
    # catalog leads with tickers nobody asked for (A, AA, AAL, …). "Used" =
    # a pandas cache exists under EXTERNAL_CACHE_DIR, i.e. the instrument has
    # been loaded for a backtest before. One directory listing, no parsing:
    # cache names are f"{_ticker_to_filename(id)}_{granularity}.parquet", so
    # the forward mapping is checked against a prefix set.
    used_fns: set[str] = set()
    if EXTERNAL_CACHE_DIR.exists():
        for f in EXTERNAL_CACHE_DIR.glob("*.parquet"):
            used_fns.add(f.name.rsplit("_", 1)[0])
    used = {i for i in catalog_map if _ticker_to_filename(i) in used_fns}
    return {"catalog_map": catalog_map, "used": used}


def _external_catalog_window() -> tuple[float, dict, dict[str, dict]]:
    """(stamp, discovery, already-built rows) for the current 30 s window."""
    global _external_rows_cache
    now = time.monotonic()
    roots_key = tuple(str(r) for r in external_catalog_roots())
    with _external_rows_lock:
        cached = _external_rows_cache
        if (
            cached is not None
            and cached[1] == roots_key
            and now - cached[0] < _EXTERNAL_ROWS_TTL_S
        ):
            return cached[0], cached[2], dict(cached[3])
    discovery = _external_catalog_discovery()  # dizin taraması kilit DIŞINDA
    with _external_rows_lock:
        _external_rows_cache = (now, roots_key, discovery, {})
    return now, discovery, {}


def _external_catalog_row(inst_id: str, entry: dict, used: set[str]) -> dict:
    """One display row — this is where the parquet footer reads happen."""
    import pyarrow.parquet as _pq

    meta = _external_instrument_meta(entry["root"]).get(inst_id, {})
    bar_types = []
    for dir_name, interval, d in entry["bar_dirs"]:
        files = sorted(p for p in d.glob("*.parquet"))
        if not files:
            continue
        # Date range from filenames: <startNs...Z>_<endNs...Z>.parquet
        first = files[0].name.split("_")[0]
        last = files[-1].name.split("_")[1].replace(".parquet", "")
        n_rows = 0
        for f in files:
            try:
                n_rows += _pq.read_metadata(str(f)).num_rows
            except Exception:
                pass
        bar_types.append(
            {
                "dsl": dir_name,
                "state": "ingested",
                "rows": n_rows,
                "first": first or None,
                "last": last or None,
                "in_nautilus_catalog": False,
                "granularity": interval,
            }
        )
    bar_types.sort(key=lambda b: _EXT_TF_ORDER.get(b["granularity"], 99))
    adj_split, adj_div = _external_adjustment_flags(entry["root"], inst_id)
    return {
        "source": "external",
        "key": inst_id,
        "instrument_id": inst_id,
        # Drives the used-first ordering's badge: loaded before.
        "used": inst_id in used,
        # Adjustment status from the manifest, split and dividend kept
        # APART (True/False, None='unknown'). "adjusted" stays as the
        # legacy alias of the split flag so existing templates keep
        # working; dividend_adjusted is the newly honest half.
        "adjusted": adj_split,
        "split_adjusted": adj_split,
        "dividend_adjusted": adj_div,
        "coverage_gaps": _external_coverage_gaps(entry["root"], inst_id),
        "asset_class": meta.get("asset_class", "—"),
        "base_currency": None,
        "quote_currency": meta.get("quote_currency", ""),
        "price_precision": meta.get("price_precision", "—"),
        "size_precision": meta.get("size_precision", "—"),
        "price_increment": meta.get("price_increment", "—"),
        "min_quantity": None,
        "warnings": [],
        "bar_types": bar_types,
    }


def _external_catalog_rows(query: str | None = None, limit: int | None = 50) -> dict:
    """Scan the read-only external catalogs and return display rows.

    Returns {"rows": [...], "total": int}. The heavy per-bar-type work
    (parquet row counts) is done only for the ``limit`` rows actually shown;
    instrument discovery and date ranges are pulled from the filesystem
    (directory + parquet filenames) without decoding any bars. Both halves are
    reused for 30 s (see ``_EXTERNAL_ROWS_TTL_S`` for the staleness contract).
    """
    stamp, discovery, cached_rows = _external_catalog_window()
    catalog_map: dict[str, dict] = discovery["catalog_map"]
    used: set[str] = discovery["used"]

    total = len(catalog_map)

    inst_ids = sorted(used) + sorted(set(catalog_map) - used)
    if query:
        q = query.strip().lower()
        inst_ids = [i for i in inst_ids if q in i.lower()]
    matched = len(inst_ids)
    if limit is not None:
        inst_ids = inst_ids[:limit]

    fresh: dict[str, dict] = {
        i: _external_catalog_row(i, catalog_map[i], used)
        for i in inst_ids
        if i not in cached_rows
    }
    if fresh:
        with _external_rows_lock:
            current = _external_rows_cache
            # Bu arada pencere döndüyse/geçersizleştiyse yenisine yazma.
            if current is not None and current[0] == stamp:
                current[3].update(fresh)

    rows = [cached_rows[i] if i in cached_rows else fresh[i] for i in inst_ids]
    return {"rows": rows, "total": total, "matched": matched}


# ── External catalog bar loading (read-only backtest data source) ────────────

_EXT_GRAN_SECONDS = {"MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800}


def _external_interval_ns(granularity: str) -> int:
    """'1-DAY' → 86400e9. Raises ValueError on anything that isn't a
    time-aggregated catalog step (defensive against form tampering).

    Note (L34): for DAY/WEEK the derived duration is the CALENDAR NOMINAL
    interval (24 hours / 7 days), not the session length. The close→open shift
    uses this nominal step; shortened sessions / half days are not modeled
    separately.
    """
    step_s, _, unit = granularity.partition("-")
    if not step_s.isdigit() or unit not in _EXT_GRAN_SECONDS:
        raise ValueError(f"unsupported external granularity {granularity!r}")
    return int(step_s) * _EXT_GRAN_SECONDS[unit] * 1_000_000_000


def _external_bar_dir(instrument_id: str, granularity: str) -> tuple[Path, Path] | None:
    """Locate (catalog_root, bar_dir) for instrument+granularity, or None."""
    for root in external_catalog_roots():
        bar_root = root / "data" / "bar"
        if not bar_root.exists():
            continue
        for d in bar_root.iterdir():
            if not d.is_dir():
                continue
            parts = d.name.rsplit("-", 4)
            if (
                len(parts) == 5
                and parts[0] == instrument_id
                and f"{parts[1]}-{parts[2]}" == granularity
            ):
                return root, d
    return None


def list_external_instruments() -> list[dict]:
    """Cheap directory scan of all external catalogs for UI pickers.

    Returns [{"instrument_id": "QQQ.NASDAQ", "granularities": ["1-MINUTE", ...]}]
    sorted by instrument id; granularities sorted shortest→longest. No parquet
    footers are read — this stays fast for 500+ instruments.

    Each record also carries the manifest's honesty fields: `adjusted` /
    `split_adjusted` (legacy alias + the split flag), `dividend_adjusted`
    (None = unknown; never guessed) and `coverage_gaps` (sessions the symbol is
    missing while the rest of the catalog trades — see
    `_external_coverage_gaps`). Only the manifest is read here, so this stays a
    cheap directory scan.
    """
    _order = {
        "1-MINUTE": 0,
        "5-MINUTE": 1,
        "15-MINUTE": 2,
        "30-MINUTE": 3,
        "1-HOUR": 4,
        "4-HOUR": 5,
        "1-DAY": 6,
        "1-WEEK": 7,
    }
    grans: dict[str, set[str]] = {}
    roots: dict[str, Path] = {}
    for root in external_catalog_roots():
        bar_root = root / "data" / "bar"
        if not bar_root.exists():
            continue
        for d in bar_root.iterdir():
            if not d.is_dir():
                continue
            parts = d.name.rsplit("-", 4)
            if len(parts) != 5:
                continue
            grans.setdefault(parts[0], set()).add(f"{parts[1]}-{parts[2]}")
            roots.setdefault(parts[0], root)
    out: list[dict] = []
    for inst_id, g in sorted(grans.items()):
        root = roots[inst_id]
        split, div = _external_adjustment_flags(root, inst_id)
        out.append(
            {
                "instrument_id": inst_id,
                "granularities": sorted(g, key=lambda x: _order.get(x, 99)),
                # Adjustment status from the manifest (None = unknown).
                "adjusted": split,  # legacy alias of split_adjusted
                "split_adjusted": split,
                "dividend_adjusted": div,
                "coverage_gaps": _external_coverage_gaps(root, inst_id),
            }
        )
    return out


def external_instrument_object(instrument_id: str):
    """Return the Nautilus instrument object (e.g. Equity) defined in an
    external catalog, or None. The real catalog definition is used so price/
    size precision match the fixed-point encoding of the stored bars."""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    for root in external_catalog_roots():
        try:
            cat = ParquetDataCatalog(str(root))
            for inst in cat.instruments():
                if str(inst.id) == instrument_id:
                    return inst
        except Exception:
            continue
    return None


def load_external_bars(
    instrument_id: str,
    granularity: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Load OHLCV bars from a read-only external Nautilus catalog.

    Returns the app-wide DataFrame contract: float columns
    [open, high, low, close, volume], UTC DatetimeIndex named 'timestamp'
    holding bar OPEN times, sorted ascending. Catalog bars are stamped at bar
    CLOSE (Nautilus convention), so the index is shifted back one interval —
    the open→close shift in ``backtest._bars_from_df`` then applies exactly
    once, same as the Bybit/Index paths.

    Decoded bars are cached as pandas parquet under EXTERNAL_CACHE_DIR (the
    external root itself is never written to). L15: cache freshness is checked
    not by an mtime comparison but by a source signature (an ordered list of
    (name, size, mtime)) — if the signature is NOT EQUAL to the value in the
    sidecar, re-decode.
    ``start``/``end`` slice inclusively; naive datetimes are treated as UTC.

    Eşzamanlılık (DeepR 2026-08-11 [ORTA]): decode→yaz dizisi `_cache_lock`
    altında, kilit alındıktan sonra cache TAZE okunur — `load_bybit_bars` ile
    aynı desen. Bu fonksiyon gerçekten eşzamanlı çağrılıyor: studio
    `NautilusBacktestAdapter.load_bars` loader'ı bilinçli olarak kilit DIŞINDA
    çağırır, `web/routes/lab.py` ve `web/routes/agent_backtest.py` de kendi
    thread'lerinden. Cache'ten karşılanan (yalnız okuyan) çağrı kilide girmez.
    """
    interval_ns = _external_interval_ns(granularity)
    located = _external_bar_dir(instrument_id, granularity)
    if located is None:
        raise ValueError(
            f"external catalog has no bars for {instrument_id} {granularity}"
        )
    root, bar_dir = located

    src_files = sorted(bar_dir.glob("*.parquet"))
    if not src_files:
        raise ValueError(f"external bar dir is empty: {bar_dir}")
    # L15: source signature — even if a file is deleted/changed (even if mtime
    # goes backwards) the equality breaks and the cache is regenerated.
    src_sig = [[f.name, f.stat().st_size, f.stat().st_mtime] for f in src_files]

    EXTERNAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = (
        EXTERNAL_CACHE_DIR
        / f"{_ticker_to_filename(instrument_id)}_{granularity}.parquet"
    )
    sig_path = cache_path.with_name(f"{cache_path.stem}_srcsig.json")

    def _read_cache() -> pd.DataFrame | None:
        if not (cache_path.exists() and sig_path.exists()):
            return None
        try:
            if json.loads(sig_path.read_text()) == src_sig:
                return pd.read_parquet(cache_path)
        except Exception:
            return None  # corrupt cache/signature — re-decode
        return None

    df = _read_cache()  # okuma yolu kilitsiz

    if df is None:
        # Yazma yolu: decode + yazma tek bir kilit altında (bybit yolundaki
        # duruş — pahalı iş de kilidin içinde kalır ki bekleyen ikinci çağıran
        # aynı seriyi ikinci kez çözmesin).
        with _cache_lock(cache_path.with_suffix(".lock")):
            # Kilit altında TAZE oku: beklerken öbür çağıran zaten yazmış olabilir.
            df = _read_cache()
            if df is None:
                from nautilus_trader.persistence.catalog import ParquetDataCatalog

                cat = ParquetDataCatalog(str(root))
                bars = cat.bars(bar_types=[bar_dir.name])
                if not bars:
                    raise ValueError(f"no bars decoded for {bar_dir.name}")
                recs = [
                    (
                        b.ts_event - interval_ns,
                        float(b.open),
                        float(b.high),
                        float(b.low),
                        float(b.close),
                        float(b.volume),
                    )
                    for b in bars
                ]
                # ts_event is bar CLOSE — shift back one interval to OPEN-time
                # index. L15: a SINGLE pass over the bar list.
                df = pd.DataFrame(
                    recs, columns=["ts_ns", "open", "high", "low", "close", "volume"]
                )
                df.index = pd.to_datetime(
                    df.pop("ts_ns").to_numpy(), unit="ns", utc=True
                )
                df.index.name = "timestamp"
                df = df[~df.index.duplicated(keep="last")].sort_index()
                try:
                    _atomic_to_parquet(df, cache_path)  # M3a
                    _atomic_write_json(src_sig, sig_path)
                except Exception:
                    pass  # cache is an optimization; the frame itself is valid

    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        df = df[df.index >= start]
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        df = df[df.index <= end]

    # M21: unadjusted-series warning — backtest results are wrong on splits/dividends.
    if _external_adjusted_flag(root, instrument_id) is False:
        log.warning(
            "external series UNADJUSTED: %s %s — no split/dividend adjustment, "
            "returns will be computed from raw price",
            instrument_id,
            granularity,
        )
    # M21: split-suspicion scan on daily bars — |single-bar return| > 40%.
    if granularity.endswith("-DAY") and len(df) > 1:
        rets = df["close"].pct_change().abs()
        susp = rets > 0.40
        n_susp = int(susp.sum())
        if n_susp:
            log.warning(
                "split suspicion: %s daily series has %d bars with "
                "|single-bar return|>40%% (first %s) — series may be unadjusted",
                instrument_id,
                n_susp,
                df.index[susp][0].date(),
            )
    return df


def external_data_gap_report(
    df: pd.DataFrame,
    *,
    max_calendar_days: int = 14,
    granularity: str = "",
) -> dict[str, Any] | None:
    """Return the largest suspicious calendar gap in an external series.

    Weekends and ordinary exchange holidays fit comfortably under 14 days.
    Multi-month/year holes do not, and silently annualizing/ranking across them
    creates non-reproducible edge.  For known intraday granularities, also flag
    a large *same-session* gap. This intentionally avoids guessing overnight or
    holiday sessions, keeping the validation safe for equity and crypto feeds.
    """
    if df is None or len(df) < 2 or not isinstance(df.index, pd.DatetimeIndex):
        return None
    idx = pd.DatetimeIndex(df.index).sort_values()
    active_days = idx.normalize().unique().sort_values()
    if len(active_days) >= 2:
        deltas = active_days[1:] - active_days[:-1]
        largest_idx = int(deltas.argmax())
        largest_days = int(deltas[largest_idx] / pd.Timedelta(days=1))
        if largest_days > max_calendar_days:
            return {
                "kind": "calendar",
                "days": largest_days,
                "from": active_days[largest_idx].date().isoformat(),
                "to": active_days[largest_idx + 1].date().isoformat(),
                "max_allowed_days": int(max_calendar_days),
            }

    # Do not infer a cadence from arbitrary caller text.  A same-date gap of
    # more than three expected bars is a conservative corruption/missing-data
    # signal; cross-date gaps remain governed by the calendar rule above.
    interval_minutes = {
        "1-MINUTE": 1,
        "5-MINUTE": 5,
        "15-MINUTE": 15,
        "30-MINUTE": 30,
        "1-HOUR": 60,
        "4-HOUR": 240,
        "12-HOUR": 720,
    }.get(str(granularity).upper())
    if not interval_minutes:
        return None
    deltas = idx[1:] - idx[:-1]
    same_day = idx[1:].normalize() == idx[:-1].normalize()
    max_gap = pd.Timedelta(minutes=interval_minutes * 3)
    suspect = (deltas > max_gap) & same_day
    if not suspect.any():
        return None
    suspect_i = next(i for i, value in enumerate(suspect) if value)
    return {
        "kind": "intraday",
        "from": idx[suspect_i].isoformat(),
        "to": idx[suspect_i + 1].isoformat(),
        "gap_minutes": int(deltas[suspect_i] / pd.Timedelta(minutes=1)),
        "max_allowed_minutes": interval_minutes * 3,
    }


def coverage_range(
    source: str,
    *,
    symbol: str = "",
    category: str = "linear",
    interval: str = "",
    ticker: str = "",
    granularity: str = "",
    instrument_id: str = "",
) -> dict | None:
    """Cached-data coverage of ONE bar series → ``{"start","end"}`` ISO dates.

    Backs the date pickers' "MAX" button: a cheap single-cell lookup — bybit/
    index read the pandas cache's parquet FOOTER stats, the external catalog
    reads date ranges from parquet FILENAMES; no bars are decoded. Coverage =
    what is already cached/ingested for that series (Bybit's exchange history
    can extend further back; ``load_bybit_bars`` backfills on demand when an
    explicit earlier start is requested). ``None`` → nothing cached yet.
    """
    first: str | None = None
    last: str | None = None
    if source == "bybit" and symbol and interval:
        stats = _read_parquet_stats(_bybit_cache_path(category, symbol, interval))
        if stats and stats["first"]:
            first, last = stats["first"], stats["last"]
    elif source == "index" and ticker and granularity:
        safe = _ticker_to_filename(ticker)
        stats = _read_parquet_stats(INDEX_CACHE_DIR / f"{safe}_{granularity}.parquet")
        if stats and stats["first"]:
            first, last = stats["first"], stats["last"]
    elif source == "external" and instrument_id and granularity:
        # BarType directory: {instrument}-{step}-{AGG}-{price}-{source}; the
        # "1-HOUR" style granularity is exactly the {step}-{AGG} infix. File
        # names carry the range: <startNs…Z>_<endNs…Z>.parquet (both sortable).
        prefix = f"{instrument_id}-{granularity}-"
        for root in external_catalog_roots():
            bar_dir = root / "data" / "bar"
            if not bar_dir.exists():
                continue
            for d in bar_dir.iterdir():
                if not d.is_dir() or not d.name.startswith(prefix):
                    continue
                files = sorted(p.name for p in d.glob("*.parquet"))
                if not files:
                    continue
                f0 = files[0].split("_")[0]
                l0 = files[-1].split("_")[1].replace(".parquet", "")
                if f0 and (first is None or f0 < first):
                    first = f0
                if l0 and (last is None or l0 > last):
                    last = l0
    if not first or not last:
        return None
    # ISO timestamps and catalog filename stamps both lead with YYYY-MM-DD.
    return {"start": str(first)[:10], "end": str(last)[:10]}


def _external_catalog_source_label() -> str:
    """Which root(s) the /data external panel is actually reading from.

    DeepR 2026-08-08 [DÜŞÜK]: the panel title was hardcoded "(NAU_ev)"
    regardless of which root(s) were live — when only the NAU_ev desk root
    (D:\\NAU_ev\\... by default, unreachable on most boxes) was configured but
    missing, the panel wasn't empty as the title implied: EQUITY_CATALOG_DIR
    (this project's own ingest_equities.py output) silently filled it
    instead, so the user read "NAU_ev" data that was actually their own
    local ingest. Same "root exists + has a data/bar dir" test the row
    scanner uses.
    """
    active = [r for r in external_catalog_roots() if (r / "data" / "bar").exists()]
    has_local = EQUITY_CATALOG_DIR in active
    has_other = any(r != EQUITY_CATALOG_DIR for r in active)
    if has_local and has_other:
        return "NAU_ev + local ingest"
    if has_local:
        return "local ingest"
    if has_other:
        return "NAU_ev"
    return "not found"


def list_catalog(
    index_query: str | None = None,
    index_limit: int | None = 50,
    external_query: str | None = None,
    external_limit: int | None = 50,
) -> dict:
    """Return a dict grouping instrument rows by source.

    Structure:
        {
          "bybit":  [row_per_symbol],
          "index":  [row_per_ticker],
          "index_total": <int | None>,     # None means registry missing
          "index_query": query,
          "index_limit": limit,
        }
    """
    total_index_tickers: int | None = None
    if TICKER_REGISTRY.exists():
        try:
            total_index_tickers = len(
                json.loads(TICKER_REGISTRY.read_text()).get("tickers", [])
            )
        except Exception:  # pragma: no cover
            total_index_tickers = None

    external = _external_catalog_rows(query=external_query, limit=external_limit)

    return {
        "bybit": _bybit_rows(),
        "index": _index_rows(limit=index_limit, query=index_query),
        "index_total": total_index_tickers,
        "index_query": index_query,
        "index_limit": index_limit,
        "external": external["rows"],
        "external_total": external["total"],
        "external_matched": external["matched"],
        "external_query": external_query,
        "external_limit": external_limit,
        "external_source_label": _external_catalog_source_label(),
    }


def refresh_row(source: str, **kw) -> dict:
    """Force-refresh a catalog cell and return the updated row.

    Kwargs by source:
      - bybit:  symbol, category, interval  (+ optional days=7)
      - index:  ticker, granularity  (+ optional start/end datetimes)
    """
    if source == "bybit":
        symbol = kw["symbol"]
        category = kw["category"]
        interval = kw["interval"]
        if interval not in _BYBIT_MS:
            raise ValueError(
                f"unsupported interval {interval!r}; supported: {list(_BYBIT_MS)}"
            )
        days = int(kw.get("days", 7))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        load_bybit_bars(
            symbol=symbol,
            interval=interval,
            category=category,
            start=start,
            end=end,
            force_refresh=True,
        )
        # The TTL cache above must not hand back the pre-refresh row here.
        _invalidate_bybit_rows_cache()
        # Return the whole row for this symbol so all cells refresh.
        for row in _bybit_rows():
            if row["key"] == symbol:
                return row
        raise RuntimeError(f"row not built for symbol {symbol}")

    if source == "index":
        ticker = kw["ticker"]
        granularity = kw.get("granularity", "1d")
        raw_end = kw.get("end")
        raw_start = kw.get("start")
        end_date = (
            datetime.fromisoformat(raw_end).date()
            if raw_end
            else datetime.now(UTC).date()
        )
        start_date = (
            datetime.fromisoformat(raw_start).date()
            if raw_start
            else end_date - timedelta(days=365)
        )
        load_index_bars(
            ticker, start=start_date, end=end_date, granularity=granularity, force=True
        )
        # The TTL cache above must not hand back the pre-refresh row here.
        _invalidate_index_rows_cache()
        # Return the row for this specific ticker.
        for row in _index_rows(query=ticker, limit=1):
            if row["key"] == ticker:
                return row
        raise RuntimeError(f"row not built for ticker {ticker}")

    raise ValueError(f"unknown source {source!r}")


def rebuild_nautilus_catalog(*, wipe: bool = True, progress_fn=None) -> dict:
    """Wipe and rebuild the Nautilus ParquetDataCatalog from the pandas caches.

    Phase 1 changed the on-disk catalog contract in two ways: bar timestamps now
    sit at bar CLOSE (see ``backtest._bars_from_df``) and keys are per-category
    (``BYBIT_SPOT``/``BYBIT_LINEAR``/``BYBIT_INVERSE``). The existing catalog
    therefore holds stale open-time bars under category-collapsed ``*.BYBIT-*``
    keys and must be regenerated. Pandas caches are unaffected (the shift happens
    at Bar-build time), so they are reused as the source of truth here.

    Iterates every cached ``(category, symbol, interval)`` and re-writes it via
    ``_auto_write_bybit_catalog`` — whose per-bar_type delete-then-write is now
    category-scoped, making this idempotent. Returns a summary dict.
    """
    import shutil

    def _p(msg: str) -> None:
        if progress_fn:
            progress_fn(msg)
        else:
            print(msg, flush=True)

    if wipe and NAUTILUS_CATALOG_DIR.exists():
        _p(f"[rebuild] wiping {NAUTILUS_CATALOG_DIR}")
        shutil.rmtree(NAUTILUS_CATALOG_DIR)
    NAUTILUS_CATALOG_DIR.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []
    skipped = 0
    for category in BYBIT_CATEGORIES:
        for symbol in BYBIT_SYMBOLS:
            for interval_code, _label in BYBIT_ALL_INTERVALS:
                if interval_code not in _BYBIT_MS:
                    continue
                cache_path = _bybit_cache_path(category, symbol, interval_code)
                if not cache_path.exists():
                    skipped += 1
                    continue
                df = pd.read_parquet(cache_path)
                if df.empty:
                    skipped += 1
                    continue
                _auto_write_bybit_catalog(symbol, interval_code, df, category)
                written.append(
                    {
                        "category": category,
                        "symbol": symbol,
                        "interval": interval_code,
                        "rows": len(df),
                    }
                )
                _p(f"[rebuild] {category} {symbol} {interval_code}: {len(df):,} bars")

    _p(f"[rebuild] done · {len(written)} series written · {skipped} skipped")
    return {
        "written": written,
        "skipped": skipped,
        "catalog_path": str(NAUTILUS_CATALOG_DIR),
    }


if __name__ == "__main__":
    import sys

    # Progress logs use arrow/·/… glyphs; force UTF-8 so the CLI doesn't crash
    # on Windows consoles (cp1254). Mirrors server.py's stdout reconfiguration.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    if len(sys.argv) > 1 and sys.argv[1] in ("rebuild-catalog", "rebuild"):
        _summary = rebuild_nautilus_catalog()
        print(
            f"Rebuilt {len(_summary['written'])} series "
            f"({_summary['skipped']} skipped) → {_summary['catalog_path']}"
        )
        sys.exit(0)

    end = datetime.now(UTC)
    start = end - timedelta(days=2)
    for iv in ("1", "5"):
        b = load_bybit_bars("BTCUSDT", interval=iv, start=start, end=end)
        print(
            f"bybit BTCUSDT {iv}m: rows={len(b)} start={b.index[0] if len(b) else '-'} end={b.index[-1] if len(b) else '-'}"
        )
