"""Safely repair one adjusted Massive intraday session in an existing catalog.

Unlike ``download_massive.py`` this utility never clears a ticker.  It fetches
one calendar day, rebuilds that day's RTH bars for the 1-MINUTE source AND
every derived timeframe, and atomically replaces the affected Parquet files.
A ``.bak`` sibling is retained until the operator has verified the repair.

The 1-MINUTE repair matters even if you only care about a derived TF: any
later ``ingest_equities.build_tf_bars()`` run regenerates every TF from
scratch from the 1-MINUTE catalog, so a TF-only fix would be silently undone
by the next routine ingest.

"Hangi bar o güne ait?" sorusu UTC takviminden DEĞİL New York seansından
yanıtlanır: damga kapanıştadır (right-label), yani bir seansın 1-DAY barı
"seans + 1 gün 00:00 NY" ile damgalanır ve o günün UTC gününün dışına düşer.
Sınırları `ingest_equities.session_label_bounds_ns` verir — sözleşme tek bir
yerde dursun diye buradan kopyalanmaz, import edilir.

Wiki References
---------------
Bkz: [[us_equity_katalog_veri_butunlugu]], [[bar_aggregation_and_type_syntax]],
[[parquet_data_catalog]]
"""

from __future__ import annotations

import argparse
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import data
from download_massive import _Pacer, api_key, fetch_minute_aggs
from ingest_equities import (
    RTH_END,
    RTH_START,
    TFS,
    TZ,
    _sanitize_ohlc,
    resample_tf,
    session_label_bounds_ns,
)

# Nautilus'un sabit-nokta gösterimi: raw = round(value, precision) × 1e9.
# Ölçek DAİMA 1e9'dur; `precision` ölçeği değil, ÖNCE uygulanan YUVARLAMAYI
# belirler (Price(v, 2) → 2 basamağa yuvarla, sonra 1e9 ile ölçekle).
_FIXED_PRECISION = 9
_FIXED_SCALE = 10**_FIXED_PRECISION
_FIXED_INT64_MAX = 2**63 - 1
_FIXED_INT64_MIN = -(2**63)


def _fixed(values: list[float], precision: int) -> list[bytes]:
    """`values` → Nautilus sabit-nokta (int64, little-endian) bayt dizisi.

    DeepR 2026-08-11 [ORTA]: `precision` parametresi gövdede BİR KEZ BİLE
    okunmuyordu — çağrı yerleri "fiyat 2 basamak, hacim tam sayı" izlenimi
    veriyordu ama her iki değer de ham hâliyle 1e9 ile ölçekleniyordu. Ölü
    parametre değil, YANLIŞ parametreydi: `Quantity(175.4, 0)` Nautilus'ta
    175_000_000_000 raw üretirken bu fonksiyon 175_400_000_000 yazıyordu, yani
    onarılan gün katalogdaki komşularından FARKLI bir sözleşmeyle kodlanıyordu
    (modülün tüm varlık sebebi bunun tersi). Fiyat hassasiyeti bu projede
    birinci sınıf bir kavram — `backtest.price_precision_for` /
    `price_scale_hint` de tam olarak bu yüzden var; parametreyi silmek değil,
    KULLANMAK doğru olan.

    Yuvarlama Nautilus'un Rust tarafıyla aynı: YARIM SIFIRDAN UZAĞA. Python'un
    yerleşik `round()`'u banker's rounding yapar ve 10.125 fiyatını 10.12 diye
    kodlardı — Nautilus 10.13 yazar.
    """
    if not 0 <= precision <= _FIXED_PRECISION:
        raise ValueError(
            f"precision {precision} out of range 0..{_FIXED_PRECISION} "
            "(Nautilus fixed-point has 9 decimals)"
        )
    unit_scale = 10**precision
    tail_scale = 10 ** (_FIXED_PRECISION - precision)
    out: list[bytes] = []
    for i, value in enumerate(values):
        # DeepR 2026-08-09 [ORTA]: NaN/inf/overflow used to reach
        # int(round(...)).to_bytes() unvalidated and crash with an opaque
        # ValueError/OverflowError that doesn't say which value or row
        # broke it — a bad Massive response (missing field parsed as NaN,
        # a garbled huge number) turned into a stack trace, not an
        # actionable message.
        if not math.isfinite(value):
            raise ValueError(
                f"non-finite price/volume at row {i}: {value!r} "
                "(bad data from the API response, refusing to encode it)"
            )
        units = value * unit_scale
        rounded = math.floor(units + 0.5) if units >= 0 else math.ceil(units - 0.5)
        scaled = int(rounded) * tail_scale
        if not (_FIXED_INT64_MIN <= scaled <= _FIXED_INT64_MAX):
            raise ValueError(
                f"value at row {i} ({value!r}) scaled to {scaled} does not "
                "fit in a signed 8-byte fixed-point field"
            )
        out.append(scaled.to_bytes(8, "little", signed=True))
    return out


def _bar_table(df: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    ts = [int(value) for value in df["ts"].astype("int64")]
    return pa.Table.from_arrays(
        [
            pa.array(_fixed(df[name].tolist(), 2), type=schema.field(name).type)
            for name in ("open", "high", "low", "close")
        ]
        + [
            pa.array(
                _fixed(df["volume"].tolist(), 0), type=schema.field("volume").type
            ),
            pa.array(ts, type=schema.field("ts_event").type),
            pa.array(ts, type=schema.field("ts_init").type),
        ],
        schema=schema,
    )


def _replace_day(
    path: Path, replacement: pa.Table, day: date, *, dry_run: bool
) -> tuple[int, int]:
    # Okuma handle'ı DETERMİNİSTİK kapanmalı: aşağıdaki `path.replace(backup)`
    # Windows'ta, dosya hâlâ açıkken `PermissionError [WinError 32]` verir ve
    # onarım tam da yedeği alırken yarıda kalır. `pq.read_table(path)` handle'ı
    # GC'ye bırakıyordu — süite içinde sıra değişince arada bir gerçekten
    # patlıyordu. `with` bloğu okumayı bitirir bitirmez kapatır.
    with pq.ParquetFile(path) as _pf:
        old = _pf.read()
    # DeepR 2026-08-10 [KRİTİK]: silinecek pencere UTC takviminden değil, NEW
    # YORK SEANSINDAN türetilir. Damga kapanıştadır (right-label), bu yüzden
    # bir seansın 1-DAY barı "seans + 1 gün 00:00 New York" (DST'ye göre
    # 04:00/05:00 UTC) ile damgalanır — onarılan günün UTC gününün DIŞINDA.
    # Eski naif [gün 00:00 UTC, gün+1 00:00 UTC) filtresi bu yüzden yanlış barı
    # seçiyordu: KOMŞU (bir önceki) seansın günlük barını siliyor, onarılan
    # günün barını ise yerinde bırakıp üstüne yenisini ekleyerek İKİZLİYORDU —
    # yani onarım hem komşu günü kataloğdan düşürüyor hem de onarılan güne
    # eski (bozuk) değeri taşıyan ikinci bir bar bırakıyordu.
    # Sınırlar ingest_equities'ten TEK yerden gelir: damgalama sözleşmesi orada
    # tanımlı, iki taraf ıraksarsa onarılan gün komşularından farklı yazılır.
    start, end = session_label_bounds_ns(day)
    # pyarrow>=?: ChunkedArray (what Table column indexing returns) does not
    # support Python's `|` operator — must use the pc.or_() function form
    # (2026-08-09 DeepR #95 follow-up: this broke EVERY real (non-dry-run)
    # invocation of repair_day, TF-only repair included, not just the
    # 1-MINUTE addition below).
    keep = old.filter(
        pa.compute.or_(
            pa.compute.less(old["ts_event"], start),
            pa.compute.greater_equal(old["ts_event"], end),
        )
    )
    merged = pa.concat_tables([keep, replacement]).sort_by([("ts_event", "ascending")])
    if dry_run:
        return old.num_rows, merged.num_rows
    backup = path.with_suffix(path.suffix + ".bak")
    # DeepR 2026-08-09 [ORTA]: the old `if not backup.exists()` guard only
    # ever captured the state before the FIRST repair — a second repair (a
    # follow-up fix, or a mistaken re-run) overwrote path without refreshing
    # .bak, so the rollback point silently went stale: undoing the most
    # recent repair actually undid every repair back to the original.
    # Always snapshot the CURRENT on-disk state right before writing, so
    # .bak means "before the last repair," matching what "rollback" implies.
    path.replace(backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(merged, tmp, compression="zstd")
    tmp.replace(path)
    return old.num_rows, merged.num_rows


def _find_parquet(bar_dir: Path) -> Path:
    """The single *.parquet file expected under a Nautilus catalog bar-type
    directory. Raises a clear, actionable error instead of letting an empty
    glob's next() surface as an opaque StopIteration (2026-08-09 DeepR
    [DÜŞÜK]) — this fires whenever the ticker/timeframe was never ingested
    into the catalog in the first place, a plausible operator mistake."""
    try:
        return next(bar_dir.glob("*.parquet"))
    except StopIteration:
        raise FileNotFoundError(
            f"no *.parquet under {bar_dir} — has this ticker/timeframe been "
            "ingested into the catalog yet?"
        ) from None


def repair_day(
    ticker: str,
    day: date,
    catalog: Path,
    *,
    dry_run: bool = False,
    expected_minutes: int = 390,
) -> dict[str, tuple[int, int]]:
    rows, status = fetch_minute_aggs(
        ticker, day, day, api_key(), _Pacer(0), adjusted=True
    )
    if status != "ok" or not rows:
        raise RuntimeError(
            f"Massive returned {status!r}; repair aborted without writes"
        )
    frame = pd.DataFrame(rows).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    # .as_unit("ns") pins nanosecond resolution explicitly: pandas 3.x's
    # pd.to_datetime(..., unit="ms") now returns datetime64[ms] by default,
    # and every downstream `.view("int64")` in this module assumes
    # nanoseconds (matching _replace_day()'s pd.Timestamp(...).value
    # boundaries, always ns) — without this, ts_event/ts_init would be
    # written 1e6x too small, and _replace_day()'s old/new-day filter would
    # silently misclassify every row (2026-08-09 DeepR #95 follow-up).
    frame["dt"] = pd.to_datetime(frame["t"], unit="ms", utc=True).dt.as_unit(
        "ns"
    ) + pd.Timedelta(minutes=1)
    frame = frame.set_index("dt").sort_index()
    # Onarım da ingest ile AYNI kuralları kullanmalı, yoksa onarılan gün
    # katalogdaki komşularından farklı bir sözleşmeyle yazılır: RTH sınırı
    # right-label'a göredir (09:31..16:00, pre-market sızmaz) ve bozuk OHLC
    # burada da süzülür.
    frame, _ = _sanitize_ohlc(frame, label=f"{ticker} {day}: ")
    rth = frame.tz_convert(TZ).between_time(RTH_START, RTH_END)
    # DeepR 2026-08-09 [ORTA]: the hardcoded 390 assumed every session is a
    # full 6.5h RTH day — a half day (day-after-Thanksgiving, some July 3rds/
    # Christmas Eves, closing at 13:00 ET = 210 min) was unrepairable, same
    # error as a genuinely bad/partial Massive response. Deliberately NOT
    # relaxed to a blanket lower default: that would blunt the guard's actual
    # job (catching a truncated fetch). An operator who KNOWS a given day is
    # a half day passes --expected-minutes explicitly instead.
    if len(rth) < expected_minutes:
        raise RuntimeError(
            f"only {len(rth)} RTH minute bars returned (expected "
            f"{expected_minutes}); refusing partial repair. If {day} is a "
            "known half day, pass --expected-minutes explicitly."
        )
    root = catalog / "data" / "bar"
    changed: dict[str, tuple[int, int]] = {}

    # Repair the 1-MINUTE source bars FIRST. ingest_equities.build_tf_bars()
    # derives every other timeframe fresh from these on each run (rmtree +
    # resample-from-scratch) — a TF-only repair is silently undone the next
    # time Phase B runs, since it never looks at the TF files, only the
    # 1-MINUTE catalog (2026-08-09 DeepR finding).
    minute_rth = rth.copy()
    minute_rth["ts"] = minute_rth.index.tz_convert("UTC").view("int64")
    minute_path = _find_parquet(root / f"{ticker}.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    minute_schema = pq.read_schema(minute_path)
    changed["1-MINUTE"] = _replace_day(
        minute_path, _bar_table(minute_rth, minute_schema), day, dry_run=dry_run
    )

    for tf, (_rule, spec) in TFS.items():
        # resample_tf: 4-HOUR seans-göreli gruplanır (mutlak 240 dk kovaları
        # DST'de kırılıyordu), diğer TF'ler pandas resample'da kalır.
        agg = resample_tf(
            rth,
            tf,
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            },
        ).dropna(subset=["close"])
        agg["ts"] = agg.index.tz_convert("UTC").view("int64")
        path = _find_parquet(root / f"{ticker}.NASDAQ-{spec}-LAST-EXTERNAL")
        schema = pq.read_schema(path)
        changed[spec] = _replace_day(
            path, _bar_table(agg, schema), day, dry_run=dry_run
        )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--catalog", type=Path, default=data.EQUITY_CATALOG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--expected-minutes",
        type=int,
        default=390,
        help="RTH minutes expected for --date (default 390 = full 6.5h session; "
        "pass a lower value for a known half day, e.g. 210 for a 13:00 ET close)",
    )
    args = parser.parse_args()
    result = repair_day(
        args.ticker.upper(),
        args.date,
        args.catalog,
        dry_run=args.dry_run,
        expected_minutes=args.expected_minutes,
    )
    for spec, (before, after) in result.items():
        print(f"{spec}: {before} -> {after}")


if __name__ == "__main__":
    main()
