"""Ticker değiştirmiş bir enstrümanın parçalarını tek sürekli seriye diker.

Ticker kimlik değil, o günün etiketidir: aynı fon/şirket yıllar içinde ad
değiştirebilir (QQQ→QQQQ→QQQ, FB→META). Parçalar ayrı enstrüman olarak
katalogda durduğunda her backtest'te elle birleştirmek gerekir ve unutulduğunda
sessizce yanlış sonuç verir — QQQ'da 2004-12..2011-03 deliği, 2008 ayı
piyasasının tamamını gizleyip al-tut maksimum düşüşünü %53,5 yerine %35,6
gösteriyordu.

Bu modül parçaları **1-MINUTE** düzeyinde birleştirip yeni bir enstrüman
(`--target`) olarak kataloğa yazar, sonra `ingest_equities.build_tf_bars` ile
TF'leri türetir ve manifest'i yazar — yani sonuç, doğrudan indirilmiş bir
enstrümandan ayırt edilemez.

**Dikiş denetlenir, varsayılmaz.** İki parça da bugüne göre split-ayarlı
olduğu için seviyelerin devam etmesi beklenir, ama beklenti ölçüm değildir:
dikişin iki yanındaki günlük hareket serinin kendi medyan hareketiyle
karşılaştırılır. `--max-seam-ratio` katını aşarsa koşum durur (parçalardan biri
farklı ayarlanmış ya da yanlış eşleştirilmiş demektir). Ölçüm (2026-08-07,
QQQ+QQQQ): dikişler +%2,04 ve +%0,54, medyan günlük %0,66 → oran 3,1 ve 0,8.

Kullanım (repo kökü, PYTHONUTF8=1):
  python stitch_ticker.py --target QQQC --parts QQQ,QQQQ
  python stitch_ticker.py --target METAC --parts META,FB --max-seam-ratio 8

Wiki References
---------------
Bkz: [[ticker_kimlik_degil_o_gunun_etiketi]], [[parquet_data_catalog]]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import data
from ingest_equities import _equity, _log, build_tf_bars, write_manifest

DEFAULT_VENUE = "NASDAQ"
DEFAULT_MAX_SEAM_RATIO = 6.0  # dikiş, medyan günlük hareketin bu katını aşamaz
_YEAR_CHUNK = 1  # bar'lar yıl yıl taşınır: pik RAM ~1 yıl × 1 parça


class StitchError(RuntimeError):
    """Uyumsuz parça/dikiş — sessizce devam edilmemesi gereken hata sınıfı."""


def _bar_type(symbol: str, venue: str, spec: str = "1-MINUTE-LAST-EXTERNAL") -> str:
    return f"{symbol}.{venue}-{spec}"


def load_daily_closes(symbol: str, venue: str = DEFAULT_VENUE):
    """Dikiş denetimi için günlük kapanışlar (pandas Series, ts indeksli)."""
    import pandas as pd

    df = data.load_external_bars(f"{symbol}.{venue}", "1-DAY")
    if df.empty:
        raise StitchError(f"{symbol}: katalogda 1-DAY bar yok")
    return pd.Series(df["close"].astype(float).values, index=df.index)


def ownership(parts: list[str], venue: str) -> list[tuple]:
    """Birleşik takvimin her günü için (gün, kapanış, hangi parça).

    Yazma sırasıyla AYNI kural: parçalar ilk tarihlerine göre dizilir, bir günü
    ilk sahiplenen parça o günü tutar. Dikişler bu diziden çıkarılır.
    """
    series = {p: load_daily_closes(p, venue) for p in parts}
    order = sorted(parts, key=lambda p: series[p].index[0])
    owner: dict = {}
    for p in order:
        for ts, close in series[p].items():
            if ts not in owner:
                owner[ts] = (float(close), p)
    return [(ts, *owner[ts]) for ts in sorted(owner)]


def check_seams(parts: list[str], venue: str, max_ratio: float) -> list[dict]:
    """Sahiplik dizisindeki HER geçişi ölçer — parça sayısı kadar değil.

    Önceki sürüm parçaları ikili dizip parça-1 kadar dikiş sayıyordu; bu, bir
    parçanın aralığı diğerini KAPSADIĞINDA yanlıştı: QQQ 2003→2026 uzanıp
    ortasında delik bıraktığı için QQQ→QQQQ dikişi ölçülüyor, 2011'deki
    QQQQ→QQQ dönüşü hiç görülmüyordu (ve aralıklar "örtüşüyor" diye yanlış
    alarm veriyordu). Sahiplik dizisinde kaynak her değiştiğinde bir dikiş
    vardır — kapsayan aralıklarda iki uç da böylece ölçülür.

    Dönen liste her dikiş için {from, to, at, move, median, ratio}; `ratio`
    `max_ratio`'yu aşarsa StitchError.
    """
    import statistics

    rows = ownership(parts, venue)
    # Ölçek PARÇA İÇİ hareketlerden gelir: dikişler medyana katılırsa kendi
    # eşiklerini yükseltip muhafızı etkisizleştirir (az parçalı seride iki
    # dikiş medyanı %38'e çıkarıp %104'lük sıçramayı "2,7× normal" gösterdi).
    intra = [
        abs(c1 / c0 - 1)
        for (_, c0, p0), (_, c1, p1) in zip(rows, rows[1:])
        if p0 == p1 and c0
    ]
    if not intra:
        raise StitchError("parça içi hareket yok — ölçek hesaplanamıyor")
    median_move = float(statistics.median(intra))

    claimed = {p: 0 for p in parts}
    for _, _, p in rows:
        claimed[p] += 1
    shadowed = {
        p: len(load_daily_closes(p, venue)) - claimed[p]
        for p in parts
        if len(load_daily_closes(p, venue)) != claimed[p]
    }
    for p, n in shadowed.items():
        _log(f"  {p}: {n} gün başka parçada da var, öncelikli parça korundu")

    seams: list[dict] = []
    for (ts0, c0, p0), (ts1, c1, p1) in zip(rows, rows[1:]):
        if p0 == p1:
            continue
        move = c1 / c0 - 1
        ratio = abs(move) / median_move if median_move else float("inf")
        seams.append(
            {
                "from": p0,
                "to": p1,
                "at": str(ts1.date()),
                "move": move,
                "median": median_move,
                "ratio": ratio,
            }
        )
        _log(
            f"  dikiş {p0}→{p1} @ {seams[-1]['at']}: {move * 100:+.2f}% "
            f"(medyan günlük {median_move * 100:.2f}% · oran {ratio:.1f}×)"
        )
    if not seams:
        _log(
            "  uyarı: hiç dikiş yok — parçalar bitişik değil ya da tek parça sahipleniyor"
        )
    bad = [s for s in seams if s["ratio"] > max_ratio]
    if bad:
        raise StitchError(
            f"{len(bad)} dikiş fazla sıçruyor (>{max_ratio}× medyan): "
            + ", ".join(f"{s['from']}→{s['to']} {s['move'] * 100:+.1f}%" for s in bad)
            + ". Parçalar farklı ayarlanmış olabilir; --max-seam-ratio ile "
            "bilinçli olarak gevşetilebilir."
        )
    return seams


def stitch(
    target: str,
    parts: list[str],
    *,
    venue: str = DEFAULT_VENUE,
    catalog_dir: Path | None = None,
    max_seam_ratio: float = DEFAULT_MAX_SEAM_RATIO,
) -> dict:
    """Parçaların 1-MINUTE bar'larını `target` altında birleştir, TF+manifest yaz."""
    import shutil

    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    cdir = catalog_dir or data.EQUITY_CATALOG_DIR
    if target in parts:
        raise StitchError(f"hedef ({target}) parçalardan biri olamaz")
    _log(f"{target} ← {' + '.join(parts)} (venue={venue}, katalog={cdir})")

    seams = check_seams(parts, venue, max_seam_ratio)

    cat = ParquetDataCatalog(str(cdir))
    for d in (cdir / "data" / "bar").glob(f"{target}.{venue}-*"):
        shutil.rmtree(d, ignore_errors=True)  # taze koşum, idempotent
    cat.write_data([_equity(target, venue)])
    bt = BarType.from_str(_bar_type(target, venue))

    total = 0
    first_ts: int | None = None
    last_ts: int | None = None
    seen: set[int] = set()
    for part in sorted(parts, key=lambda p: load_daily_closes(p, venue).index[0]):
        src = _bar_type(part, venue)
        # Yıl aralığı GÜNLÜK seriden gelir: ucuz ve ARADAKİ yılları da kapsar.
        # (1-MINUTE'ü sınır bulmak için sorgulamak hem 3 M bar'ı belleğe alır
        # hem de yalnız ilk/son yılı verip aradaki 20 yılı sessizce atlardı.)
        idx = load_daily_closes(part, venue).index
        for year in range(idx[0].year, idx[-1].year + 1):
            lo = int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1e9)
            hi = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp() * 1e9)
            bars = cat.query(data_cls=Bar, identifiers=[src], start=lo, end=hi - 1)
            fresh = []
            for b in bars:
                if b.ts_event in seen:  # örtüşen gün: ilk parça korunur
                    continue
                seen.add(b.ts_event)
                fresh.append(
                    Bar(
                        bt,
                        b.open,
                        b.high,
                        b.low,
                        b.close,
                        b.volume,
                        b.ts_event,
                        b.ts_init,
                    )
                )
            if not fresh:
                continue
            cat.write_data(fresh)
            total += len(fresh)
            first_ts = min(first_ts or fresh[0].ts_event, fresh[0].ts_event)
            last_ts = max(last_ts or fresh[-1].ts_event, fresh[-1].ts_event)
            _log(f"  {part} {year}: {len(fresh):,} bar → {target}")

    if not total:
        raise StitchError("parçalardan hiç bar okunamadı")

    build_tf_bars({target}, venue=venue, catalog_dir=cdir)
    stats = {
        target: {
            "bars": total,
            "first": datetime.fromtimestamp(first_ts / 1e9, UTC).date().isoformat(),
            "last": datetime.fromtimestamp(last_ts / 1e9, UTC).date().isoformat(),
        }
    }
    write_manifest(
        stats,
        venue=venue,
        catalog_dir=cdir,
        # Parçalar bugüne göre SPLIT-ayarlı geliyor (dikiş denetimi bunu
        # varsaymıyor, ölçüyor); temettü hiçbir parçada ayarlanmadığı için
        # dikilmiş seri de temettü-ayarsızdır.
        split_adjusted=True,
        dividend_adjusted=False,
        source="stitch:" + "+".join(parts),
        note=(
            f"{' + '.join(parts)} sürekli seri olarak dikildi; dikişler: "
            + "; ".join(
                f"{s['at']} {s['move'] * 100:+.2f}% ({s['ratio']:.1f}× medyan)"
                for s in seams
            )
            + ". Yalnız split ayarlı (temettü ayarlanmaz)."
        ),
    )
    _log(f"=== {target}: {total:,} minute bar · TF'ler türetildi · {cdir} ===")
    return {"bars": total, "seams": seams}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", required=True, help="yeni sürekli enstrüman, ör. QQQC")
    ap.add_argument(
        "--parts", required=True, help="virgüllü parça listesi, ör. QQQ,QQQQ"
    )
    ap.add_argument("--venue", default=DEFAULT_VENUE)
    ap.add_argument(
        "--max-seam-ratio",
        type=float,
        default=DEFAULT_MAX_SEAM_RATIO,
        help="dikişin medyan günlük harekete oranı için tavan",
    )
    args = ap.parse_args()

    parts = [p.strip().upper() for p in args.parts.split(",") if p.strip()]
    if len(parts) < 2:
        _log("HATA: en az iki parça gerekli")
        sys.exit(1)
    try:
        stitch(
            args.target.strip().upper(),
            parts,
            venue=args.venue,
            max_seam_ratio=args.max_seam_ratio,
        )
    except StitchError as e:
        _log(f"HATA: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
