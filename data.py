"""Data ingestion — Bybit v5 klines, US-index tick CSVs.

Provides the DataFrame surface consumed by ``backtest.py``. Every source
maintains its own on-disk parquet cache under ``~/.cache/nautilus_web_app/``.

Wiki References
---------------
Bkz: [[data_engine]], [[data_wranglers]], [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]], [[index_backtest_via_equity_proxy]]

Feeds raw OHLCV frames into `backtest.py`. Cache-tail-forward parquets are the
local analog of [[parquet_data_catalog]] — same nanosecond-timestamp Parquet idea,
smaller scope.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd
import requests

from app_constants import NO_WINDOW_FLAGS

log = logging.getLogger(__name__)

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
EXTERNAL_CATALOGS: list[Path] = [
    Path(p.strip())
    for p in os.environ.get(
        "NAUTILUS_EXTERNAL_CATALOGS",
        r"E:\myAI_Projects\NAU_ev\backend\data\catalog",
    ).split(os.pathsep)
    if p.strip()
]

# L31: var olmayan kök sessizce boş panele dönüşüyordu — modül yüklenirken uyar.
for _ext_root in EXTERNAL_CATALOGS:
    if not _ext_root.exists():
        log.warning("EXTERNAL_CATALOGS kökü mevcut değil: %s", _ext_root)

# Cache instrument metadata per external catalog root (read-only, static).
_EXT_INSTRUMENT_META: dict[str, dict] = {}

# Cache the optional _manifest.json per external catalog root (M21).
_EXT_MANIFEST: dict[str, dict] = {}


# ---- Eşzamanlılık + atomik yazma yardımcıları (M3) --------------------------
#
# Aynı cache dosyasına iki istek (thread ya da süreç) aynı anda
# read-modify-write yaparsa sonuncu kazanır ve aradaki barlar kaybolur.
# İki katmanlı kilit:
#   1) süreç-içi: anahtar başına threading.Lock
#   2) süreçler-arası: hedef dosyanın yanında O_CREAT|O_EXCL lock dosyası
_PROC_LOCKS: dict[str, threading.Lock] = {}
_PROC_LOCKS_GUARD = threading.Lock()


@contextmanager
def _cache_lock(lock_path: Path, timeout: float = 120.0, stale_after: float = 1800.0):
    """Süreç-içi + süreçler-arası basit kilit.

    `timeout` saniye içinde alınamazsa TimeoutError. `stale_after` (varsayılan
    30 dk) saniyeden ESKİ bir lock dosyası çökmüş sürecin artığı sayılıp bir kez
    kırılır. M121: eskiden bayat-eşiği wait-timeout ile AYNIYDI (60sn) ve uzun
    backfill'ler (3 yıl 1m ~10 dk lock tutar) rutin 60sn'yi aştığından ikinci
    bir süreç CANLI kilidi kırıyordu. Bayat eşiği artık her meşru tutuştan çok
    daha uzun — yalnız gerçekten terk edilmiş kilit kırılır.
    """
    key = str(lock_path)
    with _PROC_LOCKS_GUARD:
        tlock = _PROC_LOCKS.setdefault(key, threading.Lock())
    if not tlock.acquire(timeout=timeout):
        raise TimeoutError(f"süreç-içi kilit alınamadı: {lock_path}")
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
                        continue  # dosya bu arada silinmiş — hemen tekrar dene
                    if age > stale_after and not broke_stale:
                        log.warning(
                            "bayat lock dosyası kırılıyor (%.0fs): %s", age, lock_path
                        )
                        try:
                            lock_path.unlink()
                        except OSError:
                            pass
                        broke_stale = True
                        deadline = time.monotonic() + 5.0
                        continue
                    raise TimeoutError(f"kilit zaman aşımı: {lock_path}")
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


def _atomic_to_parquet(df: pd.DataFrame, path: Path) -> None:
    """M3a: to_parquet → geçici dosya + os.replace — yarım parquet kalmaz."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        df.to_parquet(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_json(obj, path: Path) -> None:
    """JSON sidecar'ları da atomik yaz (M3a ile aynı desen)."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


Granularity = Literal["1d", "1m"]
_GRAN_RULE = {"1d": "1D", "1m": "1min"}


def _ticker_to_filename(t: str) -> str:
    """`I:AAVE100` -> `I_AAVE100` (filesystem-safe; Nautilus Symbol keeps raw)."""
    return t.replace(":", "_").replace("/", "_")


def _index_file_for(d: date) -> Path:
    return INDEX_ROOT / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.isoformat()}.csv.gz"


def _latest_available_file() -> Path | None:
    if not INDEX_ROOT.exists():
        return None
    files = sorted(INDEX_ROOT.glob("*/*/*.csv.gz"), reverse=True)
    return files[0] if files else None


def discover_index_tickers(force: bool = False) -> list[str]:
    """Return sorted list of unique tickers seen in the newest daily file.

    Caches to `TICKER_REGISTRY` as JSON. First run scans a ~2 GB gzip via awk
    (streamed) and takes minutes; subsequent calls are instant.
    """
    if TICKER_REGISTRY.exists() and not force:
        try:
            data = json.loads(TICKER_REGISTRY.read_text())
            tickers = data.get("tickers") or []
            if tickers:
                return tickers
        except Exception:
            pass

    src = _latest_available_file()
    if src is None:
        raise RuntimeError(f"no index files found under {INDEX_ROOT}")

    import shlex as _shlex

    # Tickers are stored in contiguous blocks — awk print-when-changed is
    # both memory-cheap and file-order deterministic.
    cmd = (
        f"gunzip -c {_shlex.quote(str(src))} | "
        f"awk -F, 'NR>1 && $1!=prev {{print $1; prev=$1}}'"
    )
    proc = subprocess.run(
        ["bash", "-c", cmd],
        check=True,
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW_FLAGS,  # Windows: konsol penceresi açma
    )
    tickers = sorted(
        set(line.strip() for line in proc.stdout.splitlines() if line.strip())
    )

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
    """Filter one day's CSV.gz for `ticker` via awk pipe → DataFrame.

    Returns empty DataFrame with the same columns if the day is missing.
    """
    src = _index_file_for(day)
    if not src.exists():
        return pd.DataFrame(columns=["ticker", "value", "timestamp"])

    import shlex as _shlex

    # Use list-form awk with -v to avoid shell injection via ticker name.
    cmd = [
        "bash",
        "-c",
        f"gunzip -c {_shlex.quote(str(src))} | "
        f"awk -F, -v T={_shlex.quote(ticker)} 'NR==1 || $1==T'",
    ]
    # Windows: her ticker yüklemesinde konsol penceresi açılıp kapanmasın.
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, creationflags=NO_WINDOW_FLAGS
    ) as proc:
        try:
            df = pd.read_csv(proc.stdout)
        finally:
            proc.stdout.close()
            proc.wait()
    if df.empty:
        return df
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
    # M34: NAU konvansiyonu — index serilerinde hacim kavramı yok. Eskiden
    # tick sayısı (count) yazılıyordu; bu sahte hacim volume tabanlı blokları
    # yanıltabiliyordu. volume_spike'ın avg<=0 guard'ı sayesinde 0 no-op olur.
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
    named 'timestamp'. Volume her zaman 0'dır (M34 — index'te hacim yok).

    ``force=True`` cache'lenmiş gün kümesini ve 'tarandı-ama-boş' sidecar'ını
    yok sayar: istenen aralıktaki her gün kaynaktan yeniden taranır.

    H3: bitmemiş gün (bugün, UTC) kalıcı parquet'e yazılmaz — bellek-içi
    dönüşte kalabilir; gün tamamlanınca bir sonraki çağrı yeniden çeker.
    """
    if granularity not in _GRAN_RULE:
        raise ValueError(f"granularity must be one of {list(_GRAN_RULE)}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = _ticker_to_filename(ticker)
    cache_path = INDEX_CACHE_DIR / f"{safe}_{granularity}.parquet"
    # M20: taranmış ama veri çıkmamış GEÇMİŞ günlerin sidecar'ı — her çağrının
    # aynı boş günleri (tatil, seri başlangıcından önce, …) yeniden taramasını
    # engeller.
    scanned_path = INDEX_CACHE_DIR / f"{safe}_{granularity}_scanned.json"

    empty_cols = ["open", "high", "low", "close", "volume"]
    existing = pd.DataFrame(columns=empty_cols)
    cached_days: set[date] = set()
    if cache_path.exists():
        try:
            existing = pd.read_parquet(cache_path)
        except Exception as read_err:
            # M3c: bozuk parquet — cache'i yok say, günler yeniden taranır.
            log.warning(
                "bozuk index cache yok sayılıyor (%s): %s", cache_path, read_err
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

    wanted_days = []
    d = start
    while d <= end:
        wanted_days.append(d)
        d += timedelta(days=1)
    missing_days = [
        d for d in wanted_days if d not in cached_days and d not in scanned_empty
    ]

    today_utc = datetime.now(UTC).date()
    new_frames: list[pd.DataFrame] = []
    newly_empty: list[date] = []
    for day in missing_days:
        rows = _stream_ticker_rows(ticker, day)
        bars = (
            _aggregate_ohlc(rows, granularity)
            if not rows.empty
            else pd.DataFrame(columns=empty_cols)
        )
        if bars.empty:
            # M20: yalnız GEÇMİŞ günleri 'boş' işaretle — bugün (H3) hâlâ
            # oluşuyor olabilir, bir sonraki çağrıda yeniden denenmeli.
            # H371: KAYNAK DOSYA VARSA ve ticker o gün yoksa 'tarandı-boş'tur
            # (kalıcı). Ama dosya HİÇ YOKSA (NFS kesintisi / geç yayımlanan
            # günlük dosya) günü zehirleme — sonra dosya gelince yeniden
            # taransın. Aksi halde geçici kesinti geçmiş günleri kalıcı siler.
            if day < today_utc and _index_file_for(day).exists():
                newly_empty.append(day)
            continue
        new_frames.append(bars)

    if newly_empty:
        merged = sorted(prior_scanned | {d.isoformat() for d in newly_empty})
        _atomic_write_json(merged, scanned_path)

    if new_frames:
        combined = pd.concat([existing, *new_frames])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        # H3: bugünün (UTC) barları henüz tamamlanmadı — diske yazma, yalnız
        # geçmiş günleri kalıcılaştır. combined bellek-içi dönüşte tam kalır.
        persist = combined[combined.index.normalize().date < today_utc]
        _atomic_to_parquet(persist, cache_path)  # M3a: atomik yazım
    else:
        combined = existing

    if combined.empty:
        return combined

    # Return only the requested window.
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    mask = (combined.index >= start_ts) & (combined.index < end_ts)
    result = combined.loc[mask].copy()
    # M34: eski cache'lerde count tabanlı sahte hacim kalmış olabilir —
    # migrasyona gerek kalmadan dönüşte volume=0 garanti edilir.
    result["volume"] = 0.0
    return result


# ------------------------------------------------------------------ bybit


BybitCategory = Literal["spot", "linear", "inverse"]
BybitInterval = Literal["1", "5", "15", "60", "240", "D"]
_BYBIT_URL = "https://api.bybit.com/v5/market/kline"
_BYBIT_LIMIT = 1000  # max rows per request
_BYBIT_MS = {
    "1": 60_000,  # 1 minute
    "5": 300_000,  # 5 minutes
    "15": 900_000,  # 15 minutes
    "60": 3_600_000,  # 1 hour
    "240": 14_400_000,  # 4 hours
    "D": 86_400_000,  # 1 day
}


def _bybit_cache_path(category: str, symbol: str, interval: str) -> Path:
    safe = symbol.replace("/", "_")
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
            r = requests.get(_BYBIT_URL, params=params, timeout=30)
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


def _bybit_gap_frames(
    cached: pd.DataFrame, cache_path: Path, step_ms: int, fetch_segment
) -> list[pd.DataFrame]:
    """M2: cache'lenmiş aralığın İÇİNDEKİ delikleri bul ve hedefli doldur.

    Interval sabit adımlı olduğundan [ilk, son] aralığının beklenen bar sayısı
    aritmetiktir; gerçek satır sayısı eksikse ardışık bar açılışları arasındaki
    > step_ms boşluklar delik demektir. Dolgu boş dönerse aralık
    '{cache_adı}_gaps.json' sidecar'ında 'bilinen boş' işaretlenir ki her
    çağrı aynı aralığı yeniden denemesin (ör. borsa kesintisi, listing arası).
    """
    try:
        idx_ns = cached.index.as_unit("ns").asi8
    except AttributeError:  # eski pandas: asi8 zaten ns
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
    if not gaps:
        return []

    gaps_path = cache_path.with_name(f"{cache_path.stem}_gaps.json")
    known: list[list[int]] = []
    if gaps_path.exists():
        try:
            known = json.loads(gaps_path.read_text())
        except Exception:
            known = []

    frames: list[pd.DataFrame] = []
    known_changed = False
    for gs, ge in gaps:
        if any(ks <= gs and ge <= ke for ks, ke in known):
            continue  # bilinen boş aralık — API'yi yeniden yorma
        log.warning(
            "bybit cache deliği (%s): %s → %s (%d bar eksik)",
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
                "delik dolduruldu (%s): %d bar geldi", cache_path.name, rows_in_gap
            )
        else:
            known.append([gs, ge])
            known_changed = True
            log.warning(
                "delik boş döndü — 'bilinen boş' işaretlendi (%s): %s → %s",
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

    # H604: inverse (coin-margined) kontratların GERÇEK borsa sembolü BTCUSD'dir
    # (kanonik BTCUSDT değil). Fetch yolunda dönüşüm yapılmıyordu ve Bybit v5,
    # inverse&symbol=BTCUSDT'yi sessizce LINEAR seriyle yanıtlıyordu → inverse
    # cache/katalog yanlış (linear) veriyle doluyordu (canlı doğrulandı). Cache
    # ANAHTARI kanonik kalır (tutarlılık); yalnız API çağrısı market sembolü alır.
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
                # H2: boş pencere fetch'i BİTİRMEZ — pencereyi atla ve devam
                # et (listing öncesi boşluk / kesinti sonrası veri sürebilir).
                # Boş pencerede API'den satır akmadı; pacing beklemesi gereksiz.
                cur = page_end + 1
                continue
            frames.append(page)
            last_ms = int(page.index[-1].timestamp() * 1000)
            next_cur = last_ms + step_ms
            # Guard against a stuck page (e.g., API returns same last row).
            if next_cur <= cur:
                break
            cur = next_cur
            time.sleep(0.15)  # conservative pacing for large parallel fetches
        return frames

    # M3b: read-modify-write bloğu anahtar-başına kilit altında — aynı
    # (category, symbol, interval) için eşzamanlı istekler birbirinin yeni
    # barlarını ezemez (thread + süreç düzeyinde).
    with _cache_lock(cache_path.with_suffix(".lock")):
        cached = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # H626: force_refresh'te de mevcut cache OKUNUR — eskiden okunmuyordu ve
        # yalnız [start,end) yazılıp pencere DIŞINDAKİ tüm barlar siliniyordu
        # (/data refresh butonu days=7 ile yılların 1m geçmişini 7 güne
        # indiriyordu). Şimdi taze pencere cache'le BİRLEŞTİRİLİR (dedup
        # keep='last' → istenen pencere tazelenir, dışı korunur).
        if cache_path.exists():
            try:
                cached = pd.read_parquet(cache_path)
            except Exception as read_err:
                # M3c: bozuk parquet — cache'i yok say, aralığı yeniden çek.
                log.warning(
                    "bozuk bybit cache yok sayılıyor (%s): %s", cache_path, read_err
                )
                cached = pd.DataFrame(
                    columns=["open", "high", "low", "close", "volume"]
                )

        new_frames: list[pd.DataFrame] = []
        if force_refresh:
            # İstenen pencereyi KOŞULSUZ yeniden çek (bayat/yarım barları
            # tazele); cache'in geri kalanı concat+dedup ile korunur.
            new_frames = _fetch_segment(start_ms, end_ms)
        elif cached.empty:
            new_frames = _fetch_segment(start_ms, end_ms)
        else:
            cached_start_ms = int(cached.index[0].timestamp() * 1000)
            cached_end_ms = int(cached.index[-1].timestamp() * 1000)
            # Backfill older history when the request predates the cache, so a
            # wider `start` actually widens the returned range.
            if start_ms < cached_start_ms:
                new_frames += _fetch_segment(start_ms, cached_start_ms)
            # M2: cache içi delikleri tespit et ve hedefli doldur.
            new_frames += _bybit_gap_frames(cached, cache_path, step_ms, _fetch_segment)
            # H1a: kuyruk SON cache'li bardan başlar (cached_end_ms + step_ms
            # değil) — son bar yazıldığında oluşmakta olan yarım bar olabilir;
            # yeniden çekilir ve dedup keep='last' onu tazesiyle değiştirir.
            tail_from = cached_end_ms
            if tail_from < end_ms:
                new_frames += _fetch_segment(tail_from, end_ms)

        if new_frames:
            combined = pd.concat([cached, *new_frames])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            # H1b: OLUŞMAKTA OLAN barı diske ve Nautilus catalog'a yazmadan
            # kırp — açılışı + step henüz geçmemişse bar tamamlanmamıştır.
            if not combined.empty:
                now_ms = int(time.time() * 1000)
                last_open_ms = int(combined.index[-1].timestamp() * 1000)
                if last_open_ms + step_ms > now_ms:
                    combined = combined.iloc[:-1]
            _atomic_to_parquet(combined, cache_path)  # M3a: atomik yazım
        else:
            combined = cached

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
            print(
                f"[catalog] auto-write failed for {symbol}/{interval}: {e}", flush=True
            )

    return result


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
    ("60", "1h"),
    ("240", "4h"),
    ("D", "1d"),
)


def _read_parquet_stats(path: Path) -> dict | None:
    """Return {rows, first, last} for an existing OHLCV parquet, or None."""
    if not path.exists():
        return None
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
    """Ticker suffix'inden base para birimini çıkar (BTCUSDT→BTC, ETHUSDC→ETH).

    Kaba ``symbol[:-4]``/``symbol[:3]`` USDC (5 harf) ve 4+ harfli base'leri
    yanlış etiketliyordu; _instrument_meta'daki suffix-soyma ile aynı mantık.
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
        base_guess = "BTC"
        for suffix in ("USDT", "USDC", "USD"):
            if symbol.endswith(suffix):
                base_guess = symbol[: -len(suffix)] or "BTC"
                break
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
                "text": "size_precision=0 — kesirli trade_size sessizce 0'a çevrilir; RiskEngine emri düşer.",
            }
        ]
    return []


# ---- Per-source row builders ----------------------------------------------


def _bybit_rows() -> list[dict]:
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


def _index_rows(limit: int | None = None, query: str | None = None) -> list[dict]:
    """List US-index tickers. If _tickers.json is missing, return an empty list;
    the UI shows a Discover call-to-action in that case.
    """
    if not TICKER_REGISTRY.exists():
        return []
    reg = json.loads(TICKER_REGISTRY.read_text())
    tickers: list[str] = reg.get("tickers", [])
    if query:
        q = query.lower()
        tickers = [t for t in tickers if q in t.lower()]
    if limit:
        tickers = tickers[:limit]

    rows: list[dict] = []
    for ticker in tickers:
        meta = _instrument_meta("index", ticker=ticker)
        bar_types = []
        for gran in ("1d", "1m"):
            step, aggregation = (1, "DAY") if gran == "1d" else (1, "MINUTE")
            safe = _ticker_to_filename(ticker)
            cache_path = INDEX_CACHE_DIR / f"{safe}_{gran}.parquet"
            stats = _read_parquet_stats(cache_path)
            bt = _bar_type_dsl(
                meta["instrument_id"], step=step, aggregation=aggregation
            )
            bt.update(
                {
                    "granularity": gran,
                    "state": "ingested" if stats else "available",
                    "rows": stats["rows"] if stats else 0,
                    "first": stats["first"] if stats else None,
                    "last": stats["last"] if stats else None,
                    "cache_path": str(cache_path),
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

    L20: varsayılan yol salt KUYRUK-EKLEME — katalogdaki son ts_event
    (last_ns, ucuz M19 state'i) okunur ve yalnız ondan SONRA kapanan barlar
    ``write_data`` ile eklenir (Nautilus append'i yeni parquet dosyası olarak
    yazar). Tam delete+rewrite yalnız backfill (df, katalog başlangıcından
    önce başlıyor) veya onarım (katalog aralığında eksik satır / state
    okunamadı) durumunda çalışır. Kuyruk yazımı kesinlikle last_ns'ten SONRA
    başlar — mükerrer bar riski yok.

    M3b: delete+write çifti ve append yolu, bar_type-başına kilit altında.
    """
    from nautilus_trader.model.data import Bar

    from backtest import _bars_from_df, _make_bybit_bar_type, _make_bybit_instrument

    # Infer base from the canonical symbol (BTCUSDT → BTC); quote is derived from
    # the category by _make_bybit_instrument (USDT for spot/linear, USD for inverse).
    base = _base_ccy(symbol)
    instrument = _make_bybit_instrument(symbol=symbol, base=base, category=category)
    bar_type = _make_bybit_bar_type(instrument.id, interval)
    if df.empty:
        return
    interval_ns = _BYBIT_MS[interval] * 1_000_000

    NAUTILUS_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    # Kilit adı düz string girdilerden türetilir (bar_type nesnesi test'te
    # mock'lanabilir; dosya adına güvenle giremez).
    lock_path = NAUTILUS_CATALOG_DIR / f"{category}_{symbol}_{interval}.lock"
    with _cache_lock(lock_path):
        catalog = get_nautilus_catalog()
        catalog.write_data([instrument])

        state = nautilus_catalog_bar_state(str(bar_type))
        first_ns = state["first_ns"] if state else None
        last_ns = state["last_ns"] if state else None
        # df index'i bar OPEN zamanı; katalog ts_event = open + interval (CLOSE).
        # pd.Timestamp() sarmalaması indeks tipine karşı savunma (testler düz
        # RangeIndex'li df geçirebiliyor).
        df_first_close_ns = int(pd.Timestamp(df.index[0]).value) + interval_ns

        append_only = False
        if state is not None and first_ns is not None and last_ns is not None:
            # Onarım tespiti: katalog kendi [first, last] aralığında eksik
            # satır taşıyorsa (ör. M2 delik dolgusu geldi) tam rewrite gerekir.
            expected_rows = (last_ns - first_ns) // interval_ns + 1
            catalog_has_gaps = state["rows"] < expected_rows
            append_only = df_first_close_ns >= first_ns and not catalog_has_gaps

        if append_only:
            # ts_event = open + interval > last_ns  ⇔  open > last_ns - interval
            cut = pd.Timestamp(last_ns - interval_ns, unit="ns", tz="UTC")
            tail = df[df.index > cut]
            if tail.empty:
                return  # eklenecek yeni bar yok — hiç dokunma
            bars = _bars_from_df(bar_type, instrument, tail)
            catalog.write_data(bars)
            print(f"[catalog] appended {len(bars):,} bars → {bar_type}", flush=True)
            return

        # Backfill / ilk yazım / onarım: tam delete + rewrite.
        bars = _bars_from_df(bar_type, instrument, df)
        try:
            catalog.delete_data_range(data_cls=Bar, identifier=str(bar_type))
        except Exception as _del_err:
            # delete_data_range 'no data/not found' ise sorun yok (ilk yazım).
            _msg = str(_del_err).lower()
            if "no data" not in _msg and "not found" not in _msg:
                # M1030: silme GERÇEKTEN başarısızsa (PermissionError, yarım
                # silme) write_data KOŞULSUZ çağrılırsa eski + yeni dosyalar
                # örtüşen aralıklar (non-disjoint) oluşturur ve sonraki okuma
                # patlar. Yazmayı iptal et — cache'ten bir sonraki fetch onarır.
                print(
                    f"[catalog] delete_data_range HATASI, yazım atlanıyor → "
                    f"{bar_type}: {_del_err}",
                    flush=True,
                )
                return
        catalog.write_data(bars)
        print(f"[catalog] wrote {len(bars):,} bars → {bar_type}", flush=True)


def get_nautilus_catalog():
    """Return a ParquetDataCatalog opened at NAUTILUS_CATALOG_DIR (lazy import)."""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    NAUTILUS_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(str(NAUTILUS_CATALOG_DIR))


def _catalog_fname_ns(stamp: str) -> int | None:
    """Katalog parquet dosya adı damgası → epoch ns.

    Örn. '2026-07-08T00-00-00-000000000Z' (Nautilus '{startZ}_{endZ}.parquet'
    adlandırması, bar CLOSE zamanları). Çözülemezse None.
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

    M19: eski sürüm tüm barları decode ediyordu (büyük serilerde saniyeler).
    Artık ucuz filesystem taraması: satır sayısı parquet footer
    metadata'sından (pyarrow.parquet.read_metadata), first/last ns dosya
    adından ('{startZ}_{endZ}.parquet'). Dönüş imzası korunur.
    """
    bar_dir = NAUTILUS_CATALOG_DIR / "data" / "bar" / bar_type_str
    try:
        if not bar_dir.is_dir():
            return None
        files = sorted(bar_dir.glob("*.parquet"))
    except OSError:  # geçersiz dizin adı (örn. Windows'ta ':') vb.
        return None
    if not files:
        return None

    import pyarrow.parquet as _pq

    rows = 0
    for f in files:
        try:
            rows += _pq.read_metadata(str(f)).num_rows
        except Exception:
            pass  # bozuk/yarım dosya — sayıma katma
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
    )

    catalog = get_nautilus_catalog()

    if source == "bybit":
        symbol = kw["symbol"]
        category = kw.get("category", "linear")
        interval = kw["interval"]
        # M1174: eskiden load_bybit_bars(days=7) MASKELENMİŞ 7 günlük pencereyi
        # alıp katalog bar_type'ının TAMAMINI silip yalnız 7 günü yazıyordu —
        # 'kataloğa yaz' butonu katalog geçmişini 7 güne indiriyordu. load_bybit_bars
        # zaten TAM combined'ı otomatik yazar; burada da TAM cache'i (parquet)
        # oku ve yaz, 7 günlük pencereyle değil.
        _cp = _bybit_cache_path(category, symbol, interval)
        if not _cp.exists():
            raise RuntimeError(
                f"No cached data for {symbol}/{category}/{interval}. "
                "Fetch it first from the Data catalog screen."
            )
        try:
            df = pd.read_parquet(_cp)
        except Exception as _e:
            raise RuntimeError(f"bybit cache okunamadı ({_cp}): {_e}") from _e
        if df.empty:
            raise RuntimeError(
                f"No cached data for {symbol}/{category}/{interval}. "
                "Fetch it first from the Data catalog screen."
            )
        instrument = _make_bybit_instrument(
            symbol=symbol,
            base=_base_ccy(symbol),
            category=category,
        )
        bar_type = _make_bybit_bar_type(instrument.id, interval)
        bars = _bars_from_df(bar_type, instrument, df)
        # #5: _auto_write_bybit_catalog ile AYNI kilit anahtarı — iki katalog
        # yazıcısı aynı bar_type'ta çakışırsa çıplak write_data 'non-disjoint
        # intervals' (HTTP 500) verir / örtüşen parquet bırakır. M1030 delete→write
        # guard'ı yalnız TÜM yazıcılar kilidi alırsa geçerli.
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
        # #5: index kaynağı için de kararlı, kendine-özgü kilit anahtarı.
        lock_path = NAUTILUS_CATALOG_DIR / f"index_{safe}_{granularity}.lock"

    else:
        raise ValueError(f"unknown source {source!r}")

    # #5: instrument + delete_data_range + write_data'yı TEK kilit altında
    # atomik yap — _auto_write_bybit_catalog ile aynı anahtar (mutual exclusion).
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
                # M1030: silme gerçekten başarısızsa yazma (non-disjoint önlemi).
                raise RuntimeError(
                    f"delete_data_range başarısız, yazım iptal: {_del_err}"
                ) from _del_err
        catalog.write_data(bars)

    return {
        "instrument_id": str(instrument.id),
        "bar_type": str(bar_type),
        "rows_written": len(bars),
        "catalog_path": str(NAUTILUS_CATALOG_DIR),
    }


def _external_instrument_meta(root: Path) -> dict:
    """Return {instrument_id: {asset_class, price_precision, size_precision,
    price_increment, quote_currency}} for one external catalog root.

    Loads the catalog's instrument definitions once and caches them (the
    external catalog is read-only reference data, so it never changes).
    """
    key = str(root)
    if key in _EXT_INSTRUMENT_META:
        return _EXT_INSTRUMENT_META[key]

    meta: dict = {}
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
                continue
    except Exception:
        meta = {}

    _EXT_INSTRUMENT_META[key] = meta
    return meta


def _external_manifest(root: Path) -> dict:
    """M21: kökteki OPSİYONEL ``_manifest.json``'ı oku ve önbelleğe al.

    Sembol → meta sözlüğü döner (NAU_ev üretimi: bars/first/last/ok/…, varsa
    'adjusted'). Dosya yoksa ya da bozuksa boş dict — 'adjusted' bilgisi
    'unknown' kalır.
    """
    key = str(root)
    if key in _EXT_MANIFEST:
        return _EXT_MANIFEST[key]
    manifest: dict = {}
    p = root / "_manifest.json"
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                manifest = loaded
    except Exception as e:
        log.warning("harici katalog manifest'i okunamadı (%s): %s", p, e)
    _EXT_MANIFEST[key] = manifest
    return manifest


def _external_adjusted_flag(root: Path, instrument_id: str) -> bool | None:
    """Manifest'teki 'adjusted' alanı: True/False, bilinmiyorsa None.

    Manifest bare sembolle anahtarlı (örn. 'NVDA', 'BRK.A'), instrument_id ise
    'NVDA.NASDAQ' / 'BRK.A.NASDAQ' biçiminde — yalnız SON nokta (venue) atılır.
    M1260: eskiden ilk nokta bölünüyordu ('BRK.A.NASDAQ' → 'BRK') ve noktalı
    sembollerin (BRK.A gerçekten adjusted=False!) UNADJUSTED uyarısı kayboluyordu.
    """
    symbol = instrument_id.rsplit(".", 1)[0]
    entry = _external_manifest(root).get(symbol)
    if isinstance(entry, dict):
        val = entry.get("adjusted")
        if isinstance(val, bool):
            return val
    return None


def _external_catalog_rows(query: str | None = None, limit: int | None = 50) -> dict:
    """Scan the read-only external catalogs and return display rows.

    Returns {"rows": [...], "total": int}. The heavy per-bar-type work
    (parquet row counts) is done only for the ``limit`` rows actually shown;
    instrument discovery and date ranges are pulled from the filesystem
    (directory + parquet filenames) without decoding any bars.
    """
    # instrument_id -> {"root": Path, "bar_dirs": [(dir_name, interval, Path)]}
    catalog_map: dict[str, dict] = {}
    for root in EXTERNAL_CATALOGS:
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

    total = len(catalog_map)

    inst_ids = sorted(catalog_map)
    if query:
        q = query.strip().lower()
        inst_ids = [i for i in inst_ids if q in i.lower()]
    matched = len(inst_ids)
    if limit is not None:
        inst_ids = inst_ids[:limit]

    # Order the timeframe buckets shortest→longest for stable display.
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

    import pyarrow.parquet as _pq

    rows: list[dict] = []
    for inst_id in inst_ids:
        entry = catalog_map[inst_id]
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
        bar_types.sort(key=lambda b: _order.get(b["granularity"], 99))
        rows.append(
            {
                "source": "external",
                "key": inst_id,
                "instrument_id": inst_id,
                # M21: manifest'ten split/temettü düzeltme durumu
                # (True/False, manifest yok ya da alan yoksa None='unknown').
                "adjusted": _external_adjusted_flag(entry["root"], inst_id),
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
        )

    return {"rows": rows, "total": total, "matched": matched}


# ── External catalog bar loading (read-only backtest data source) ────────────

_EXT_GRAN_SECONDS = {"MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800}


def _external_interval_ns(granularity: str) -> int:
    """'1-DAY' → 86400e9. Raises ValueError on anything that isn't a
    time-aggregated catalog step (defensive against form tampering).

    Not (L34): DAY/WEEK için çıkarılan süre TAKVİMSEL NOMİNAL aralıktır
    (24 saat / 7 gün), seans süresi değil. Close→open kaydırması bu nominal
    adımla yapılır; kısaltılmış seanslar / yarım günler ayrıca modellenmez.
    """
    step_s, _, unit = granularity.partition("-")
    if not step_s.isdigit() or unit not in _EXT_GRAN_SECONDS:
        raise ValueError(f"unsupported external granularity {granularity!r}")
    return int(step_s) * _EXT_GRAN_SECONDS[unit] * 1_000_000_000


def _external_bar_dir(instrument_id: str, granularity: str) -> tuple[Path, Path] | None:
    """Locate (catalog_root, bar_dir) for instrument+granularity, or None."""
    for root in EXTERNAL_CATALOGS:
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
    for root in EXTERNAL_CATALOGS:
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
    return [
        {
            "instrument_id": inst_id,
            "granularities": sorted(g, key=lambda x: _order.get(x, 99)),
            # M21: manifest'teki düzeltme durumu (None = bilinmiyor).
            "adjusted": _external_adjusted_flag(roots[inst_id], inst_id),
        }
        for inst_id, g in sorted(grans.items())
    ]


def external_instrument_object(instrument_id: str):
    """Return the Nautilus instrument object (e.g. Equity) defined in an
    external catalog, or None. The real catalog definition is used so price/
    size precision match the fixed-point encoding of the stored bars."""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    for root in EXTERNAL_CATALOGS:
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
    external root itself is never written to). L15: cache tazeliği mtime
    karşılaştırmasıyla değil, kaynak imzasıyla (sıralı (ad, boyut, mtime)
    listesi) sınanır — imza sidecar'daki değerle EŞİT değilse yeniden decode.
    ``start``/``end`` slice inclusively; naive datetimes are treated as UTC.
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
    # L15: kaynak imzası — dosya silinse/değişse de (mtime geriye gitse bile)
    # eşitlik bozulur ve cache yeniden üretilir.
    src_sig = [[f.name, f.stat().st_size, f.stat().st_mtime] for f in src_files]

    EXTERNAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = (
        EXTERNAL_CACHE_DIR
        / f"{_ticker_to_filename(instrument_id)}_{granularity}.parquet"
    )
    sig_path = cache_path.with_name(f"{cache_path.stem}_srcsig.json")

    df: pd.DataFrame | None = None
    if cache_path.exists() and sig_path.exists():
        try:
            if json.loads(sig_path.read_text()) == src_sig:
                df = pd.read_parquet(cache_path)
        except Exception:
            df = None  # bozuk cache/imza — yeniden decode

    if df is None:
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        cat = ParquetDataCatalog(str(root))
        bars = cat.bars(bar_types=[bar_dir.name])
        if not bars:
            raise ValueError(f"no bars decoded for {bar_dir.name}")
        # ts_event is bar CLOSE — shift back one interval to OPEN-time index.
        # L15: bar listesi üzerinde TEK geçiş (beş ayrı list-comp yerine).
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
        df = pd.DataFrame(
            recs, columns=["ts_ns", "open", "high", "low", "close", "volume"]
        )
        df.index = pd.to_datetime(df.pop("ts_ns").to_numpy(), unit="ns", utc=True)
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

    # M21: düzeltilmemiş seri uyarısı — backtest sonuçları split/temettüde yanılır.
    if _external_adjusted_flag(root, instrument_id) is False:
        log.warning(
            "harici seri UNADJUSTED: %s %s — split/temettü düzeltmesi yok, "
            "getiriler ham fiyattan hesaplanacak",
            instrument_id,
            granularity,
        )
    # M21: günlük barlarda split şüphesi taraması — |tek-bar getiri| > %40.
    if granularity.endswith("-DAY") and len(df) > 1:
        rets = df["close"].pct_change().abs()
        susp = rets > 0.40
        n_susp = int(susp.sum())
        if n_susp:
            log.warning(
                "split şüphesi: %s günlük seride |tek-bar getiri|>%%40 olan "
                "%d bar (ilki %s) — seri unadjusted olabilir",
                instrument_id,
                n_susp,
                df.index[susp][0].date(),
            )
    return df


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
        # Return the row for this specific ticker.
        for row in _index_rows(query=ticker, limit=1):
            if row["key"] == ticker:
                return row
        raise RuntimeError(f"row not built for ticker {ticker}")

    raise ValueError(f"unknown source {source!r}")


def rebuild_nautilus_catalog(*, wipe: bool = True, progress_fn=None) -> dict:
    """Wipe and rebuild the Nautilus ParquetDataCatalog from the pandas caches.

    Faz 1 changed the on-disk catalog contract in two ways: bar timestamps now
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
