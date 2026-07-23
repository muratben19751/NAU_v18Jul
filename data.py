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
EXTERNAL_CATALOGS: list[Path] = [
    Path(p.strip())
    for p in os.environ.get(
        "NAUTILUS_EXTERNAL_CATALOGS",
        r"E:\myAI_Projects\NAU_ev\backend\data\catalog",
    ).split(os.pathsep)
    if p.strip()
]

# L31: a non-existent root silently turned into an empty panel — warn at module load.
for _ext_root in EXTERNAL_CATALOGS:
    if not _ext_root.exists():
        log.warning("EXTERNAL_CATALOGS root does not exist: %s", _ext_root)

# Cache instrument metadata per external catalog root (read-only, static).
_EXT_INSTRUMENT_META: dict[str, dict] = {}

# Cache the optional _manifest.json per external catalog root (M21).
_EXT_MANIFEST: dict[str, dict] = {}


# ---- Concurrency + atomic write helpers (M3) --------------------------
#
# If two requests (thread or process) do a read-modify-write on the same
# cache file at the same time, the last one wins and the bars in between
# are lost. A two-layer lock:
#   1) in-process: a threading.Lock per key
#   2) inter-process: an O_CREAT|O_EXCL lock file next to the target file
_PROC_LOCKS: dict[str, threading.Lock] = {}
_PROC_LOCKS_GUARD = threading.Lock()


@contextmanager
def _cache_lock(lock_path: Path, timeout: float = 120.0, stale_after: float = 1800.0):
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
                    raise TimeoutError(f"lock timeout: {lock_path}")
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
    """M3a: to_parquet → temp file + os.replace — no half-written parquet remains."""
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
    """Write JSON sidecars atomically too (same pattern as M3a)."""
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
        creationflags=NO_WINDOW_FLAGS,  # Windows: don't open a console window
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
    # Windows: don't open and close a console window on every ticker load.
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, creationflags=NO_WINDOW_FLAGS
    ) as proc:
        try:
            df = pd.read_csv(proc.stdout)
        except pd.errors.EmptyDataError:
            # awk produced no output at all (missing/corrupt/empty gzip, or the
            # header row itself was absent) — pandas can't infer columns. Match
            # the missing-day contract and return an empty typed frame.
            return pd.DataFrame(columns=["ticker", "value", "timestamp"])
        finally:
            proc.stdout.close()
            proc.wait()
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
    existing = pd.DataFrame(columns=empty_cols)
    cached_days: set[date] = set()
    if cache_path.exists():
        try:
            existing = pd.read_parquet(cache_path)
        except Exception as read_err:
            # M3c: corrupt parquet — ignore the cache, days will be re-scanned.
            log.warning("ignoring corrupt index cache (%s): %s", cache_path, read_err)
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
            # M20: mark only PAST days as 'empty' — today (H3) may still be
            # forming and should be retried on the next call.
            # H371: IF THE SOURCE FILE EXISTS and the ticker is missing that
            # day, it's 'scanned-empty' (permanent). But if the file is NOT
            # THERE AT ALL (NFS outage / late-published daily file) don't
            # poison the day — re-scan it later once the file arrives.
            # Otherwise a transient outage permanently deletes past days.
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
        # H3: today's (UTC) bars aren't complete yet — don't write to disk,
        # persist only past days. combined stays complete in the in-memory return.
        persist = combined[combined.index.normalize().date < today_utc]
        _atomic_to_parquet(persist, cache_path)  # M3a: atomic write
    else:
        combined = existing

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
            continue  # known empty range — don't hammer the API again
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

    # M3b: the read-modify-write block is under a per-key lock — concurrent
    # requests for the same (category, symbol, interval) can't clobber each
    # other's new bars (at thread + process level).
    with _cache_lock(cache_path.with_suffix(".lock")):
        cached = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # H626: on force_refresh the existing cache is ALSO READ — formerly it
        # wasn't, and only [start,end) was written while ALL bars OUTSIDE the
        # window were deleted (the /data refresh button with days=7 shrank years
        # of 1m history to 7 days). Now the fresh window is MERGED with the
        # cache (dedup keep='last' → the requested window is refreshed, the rest
        # preserved).
        if cache_path.exists():
            try:
                cached = pd.read_parquet(cache_path)
            except Exception as read_err:
                # M3c: corrupt parquet — ignore the cache, re-fetch the range.
                log.warning(
                    "ignoring corrupt bybit cache (%s): %s", cache_path, read_err
                )
                cached = pd.DataFrame(
                    columns=["open", "high", "low", "close", "volume"]
                )

        new_frames: list[pd.DataFrame] = []
        if force_refresh:
            # UNCONDITIONALLY re-fetch the requested window (refresh stale/half
            # bars); the rest of the cache is preserved via concat+dedup.
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
            # M2: detect and targeted-fill holes inside the cache.
            new_frames += _bybit_gap_frames(cached, cache_path, step_ms, _fetch_segment)
            # H1a: the tail starts from the LAST cached bar (not cached_end_ms +
            # step_ms) — when the last bar was written it may have been a half
            # bar still forming; it's re-fetched and dedup keep='last' replaces
            # it with the fresh one.
            tail_from = cached_end_ms
            if tail_from < end_ms:
                new_frames += _fetch_segment(tail_from, end_ms)

        if new_frames:
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
    ("30", "30m"),
    ("60", "1h"),
    ("240", "4h"),
    ("720", "12h"),
    ("D", "1d"),
)


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
    letter bases; same logic as the suffix-stripping in _instrument_meta.
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
                "text": "size_precision=0 — fractional trade_size is silently truncated to 0; the RiskEngine drops the order.",
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
                print(
                    f"[catalog] delete_data_range ERROR, skipping write → "
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
    """M21: read and cache the OPTIONAL ``_manifest.json`` at the root.

    Returns a symbol → meta dict (NAU_ev output: bars/first/last/ok/…, plus
    'adjusted' if present). If the file is missing or corrupt, an empty dict —
    'adjusted' info stays 'unknown'.
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
        log.warning("could not read external catalog manifest (%s): %s", p, e)
    _EXT_MANIFEST[key] = manifest
    return manifest


def _external_adjusted_flag(root: Path, instrument_id: str) -> bool | None:
    """The 'adjusted' field in the manifest: True/False, None if unknown.

    The manifest is keyed by bare symbol (e.g. 'NVDA', 'BRK.A'), while
    instrument_id is like 'NVDA.NASDAQ' / 'BRK.A.NASDAQ' — only the LAST dot
    (venue) is dropped. M1260: formerly the first dot was split ('BRK.A.NASDAQ'
    → 'BRK') and the UNADJUSTED warning for dotted symbols (BRK.A really is
    adjusted=False!) was lost.
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
                # M21: split/dividend adjustment status from the manifest
                # (True/False, None='unknown' if manifest or field is missing).
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
            # M21: adjustment status from the manifest (None = unknown).
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
    external root itself is never written to). L15: cache freshness is checked
    not by an mtime comparison but by a source signature (an ordered list of
    (name, size, mtime)) — if the signature is NOT EQUAL to the value in the
    sidecar, re-decode.
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
    # L15: source signature — even if a file is deleted/changed (even if mtime
    # goes backwards) the equality breaks and the cache is regenerated.
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
            df = None  # corrupt cache/signature — re-decode

    if df is None:
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        cat = ParquetDataCatalog(str(root))
        bars = cat.bars(bar_types=[bar_dir.name])
        if not bars:
            raise ValueError(f"no bars decoded for {bar_dir.name}")
        # ts_event is bar CLOSE — shift back one interval to OPEN-time index.
        # L15: a SINGLE pass over the bar list (instead of five separate list-comps).
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
