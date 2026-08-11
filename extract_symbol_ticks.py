"""Flat-file tick arşivinden sembol bazlı tick arşivi (trades / quotes).

Neden gerekli: flat file'lar **gün bazlı ve tüm evreni** taşır — 2026-08-04'ün
trades dosyası 3,8 GB ve 160 milyon satır. Tek bir sembolün tick'ine bakmak
için her seferinde o dağı taramak anlamsız; bu modül dağı bir kez tarayıp
sembol başına ayrı ağaç yazar:

    E:\\MarketData\\by_symbol\\<SEMBOL>\\<dataset>\\YYYY\\MM\\YYYY-MM-DD.csv.gz

Ölçülmüş kısıtlar (2026-08-06, 2026-08-04 trades dosyası):

* **Darboğaz gzip çözme, ayrıştırma değil.** Saf Python `csv` 0,7 M satır/s,
  pyarrow 1,7 M satır/s (95 s/dosya). Bu yüzden pyarrow kullanılır ve
  paralellik dosya BAŞINA verilir (`--workers`): tek dosyayı bölmek mümkün
  değil, ama 6 dosyayı aynı anda çözmek doğrusal hızlanma verir.
* **Bir tarama, tüm semboller.** Dosya zaten açıldığı için sembol sayısını
  artırmak neredeyse bedava; 7 sembol tek geçişte çıkar (6,65 M satır).

Yarım dosya bırakmama sözleşmesi `download_flatfiles.py` ile aynı: `.part`'a
yazılır, tamamlanınca `os.replace`. Var olan gün atlanır (`--overwrite`
zorlar), böylece kesilen koşum baştan başlamaz.

Kullanım (repo kökü, PYTHONUTF8=1):
  python extract_symbol_ticks.py --dataset trades_v1 --symbols SPY,QQQ,AAPL
  python extract_symbol_ticks.py --dataset quotes_v1 --symbols SPY --workers 6 \
      --start 2026-01-01 --end 2026-08-05

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[ticker_kimlik_degil_o_gunun_etiketi]]
"""

from __future__ import annotations

import argparse
import glob
import gzip
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

SRC_ROOT = Path(r"E:\MarketData\massive-flatfiles\us_stocks_sip")
OUT_ROOT = Path(r"E:\MarketData\by_symbol")
DEFAULT_WORKERS = 6  # gzip çözme CPU'ya bağlı; çekirdek sayısının altında tut
_BLOCK_SIZE = 64 << 20  # pyarrow okuma bloğu (test küçültür)


class ExtractError(RuntimeError):
    """Kaynak/yapılandırma — yeniden denemenin çözmeyeceği hata sınıfı."""


def _log(m: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {m}", flush=True)


def out_path(symbol: str, dataset: str, day: str, out_root: Path | None = None) -> Path:
    root = out_root or OUT_ROOT
    return root / symbol / dataset / day[:4] / day[5:7] / f"{day}.csv.gz"


def _day_of(path: str) -> str:
    return os.path.basename(path)[:10]


def extract_day(
    src: str,
    dataset: str,
    symbols: tuple[str, ...],
    overwrite: bool,
    out_root: Path | None = None,
) -> dict:
    """Bir gün dosyasını tara, her sembol için ayrı .csv.gz yaz.

    Dönen sözlük: {sembol: satır}. Zaten yazılmış günler için dosya
    açılmaz — kesilen koşumun ikinci turu neredeyse bedava olsun.

    `out_root` açıkça geçirilir çünkü bu fonksiyon ayrı bir SÜREÇTE koşar:
    modül sabitini okumak alt süreçte ana sürecin yapılandırmasını görmez
    (testte geçici kök yerine gerçek arşive bakıp "zaten var" der).
    """
    import pyarrow as pa
    from pyarrow import compute as pc
    from pyarrow import csv as pacsv

    day = _day_of(src)
    targets = {s: out_path(s, dataset, day, out_root) for s in symbols}
    if not overwrite and all(p.exists() for p in targets.values()):
        return dict.fromkeys(symbols, -1)  # -1 = atlandı

    want = pa.array(symbols)
    parts: dict[str, list] = {s: [] for s in symbols}
    # HER SÜTUN METİN. pyarrow tipi blok blok tahmin ediyor: quotes'ta
    # `indicators` ilk bloklarda boş olduğu için sayı sanılıyor, ilerideki
    # "12,2" satırında koşum çöküyordu. Süzüp aynen geri yazdığımız için tipe
    # zaten ihtiyaç yok; metin hem çökmez hem daha hızlı.
    with gzip.open(src, "rt", encoding="utf-8") as hf:
        header = hf.readline().rstrip("\n").split(",")
    as_text = pacsv.ConvertOptions(column_types=dict.fromkeys(header, pa.string()))
    with gzip.open(src, "rb") as fh:
        reader = pacsv.open_csv(
            fh,
            read_options=pacsv.ReadOptions(block_size=_BLOCK_SIZE),
            convert_options=as_text,
        )
        for batch in reader:
            t = pa.Table.from_batches([batch])
            sel = t.filter(pc.is_in(t.column("ticker"), value_set=want))
            if sel.num_rows:
                for s in symbols:
                    one = sel.filter(pc.equal(sel.column("ticker"), s))
                    if one.num_rows:
                        parts[s].append(one)

    counts: dict[str, int] = {}
    for s, chunks in parts.items():
        counts[s] = sum(c.num_rows for c in chunks)
        if not chunks:
            continue
        p = targets[s]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        try:
            table = pa.concat_tables(chunks)
            with pa.CompressedOutputStream(str(tmp), "gzip") as sink:
                pacsv.write_csv(table, sink)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, p)
    return counts


def _job(args: tuple) -> tuple[str, dict]:
    src, dataset, symbols, overwrite, out_root = args
    return _day_of(src), extract_day(src, dataset, symbols, overwrite, out_root)


def run(
    dataset: str,
    symbols: tuple[str, ...],
    *,
    start: str = "",
    end: str = "",
    workers: int = DEFAULT_WORKERS,
    overwrite: bool = False,
) -> dict[str, int]:
    src_dir = SRC_ROOT / dataset
    files = sorted(glob.glob(str(src_dir / "*" / "*" / "*.csv.gz")))
    if start or end:
        files = [
            f
            for f in files
            if (not start or _day_of(f) >= start) and (not end or _day_of(f) <= end)
        ]
    if not files:
        raise ExtractError(
            f"{src_dir} altında gün dosyası yok (aralık: {start or '-'}..{end or '-'})"
        )

    total = {s: 0 for s in symbols}
    failed: dict[str, str] = {}
    skipped = 0
    t0 = time.time()
    _log(
        f"{len(files)} gün · {dataset} · {len(symbols)} sembol · {workers} işçi → {OUT_ROOT}"
    )
    jobs = [(f, dataset, symbols, overwrite, OUT_ROOT) for f in files]
    done = 0

    def _record(counts: dict) -> None:
        nonlocal done, skipped
        done += 1
        if all(v == -1 for v in counts.values()):
            skipped += 1
        else:
            for s, n in counts.items():
                total[s] += max(n, 0)
        if done % 10 == 0 or done == len(files):
            el = time.time() - t0
            left = (len(files) - done) * el / done / 60
            _log(
                f"  {done}/{len(files)} gün · {sum(total.values()):,} satır · kalan ~{left:.0f} dk"
            )

    # Tek bozuk gün 4 saatlik koşumu öldürmesin: hata o güne yazılır, koşum
    # sürer, sonda topluca bildirilir (ve çıkış kodu 2 olur). Atlanan gün
    # zaten yeniden koşulabilir — `.part` bırakılmadığı için kalıntı yok.
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_job, j): _day_of(j[0]) for j in jobs}
            for fut in as_completed(futs):
                try:
                    _record(fut.result()[1])
                except Exception as e:  # noqa: BLE001
                    failed[futs[fut]] = f"{type(e).__name__}: {e}"
                    _record(dict.fromkeys(symbols, 0))
    else:
        for j in jobs:  # tek işçide süreç havuzu kurma — aynı süreçte koş
            try:
                _record(_job(j)[1])
            except Exception as e:  # noqa: BLE001
                failed[_day_of(j[0])] = f"{type(e).__name__}: {e}"
                _record(dict.fromkeys(symbols, 0))
    _log(
        f"=== {len(files) - skipped - len(failed)} gün işlendi · {skipped} atlandı · "
        f"{len(failed)} HATA · {(time.time() - t0) / 60:.1f} dk ==="
    )
    for s in symbols:
        _log(f"    {s:6} {total[s]:>14,} satır")
    for day, msg in sorted(failed.items()):
        _log(f"    HATA {day}: {msg[:160]}")
    if failed:
        raise ExtractError(f"{len(failed)} gün işlenemedi (yukarıda listelendi)")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, help="trades_v1 | quotes_v1")
    ap.add_argument("--symbols", required=True, help="virgüllü liste")
    ap.add_argument("--start", default="", help="YYYY-MM-DD")
    ap.add_argument("--end", default="", help="YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    syms = tuple(
        sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    )
    if not syms:
        _log("HATA: sembol yok")
        sys.exit(1)
    try:
        run(
            args.dataset,
            syms,
            start=args.start,
            end=args.end,
            workers=max(1, args.workers),
            overwrite=args.overwrite,
        )
    except ExtractError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
