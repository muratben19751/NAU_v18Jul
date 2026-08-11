"""Massive (Polygon) grouped-daily → günlük "tüm US hisseleri" CSV'si.

`download_flatfiles.py`'nin ölçtüğü kısıtın pratik cevabı: `us_stocks_sip/`
flat-file'ları bu abonelikte **403 NOT_AUTHORIZED** veriyor, ama aynı günün
aynı içeriği REST'te açık — `/v2/aggs/grouped/...` **tek çağrıda** o günün tüm
US hisselerini (~12,4 bin ticker) döndürüyor. Bu modül o ucu, flat-file
aynasıyla aynı yol ve şema sözleşmesine yazar:

    E:\\MarketData\\massive-flatfiles\\us_stocks_sip\\<dataset>\\YYYY\\MM\\YYYY-MM-DD.csv.gz
    ticker,volume,open,close,high,low,window_start,transactions   (window_start: ns)

Böylece `ingest_equities.py`'nin yol kuralı değişmeden çalışır. Dataset adı
ayarlamayı taşır — karışmasınlar diye ayrı ağaçlar:

* `day_aggs_adjusted_v1` → varsayılan, `adjusted=true` (**yalnız split** ayarlı;
  ölçüm 2026-08-06: NVDA 10:1 öncesi adjusted = unadjusted/10, buna karşılık
  AAPL'ın üç ex-temettü gününde fark tam 0,0000 → temettü ayarlanmıyor, seri
  fiyat getirisidir, toplam getiri değil)
* `day_aggs_v1` → `--unadjusted`, ham fiyat (flat-file arşivinin sözleşmesi)

Ölçülmüş üç davranış (2026-08-05 günü, 12.392 ticker):

* **Tek gün için adjusted ≡ unadjusted.** `adjusted` geçmişi *bugüne* göre
  düzeltir; dün ile bugün arasında split olmadıkça OHLC birebir aynı çıkar
  (ölçüm: 12.392 ticker'ın 0'ında fiyat farkı). Fark çok günlük geçmişte doğar.
* **Hacim gün içinde oturmamış olabilir.** İki çağrı arasında 721 ticker'ın
  `volume`'ü değişti (AAPL 52.071.836 → 52.074.608): geç raporlanan trade'ler.
  Kesin hacim için günü ertesi gün ilerleyen saatlerde çekmek gerekir.
* **Tatil/hafta sonu hata değildir**: `resultsCount=0` gelir, o gün atlanır ve
  koşum devam eder (yarım dosya bırakılmaz).

Yarım dosya bırakmama sözleşmesi `download_flatfiles.py` ile aynı: `.part`'a
yazılır, tamamlanınca `os.replace` ile asıl ada taşınır. Var olan gün
atlanır (`--overwrite` zorlar).

Anahtar yalnız ortamdan okunur (`MASSIVE_API_KEY`, alternatif
`POLYGON_API_KEY`) ve `Authorization: Bearer` başlığıyla gider — URL'de ve
logda görünmez.

Plan ölçümü (2026-08-06, bu anahtar): grouped-daily'de geçmiş **2003-09-10**'a
kadar açık (09-09 boş döner → verinin tabanı, plan sınırı değil) ve 10 ardışık
çağrıda 429 yok (~47/dk). Yani `download_massive.py`'nin dakika-bar için
ölçtüğü "5/dk, ~2 yıl" kısıtı bu uçta geçerli değil: `--rpm 0 --workers 8` ile
tüm geçmiş ~20 dk'da iner. Günler bağımsız olduğu için paralellik güvenli;
`--rpm` verilirse tavan koşum başınadır (pacer paylaşılır), işçi başına değil.

Kullanım (repo kökü, PYTHONUTF8=1):
  $env:MASSIVE_API_KEY = "..."
  python download_grouped_daily.py                          # dün
  python download_grouped_daily.py --date 2026-08-05
  python download_grouped_daily.py --start 2026-07-01 --end 2026-07-31
  python download_grouped_daily.py --days 5 --unadjusted --rpm 0
  python download_grouped_daily.py --all --rpm 0 --workers 8   # 2003→dün

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[index_backtest_via_equity_proxy]],
[[ticker_kimlik_degil_o_gunun_etiketi]]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

API_BASE = "https://api.massive.com"
DEFAULT_DEST = Path(r"E:\MarketData\massive-flatfiles")
DATASET_ROOT = "us_stocks_sip"
DATASET_ADJUSTED = "day_aggs_adjusted_v1"
DATASET_RAW = "day_aggs_v1"
DEFAULT_RPM = 5  # ücretsiz plan tavanı; ücretli anahtarda --rpm 0 (sınırsız)
HISTORY_START = date(2003, 9, 10)  # ölçüm: 09-10 dolu, 09-09 boş → verinin tabanı
_CSV_HEADER = (
    "ticker",
    "volume",
    "open",
    "close",
    "high",
    "low",
    "window_start",
    "transactions",
)


class GroupedDailyError(RuntimeError):
    """Kota/yetki/ağ — çağıranın ayırt etmesi gereken tek hata sınıfı."""


def _log(m: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {m}", flush=True)


def api_key() -> str:
    """Anahtar yalnız ortamdan (bkz. `download_massive.api_key`; oradaki modül
    nautilus_trader çekiyor, bu betik onsuz koşabilsin diye kopya)."""
    key = (
        os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or ""
    ).strip()
    if not key:
        raise GroupedDailyError(
            "MASSIVE_API_KEY yok. PowerShell: $env:MASSIVE_API_KEY = '<anahtar>' "
            "(anahtar https://massive.com/dashboard/keys)"
        )
    return key


class _Pacer:
    """rpm=0 → sınırsız; aksi halde istekler arası en az 60/rpm saniye.

    Kilitli: `--workers` ile çok iş parçacığı aynı pacer'ı paylaşır, tavan
    işçi başına değil koşum başına uygulanır.
    """

    def __init__(self, rpm: int) -> None:
        self.gap = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.gap:
            return
        with self._lock:
            sleep_for = self.gap - (time.monotonic() - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


def _get(url: str, key: str, pacer: _Pacer, *, tries: int = 6) -> dict:
    """GET + JSON. 429/5xx'te geri çekilerek yeniden dener, 401/403'te anlatır."""
    last_err = ""
    for attempt in range(tries):
        pacer.wait()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = (e.read() or b"").decode("utf-8", "replace")
            if e.code == 429:
                wait = float(e.headers.get("Retry-After") or 0) or min(
                    60.0, 5 * 2**attempt
                )
                _log(f"  429 istek limiti — {wait:.0f} s bekleniyor")
                time.sleep(wait)
                last_err = "429"
                continue
            if e.code in (401, 403):
                raise GroupedDailyError(
                    f"HTTP {e.code} — anahtar/plan yetkisi: {body[:200]}"
                ) from e
            if e.code >= 500:
                time.sleep(min(30.0, 2**attempt))
                last_err = f"HTTP {e.code}"
                continue
            raise GroupedDailyError(f"HTTP {e.code}: {body[:200]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            time.sleep(min(30.0, 2**attempt))
            last_err = str(e)
    raise GroupedDailyError(
        f"{tries} denemede yanıt alınamadı ({last_err}): {url.split('?')[0]}"
    )


def target_path(day: date, *, dest: Path = DEFAULT_DEST, adjusted: bool = True) -> Path:
    dataset = DATASET_ADJUSTED if adjusted else DATASET_RAW
    return (
        dest
        / DATASET_ROOT
        / dataset
        / f"{day.year:04d}"
        / f"{day.month:02d}"
        / f"{day.isoformat()}.csv.gz"
    )


def fetch_day(
    day: date, key: str, pacer: _Pacer, *, adjusted: bool = True
) -> list[dict]:
    """O günün tüm US hisse satırları. Tatil/hafta sonu → boş liste (hata değil)."""
    url = (
        f"{API_BASE}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"
        f"?adjusted={str(adjusted).lower()}"
    )
    body = _get(url, key, pacer)
    status = str(body.get("status", "")).upper()
    if status == "NOT_AUTHORIZED":
        raise GroupedDailyError(
            f"{day}: NOT_AUTHORIZED — bu gün anahtarın plan penceresi dışında"
        )
    if status not in {"OK", "DELAYED"}:
        raise GroupedDailyError(f"{day}: status={status} {body.get('message', '')}")
    return body.get("results") or []


def write_day(rows: list[dict], path: Path) -> int:
    """Satırları .part'a yazıp atomik taşı; yazılan bayt döner.

    DeepR 2026-08-09 [ORTA]: "T"/"t" (ticker/timestamp) eksik bir satır
    eskiden sessizce ""/epoch-0 yazılıyordu — geçersiz TİP (ör. "t": "x")
    zaten `int(...)` ile hata veriyordu, ama EKSİK anahtar hiç yakalanmıyordu
    (tutarsız doğrulama sınırı). İkisi de artık aynı şekilde reddediliyor;
    `.part`'ın altındaki `except BaseException` zaten yarım dosya bırakmıyor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    try:
        with gzip.open(part, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_CSV_HEADER)
            for r in sorted(rows, key=lambda x: str(x.get("T", ""))):
                if "T" not in r or "t" not in r:
                    missing = [k for k in ("T", "t") if k not in r]
                    raise GroupedDailyError(
                        f"row missing required field(s) {missing}: {r!r}"
                    )
                w.writerow(
                    [
                        r.get("T", ""),
                        r.get("v", 0),
                        r.get("o", ""),
                        r.get("c", ""),
                        r.get("h", ""),
                        r.get("l", ""),
                        int(r["t"]) * 1_000_000,  # ms → ns (gün başı, ET)
                        r.get("n", ""),
                    ]
                )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, path)
    return path.stat().st_size


def _one_day(
    day: date,
    key: str,
    pacer: _Pacer,
    *,
    dest: Path,
    adjusted: bool,
    overwrite: bool,
) -> tuple[str, int, int]:
    """Tek günün tüm işi: (durum, ticker, bayt). Durum = stats anahtarı."""
    path = target_path(day, dest=dest, adjusted=adjusted)
    if path.exists() and not overwrite:
        return "atlanan", 0, 0
    rows = fetch_day(day, key, pacer, adjusted=adjusted)
    if not rows:
        return "bos", 0, 0
    return "yazilan", len(rows), write_day(rows, path)


_VERBOSE_DAYS = 40  # bu sayıya kadar gün gün loglanır, üstünde ilerleme özeti


def download(
    days: list[date],
    *,
    dest: Path = DEFAULT_DEST,
    adjusted: bool = True,
    rpm: int = DEFAULT_RPM,
    overwrite: bool = False,
    workers: int = 1,
) -> dict[str, int]:
    """Gün listesini indir. {yazilan, atlanan, bos, ticker} döner.

    Günler bağımsız olduğu için `workers > 1` doğrudan hızlandırır; tavan
    `--rpm` ise paylaşılan pacer'da, yani işçi sayısı tavanı delmez.
    """
    key = api_key()
    pacer = _Pacer(rpm)
    stats = {"yazilan": 0, "atlanan": 0, "bos": 0, "ticker": 0}
    dataset = DATASET_ADJUSTED if adjusted else DATASET_RAW
    verbose = len(days) <= _VERBOSE_DAYS
    lock = threading.Lock()
    done = 0
    got = 0
    t0 = time.time()
    _log(
        f"{len(days)} gün · {DATASET_ROOT}/{dataset} · adjusted={adjusted} · "
        f"rpm={rpm or 'sınırsız'} · {workers} işçi · hedef={dest}"
    )

    def _record(day: date, res: tuple[str, int, int]) -> None:
        nonlocal done, got
        state, n, size = res
        with lock:
            stats[state] += 1
            stats["ticker"] += n
            done += 1
            got += size
            if verbose:
                if state == "atlanan":
                    _log(f"  {day}: atlandı (var) — --overwrite ile yenilenir")
                elif state == "bos":
                    _log(f"  {day}: veri yok (tatil/kapalı gün)")
                else:
                    _log(
                        f"  {day}: {n:,} ticker · {size / 1024:.0f} KB → "
                        f"{target_path(day, dest=dest, adjusted=adjusted)}"
                    )
            elif done % 50 == 0 or done == len(days):
                el = time.time() - t0
                left = (len(days) - done) * el / done / 60 if done else 0
                _log(
                    f"  {done:,}/{len(days):,} gün · {got / 1e6:.0f} MB · "
                    f"{stats['ticker']:,} satır · kalan ~{left:.0f} dk"
                )

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(
                    _one_day,
                    d,
                    key,
                    pacer,
                    dest=dest,
                    adjusted=adjusted,
                    overwrite=overwrite,
                ): d
                for d in days
            }
            try:
                for fut in as_completed(futures):
                    _record(futures[fut], fut.result())
            except GroupedDailyError:
                for f in futures:
                    f.cancel()
                raise
    else:
        for d in days:
            _record(
                d,
                _one_day(
                    d, key, pacer, dest=dest, adjusted=adjusted, overwrite=overwrite
                ),
            )

    _log(
        f"=== {stats['yazilan']} gün yazıldı · {stats['atlanan']} atlandı · "
        f"{stats['bos']} boş · {stats['ticker']:,} satır · "
        f"{got / 1e6:.0f} MB · {(time.time() - t0) / 60:.1f} dk ==="
    )
    return stats


def _weekdays(start: date, end: date) -> list[date]:
    """Hafta içi günler (hafta sonu hiç istenmez; tatil ancak yanıttan anlaşılır)."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _parse_days(args: argparse.Namespace) -> list[date]:
    if args.date:
        return [date.fromisoformat(args.date)]
    if getattr(args, "all", False):
        return _weekdays(HISTORY_START, date.today() - timedelta(days=1))
    if args.start or args.end:
        start = date.fromisoformat(args.start) if args.start else date.today()
        end = date.fromisoformat(args.end) if args.end else date.today()
        if end < start:
            raise GroupedDailyError(f"--end ({end}) --start'tan ({start}) önce")
        return _weekdays(start, end)
    end = date.today() - timedelta(days=1)  # varsayılan: dün
    start = end - timedelta(days=max(1, args.days) - 1 + 4)  # hafta sonu payı
    return _weekdays(start, end)[-max(1, args.days) :]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default="", help="tek gün: YYYY-MM-DD")
    ap.add_argument("--start", default="", help="aralık başı: YYYY-MM-DD")
    ap.add_argument("--end", default="", help="aralık sonu: YYYY-MM-DD")
    ap.add_argument(
        "--days",
        type=int,
        default=1,
        help="son N hafta içi gün (dün dahil; --date/--start verilmezse)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help=f"tüm geçmiş: {HISTORY_START} → dün (ölçülen veri tabanı)",
    )
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="yerel arşiv kökü")
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="eşzamanlı gün sayısı (uzun aralıklarda 8; --rpm tavanı korunur)",
    )
    ap.add_argument(
        "--unadjusted", action="store_true", help="ham fiyat → day_aggs_v1 ağacı"
    )
    ap.add_argument(
        "--rpm",
        type=int,
        default=DEFAULT_RPM,
        help="dakikadaki istek tavanı (0=sınırsız)",
    )
    ap.add_argument(
        "--overwrite", action="store_true", help="var olan günü yeniden yaz"
    )
    args = ap.parse_args()

    try:
        days = _parse_days(args)
        if not days:
            _log("HATA: seçilen aralıkta hafta içi gün yok")
            sys.exit(1)
        download(
            days,
            dest=Path(args.dest),
            adjusted=not args.unadjusted,
            rpm=args.rpm,
            overwrite=args.overwrite,
            workers=max(1, args.workers),
        )
    except GroupedDailyError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
