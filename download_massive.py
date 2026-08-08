"""US-equity REST ingest: Massive (Polygon) aggregates → bu projenin equity kataloğu.

`ingest_equities.py`'nin kardeşi. Hedef katalog (`data.EQUITY_CATALOG_DIR`),
right-label sözleşmesi (bar ts = KAPANIŞ), RTH TF resample'ı ve "manifest en
sonda" kuralı birebir aynı — ikisi de aynı fazları koşar, yalnız Faz A'nın
kaynağı değişir:

* `ingest_equities.py` → yerel flat-file arşivi (`E:\\MarketData\\...`, 78 GB,
  UNADJUSTED, tüm ticker'lar günlük CSV'lerde).
* bu modül → Massive REST `/v2/aggs` uçları (arşiv gerekmez, **ADJUSTED**,
  ticker başına çağrı).

Üç pratik fark:

* **Veri ADJUSTED'tır** (`--unadjusted` ile kapatılır): split olan ticker'da
  geçmiş fiyat sıçramaz, manifest'e `adjusted: true` yazılır → /data rozeti
  UNADJUSTED uyarısını göstermez.
* **Plan kotası akışı belirler.** Ücretsiz/başlangıç anahtarlarında dakika-bar
  geçmişi ~2 yıl ve dakikada 5 istek. Kotanın dışındaki yıl HATA değil,
  görünür bir atlamadır: o yıl "plan dışı" diye loglanır, koşum devam eder ve
  manifest yalnız gerçekten inen aralığı yazar (yarım veri sessizce tam
  görünmesin).
* **API anahtarı yalnız ortamdan okunur** (`MASSIVE_API_KEY`, alternatif
  `POLYGON_API_KEY`) — repoya yazılmaz, `Authorization: Bearer` başlığıyla
  gider (URL'lerde/loglarda görünmez).

Kullanım (repo kökü, PYTHONUTF8=1):
  $env:MASSIVE_API_KEY = "..."                     # PowerShell
  python download_massive.py --tickers HOOD,RIVN --years 2025-2026
  python download_massive.py --tickers-file tickers.txt --start 2024-09-01 --rpm 5

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]],
[[index_backtest_via_equity_proxy]], [[instruments]]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import data
from ingest_equities import (
    _MIN_NS,
    _PRICE_PRECISION,
    _equity,
    _log,
    build_tf_bars,
    tickers_in_other_roots,
    write_manifest,
)

API_BASE = "https://api.massive.com"
DEFAULT_RPM = 5  # ücretsiz plan tavanı; ücretli anahtarda --rpm 0 (sınırsız)
_MAX_LIMIT = 50_000  # /v2/aggs sayfa başına tavan


class MassiveError(RuntimeError):
    """Kota/yetki/ağ — çağıranın ayırt etmesi gereken tek hata sınıfı."""


def api_key() -> str:
    key = (
        os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or ""
    ).strip()
    if not key:
        raise MassiveError(
            "MASSIVE_API_KEY yok. PowerShell: $env:MASSIVE_API_KEY = '<anahtar>' "
            "(anahtar https://massive.com/dashboard/keys)"
        )
    return key


class _Pacer:
    """rpm=0 → sınırsız; aksi halde istekler arası en az 60/rpm saniye."""

    def __init__(self, rpm: int) -> None:
        self.gap = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self.gap:
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
            with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
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
                raise MassiveError(
                    f"HTTP {e.code} — anahtar/plan yetkisi: {body[:200]}"
                ) from e
            if e.code >= 500:
                time.sleep(min(30.0, 2**attempt))
                last_err = f"HTTP {e.code}"
                continue
            raise MassiveError(f"HTTP {e.code}: {body[:200]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            time.sleep(min(30.0, 2**attempt))
            last_err = str(e)
    raise MassiveError(
        f"{tries} denemede yanıt alınamadı ({last_err}): {url.split('?')[0]}"
    )


def _year_slices(start: date, end: date) -> list[tuple[date, date]]:
    """Yıl-parçalı istek penceresi — ingest_equities'in yıl döngüsüyle aynı ritim."""
    out = []
    for y in range(start.year, end.year + 1):
        lo = max(start, date(y, 1, 1))
        hi = min(end, date(y, 12, 31))
        if lo <= hi:
            out.append((lo, hi))
    return out


def fetch_minute_aggs(
    ticker: str,
    lo: date,
    hi: date,
    key: str,
    pacer: _Pacer,
    *,
    adjusted: bool = True,
) -> tuple[list[dict], str]:
    """Bir pencerenin ham agg satırları + durum ("ok" | "plan" | "empty").

    Plan penceresi dışı yıllar (`NOT_AUTHORIZED`) hata değil "plan" durumudur:
    çağıran bunu loglar ve sonraki yıla geçer.
    """
    q = urllib.parse.urlencode(
        {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": _MAX_LIMIT}
    )
    url = f"{API_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{lo}/{hi}?{q}"
    rows: list[dict] = []
    while url:
        body = _get(url, key, pacer)
        status = str(body.get("status", "")).upper()
        if status == "NOT_AUTHORIZED":
            return [], "plan"
        if status not in {"OK", "DELAYED"}:
            raise MassiveError(
                f"{ticker} {lo}..{hi}: status={status} {body.get('message', '')}"
            )
        rows.extend(body.get("results") or [])
        url = body.get("next_url") or ""
    return rows, ("ok" if rows else "empty")


def download_minute_bars(
    tickers: set[str],
    start: date,
    end: date,
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
    rpm: int = DEFAULT_RPM,
    adjusted: bool = True,
) -> dict[str, dict]:
    """Faz A — REST'ten 1-MINUTE bar'ları çek ve kataloğa yaz (taze, idempotent).

    Hedeflerin mevcut bar dizinleri baştan silinir; yıl yıl indirilir, her yıl
    yazılıp bırakılır (pik RAM ~1 yıl × 1 ticker).
    """
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    key = api_key()
    pacer = _Pacer(rpm)
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
    slices = _year_slices(start, end)
    t0 = time.time()
    failed: dict[str, str] = {}
    for tk in sorted(tickers):
        bt = BarType.from_str(f"{tk}.{venue}-1-MINUTE-LAST-EXTERNAL")
        st = stats[tk]
        try:
            for lo, hi in slices:
                rows, state = fetch_minute_aggs(
                    tk, lo, hi, key, pacer, adjusted=adjusted
                )
                if state == "plan":
                    _log(
                        f"  {tk} {lo.year}: plan dışı (bu anahtarın geçmiş penceresi dışında)"
                    )
                    continue
                if state == "empty":
                    _log(f"  {tk} {lo.year}: veri yok")
                    continue
                bars = []
                for r in rows:
                    ts = (
                        int(r["t"]) * 1_000_000 + _MIN_NS
                    )  # ms→ns, pencere BAŞI → KAPANIŞ
                    bars.append(
                        Bar(
                            bt,
                            Price(float(r["o"]), _PRICE_PRECISION),
                            Price(float(r["h"]), _PRICE_PRECISION),
                            Price(float(r["l"]), _PRICE_PRECISION),
                            Price(float(r["c"]), _PRICE_PRECISION),
                            Quantity(int(round(float(r.get("v") or 0))), 0),
                            ts,
                            ts,
                        )
                    )
                cat.write_data(bars)
                st["bars"] += len(bars)
                fd = (
                    datetime.fromtimestamp(int(rows[0]["t"]) / 1e3, UTC)
                    .date()
                    .isoformat()
                )
                ld = (
                    datetime.fromtimestamp(int(rows[-1]["t"]) / 1e3, UTC)
                    .date()
                    .isoformat()
                )
                if st["first"] is None or fd < st["first"]:
                    st["first"] = fd
                if st["last"] is None or ld > st["last"]:
                    st["last"] = ld
                _log(
                    f"  {tk} {lo.year}: {len(bars):,} dakika bar ({time.time() - t0:.0f}s)"
                )
        except MassiveError as e:
            # One ticker's failure (rate limit exhaustion, plan boundary hit
            # mid-run, transient 5xx) must not orphan the tickers already
            # written above: without this, the caller's exception aborts
            # download() before write_manifest() runs for ANY ticker, leaving
            # real parquet data on disk with no manifest entry — the same
            # "data present but undiscoverable" failure class
            # download_flatfiles.py's atomic .part→replace avoids.
            failed[tk] = str(e)
            _log(f"  {tk}: HATA, sonraki ticker'a geçiliyor — {e}")
            continue
    if failed:
        _log(
            f"{len(failed)} ticker başarısız oldu (kısmen indirilmiş olabilir, "
            f"manifest'e yalnız bar'ı olanlar girecek): {sorted(failed)}"
        )
    return stats


def download(
    tickers: set[str],
    start: date,
    end: date,
    *,
    venue: str = "NASDAQ",
    catalog_dir: Path | None = None,
    rpm: int = DEFAULT_RPM,
    adjusted: bool = True,
    force: bool = False,
) -> dict[str, dict]:
    """Tam akış: guard → REST minute indir → TF türet → manifest. Stats döner."""
    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    targets = {t.strip().upper() for t in tickers if t.strip()}
    if not force:
        elsewhere = targets & tickers_in_other_roots(cdir)
        if elsewhere:
            _log(
                f"{len(elsewhere)} ticker atlandı — başka external katalogda zaten var "
                f"(--force ile zorla): {sorted(elsewhere)[:12]}"
            )
            targets -= elsewhere
    if not targets:
        _log("hedef ticker kalmadı")
        return {}
    _log(
        f"{len(targets)} ticker indiriliyor ({start}..{end}) venue={venue} · "
        f"adjusted={adjusted} · rpm={rpm or 'sınırsız'} · hedef={cdir}"
    )
    stats = download_minute_bars(
        targets, start, end, venue=venue, catalog_dir=cdir, rpm=rpm, adjusted=adjusted
    )
    with_bars = {tk for tk, s in stats.items() if s["bars"]}
    if not with_bars:
        _log("=== hiçbir ticker'da bar yok — TF/manifest yazılmadı ===")
        return stats
    build_tf_bars(with_bars, venue=venue, catalog_dir=cdir)
    written = write_manifest(
        stats,
        venue=venue,
        catalog_dir=cdir,
        adjusted=adjusted,
        source="massive:rest_aggs",
        note=(
            "Massive REST /v2/aggs ingest"
            + (
                "; yalnız split ayarlı (adjusted=true; temettü ayarlanmaz)."
                if adjusted
                else " (UNADJUSTED)."
            )
        ),
    )
    total = sum(s["bars"] for s in stats.values())
    _log(f"=== {written} ticker · {total:,} minute bar · TF'ler türetildi · {cdir} ===")
    return stats


def _parse_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.start or args.end:
        start = (
            date.fromisoformat(args.start)
            if args.start
            else date.today() - timedelta(days=730)
        )
        end = date.fromisoformat(args.end) if args.end else date.today()
        return start, end
    yr = args.years.split("-")
    y0, y1 = int(yr[0]), int(yr[-1])
    return date(y0, 1, 1), min(date(y1, 12, 31), date.today())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tickers", default="", help="virgüllü liste")
    ap.add_argument(
        "--tickers-file", default="", help="satır-başı-ticker dosyası (# yorum)"
    )
    ap.add_argument("--years", default="2025-2026", help="YYYY veya YYYY-YYYY")
    ap.add_argument("--start", default="", help="YYYY-MM-DD (--years'ı ezer)")
    ap.add_argument("--end", default="", help="YYYY-MM-DD")
    ap.add_argument("--venue", default="NASDAQ", help="Nautilus venue etiketi")
    ap.add_argument(
        "--rpm",
        type=int,
        default=DEFAULT_RPM,
        help="dakikadaki istek tavanı (0=sınırsız)",
    )
    ap.add_argument(
        "--unadjusted", action="store_true", help="ham fiyat (split ayarı yok)"
    )
    ap.add_argument(
        "--force", action="store_true", help="başka external katalogdakini de indir"
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

    start, end = _parse_range(args)
    try:
        download(
            targets,
            start,
            end,
            venue=args.venue,
            rpm=args.rpm,
            adjusted=not args.unadjusted,
            force=args.force,
        )
    except MassiveError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
