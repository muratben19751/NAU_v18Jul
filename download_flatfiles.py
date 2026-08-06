"""Massive (Polygon) flat-file S3 aynası → yerel arşiv kökü.

`ingest_equities.py`'nin OKUDUĞU arşivi kuran adım. O modül
`E:\\MarketData\\massive-flatfiles\\<dataset>\\...` altındaki günlük
`.csv.gz`'leri yerelde varsayar; bu modül o ağacı S3'ten indirir. Anahtar
düzen kararı: **S3 key yolu neyse yerel yol da odur** (`us_indices/
minute_aggs_v1/2026/07/2026-07-31.csv.gz`), böylece dataset eklendikçe
ingest tarafında yol kuralı değişmez.

Ölçülmüş kısıtlar (2026-08-03, bu hesap):

* **Tek akış yavaş, paralel hızlı** — bir dosya 0,9 MB/s inerken 12 eşzamanlı
  indirme toplamda ~12,4 MB/s veriyor (bağlantı tavanı). Bu yüzden varsayılan
  `--workers 16`; işçi sayısını artırmak tavanı yükseltmiyor, yalnız parçalıyor.
* **Her dataset açık değil.** Aynı kimlik bilgileri `us_indices/` için 200,
  `us_stocks_sip/` (ve crypto/forex/futures/options) için **403
  `NOT_AUTHORIZED`** döndürüyor: listeleme aboneliğe bağlı değil, indirme
  bağlı. 403 bu yüzden "ağ hatası" gibi yeniden denenmez — koşum o dataset
  için hemen ve açık bir mesajla durur.

Yarım dosya bırakmama sözleşmesi: her indirme `.part` uzantısına yazılır ve
ancak tamamlandığında asıl ada taşınır (`os.replace`, atomik). Yeniden koşum
boyutu S3'teki ile aynı olan dosyaları atlar — 119 GB'lık bir aynanın
kesintiden sonra baştan inmemesi bu iki kuralın birlikte çalışmasına bağlı.

Kullanım (repo kökü, PYTHONUTF8=1):
  $env:MASSIVE_S3_KEY = "..."; $env:MASSIVE_S3_SECRET = "..."
  python download_flatfiles.py --dataset us_indices/minute_aggs_v1
  python download_flatfiles.py --dataset us_indices/day_aggs_v1 --years 2024-2026

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[index_backtest_via_equity_proxy]],
[[bar_aggregation_and_type_syntax]]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_ENDPOINT = "https://files.massive.com"
DEFAULT_BUCKET = "flatfiles"
DEFAULT_DEST = Path(r"E:\MarketData\massive-flatfiles")
DEFAULT_WORKERS = 16  # ölçülen tavan ~12,4 MB/s; fazlası bölüyor, hızlandırmıyor
_AUTH_RETRIES = 4  # koşum ortasındaki 403 throttle'dır; ısrar ederse yetkidir


class FlatFileError(RuntimeError):
    """Yetki/yapılandırma — yeniden denemenin çözmeyeceği hata sınıfı."""


def _log(m: str) -> None:
    from datetime import UTC, datetime

    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {m}", flush=True)


def make_client(endpoint: str = DEFAULT_ENDPOINT, workers: int = DEFAULT_WORKERS):
    import boto3
    from botocore.config import Config

    key = os.environ.get("MASSIVE_S3_KEY", "").strip()
    secret = os.environ.get("MASSIVE_S3_SECRET", "").strip()
    if not (key and secret):
        raise FlatFileError(
            "MASSIVE_S3_KEY / MASSIVE_S3_SECRET yok. PowerShell: "
            "$env:MASSIVE_S3_KEY = '<access key id>'; $env:MASSIVE_S3_SECRET = '<secret>' "
            "(https://massive.com/dashboard — Flat Files bölümü)"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            max_pool_connections=max(workers * 2, 16),
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def _key_day(key: str) -> str:
    """Dosya adının başındaki YYYY-MM-DD (yoksa boş) — gün bazlı süzgecin dayanağı."""
    name = key.rsplit("/", 1)[-1]
    stem = name[:10]
    return stem if len(stem) == 10 and stem[4] == stem[7] == "-" else ""


def list_dataset(
    client,
    dataset: str,
    *,
    bucket: str = DEFAULT_BUCKET,
    years: range | None = None,
    start: str = "",
    end: str = "",
) -> list[tuple[str, int]]:
    """(key, size) listesi.

    `years` yol içindeki /YYYY/ ile, `start`/`end` (YYYY-MM-DD) ise dosya
    adındaki günle süzer — "son 24 ay" gibi yuvarlak pencereler takvim yılına
    yuvarlanmasın diye. Günü okunamayan key gün süzgecinde elenir.
    """
    out: list[tuple[str, int]] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{dataset.strip('/')}/"
    ):
        for o in page.get("Contents", []):
            key = o["Key"]
            if years is not None:
                parts = key.split("/")
                if (
                    len(parts) < 4
                    or not parts[-3].isdigit()
                    or int(parts[-3]) not in years
                ):
                    continue
            if start or end:
                day = _key_day(key)
                if not day or (start and day < start) or (end and day > end):
                    continue
            out.append((key, o["Size"]))
    return sorted(out)


def _http_code(e: Exception) -> int | None:
    return getattr(e, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")


def _fetch_one(client, bucket: str, key: str, size: int, dest: Path) -> int:
    """Tek nesne → dest/key. Zaten tam inmişse 0 döner (atlandı).

    401/403 iki farklı şey olabildiği için körlemesine ölümcül sayılmaz:
    dataset aboneliğe kapalıysa `mirror` ön-yoklamada zaten durdurur, koşum
    ortasındaki 403 ise geçicidir. Ölçüm (2026-08-06): 16 işçiyle 35 MB/s'de
    5.000. dosyada 403 alındı, aynı key hemen ardından 26/26 başarılı indi —
    yani throttle. Bu yüzden yetki hatası da geri çekilerek yeniden denenir,
    ancak ısrar ederse ölümcül sayılır.
    """
    target = dest / key
    if target.exists() and target.stat().st_size == size:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(_AUTH_RETRIES):
        try:
            client.download_file(bucket, key, str(part))
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            code = _http_code(e)
            if code in (401, 403):
                if attempt + 1 < _AUTH_RETRIES:
                    time.sleep(min(30.0, 3 * 2**attempt))
                    continue
                raise FlatFileError(
                    f"HTTP {code} — {_AUTH_RETRIES} denemede de indirilemedi "
                    f"({key}). Dataset aboneliğe kapalı olabilir; listeleme "
                    "açık olsa da indirme aboneliğe bağlı."
                ) from e
            raise
        os.replace(part, target)
        return size
    raise AssertionError("ulaşılamaz")  # döngü ya döner ya yükseltir


def _preflight(client, bucket: str, key: str) -> None:
    """Tek baytlık GET: dataset aboneliğe kapalıysa 5.000 dosya sonra değil,
    daha ilk saniyede ve açık mesajla dur."""
    try:
        client.get_object(Bucket=bucket, Key=key, Range="bytes=0-0")
    except Exception as e:  # noqa: BLE001
        if _http_code(e) in (401, 403):
            raise FlatFileError(
                f"HTTP {_http_code(e)} — bu dataset bu abonelikte indirilemiyor "
                f"({key}). Listeleme açık olsa da indirme aboneliğe bağlı."
            ) from e
        raise


def mirror(
    client,
    objects: list[tuple[str, int]],
    dest: Path,
    *,
    bucket: str = DEFAULT_BUCKET,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, int]:
    """Paralel ayna. {indirilen, atlanan, bayt} döner; ilerleme loglanır."""
    _preflight(client, bucket, objects[0][0])
    total_bytes = sum(s for _, s in objects)
    done = skipped = 0
    got = 0
    lock = threading.Lock()
    t0 = time.time()
    _log(
        f"{len(objects):,} dosya · {total_bytes / 1e9:.2f} GB · {workers} işçi · hedef={dest}"
    )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_one, client, bucket, k, s, dest): (k, s)
            for k, s in objects
        }
        try:
            for fut in as_completed(futures):
                n = fut.result()
                with lock:
                    if n:
                        done += 1
                        got += n
                    else:
                        skipped += 1
                    seen = done + skipped
                    if seen % 25 == 0 or seen == len(objects):
                        el = time.time() - t0
                        rate = got / 1e6 / el if el else 0
                        left = (total_bytes - got) / 1e6 / rate / 60 if rate else 0
                        _log(
                            f"  {seen:,}/{len(objects):,} · {got / 1e9:.1f} GB · "
                            f"{rate:.1f} MB/s · kalan ~{left:.0f} dk"
                        )
        except FlatFileError:
            for f in futures:
                f.cancel()
            raise
    _log(
        f"=== {done:,} indi · {skipped:,} atlandı (zaten tam) · {got / 1e9:.2f} GB · "
        f"{(time.time() - t0) / 60:.1f} dk ==="
    )
    return {"downloaded": done, "skipped": skipped, "bytes": got}


def _parse_years(spec: str) -> range | None:
    if not spec:
        return None
    p = spec.split("-")
    return range(int(p[0]), int(p[-1]) + 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, help="ör. us_indices/minute_aggs_v1")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="yerel arşiv kökü")
    ap.add_argument(
        "--years", default="", help="YYYY veya YYYY-YYYY (varsayılan: hepsi)"
    )
    ap.add_argument("--start", default="", help="gün süzgeci başı: YYYY-MM-DD")
    ap.add_argument("--end", default="", help="gün süzgeci sonu: YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument(
        "--dry-run", action="store_true", help="yalnız listele ve boyut bildir"
    )
    args = ap.parse_args()

    try:
        client = make_client(args.endpoint, args.workers)
        objs = list_dataset(
            client,
            args.dataset,
            bucket=args.bucket,
            years=_parse_years(args.years),
            start=args.start,
            end=args.end,
        )
        if not objs:
            _log(f"HATA: {args.dataset} altında nesne yok")
            sys.exit(1)
        if args.dry_run:
            size = sum(s for _, s in objs)
            _log(
                f"{len(objs):,} dosya · {size / 1e9:.2f} GB · ilk={objs[0][0]} son={objs[-1][0]}"
            )
            return
        mirror(
            client,
            objs,
            Path(args.dest),
            bucket=args.bucket,
            workers=args.workers,
        )
    except FlatFileError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
