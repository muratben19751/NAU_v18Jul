"""One-shot: run every spec in strategy_catalog.json on current nautilus_trader,
write PnL/trades/sharpe/positions_report sha256 to regression_baseline.json.

Used as pre-migration parity anchor. See plan v1→v2 migration Phase 0.

BU BİR TARİHSEL KAYIT, UYGULANAN BİR ÇIPA DEĞİL — ve olamaz. Aşağıdaki
pencere ``datetime.now() - 7 gün``: veri her gün kayıyor, dolayısıyla
2026-07-23'te yazılan sayılar bir daha asla üretilemez. Hiçbir test ve hiçbir
CI adımı ``regression_baseline.json``'ı okumaz; okusaydı ilk koşuda kırılırdı.
Wiki'deki "bit-identical parity" iddiası bu dosyaya dayanıyor ve o iddia
migrasyon anında ölçülmüş bir gözlemdir, süregelen bir garanti değil.

ÖLÇÜLDÜ 2026-08-21 — bu dosya üç ayrı sebeple yeniden hesaplanmadı:

1. **Yıllıklaştırma düzeltmesinden ETKİLENMEDİ.** Betik BTCUSDT 1-DAKİKA
   barlarıyla koşuyor; sentetik Bybit 1-DAKİKA varsayılanı onun için zaten
   doğruydu. Yeni kod da aynı tabanı veriyor (1 dk aralık + hafta sonu barı →
   kripto → 525.600). Sharpe'ın 45× şiştiği kusur GÜNLÜK HİSSE verisine
   özgüydü.
2. **Bu kurulumda katalog BOŞ.** `load_catalog()` 0 spec döndürüyor; dosyadaki
   altı "Random ..." spec'i artık mevcut değil. Betik bugün koşsa boş bir
   sonuç yazardı.
3. **Pencere göreli** (aşağıda): bugün koşmak başka bir veri aralığının
   sayılarını üretir, 2026-07-23'teki kayıtla kıyaslanamaz.

Ve dosyadaki sayılar bugünkü kodun davranışını YANSITMIYOR: altı kaydın altısı
da negatif PnL ile POZİTİF Sharpe (+3,5…+4,6) taşıyor. Bugünkü kodla aynı
büyüklükte bir kripto penceresinde ölçüldü — zararda olan dört koşunun
dördünde de Sharpe NEGATİF çıktı (-0,06 / -23,6 / -10,7 / -22,5). Yani kayıt,
o tarihte var olup sonradan düzeltilmiş bir işaret/şişme kusurunun da
fotoğrafını taşıyor (kodda H610 notu ~725× büyüklüğünde böyle bir şişmeyi
anlatıyor).

Uygulanan çıpa ``tests/test_engine_numeric_anchor.py``'de: sabit tohumlu,
ağsız, cache'siz bir çerçeve üzerinde tam PnL/işlem/Sharpe. Motorun cevabı bir
pandas/numpy/nautilus yükseltmesiyle kayarsa CI orada kırılır. Bu betiği
uygulanabilir hâle getirmek isteyen, önce aşağıdaki pencereyi SABİT bir tarih
aralığına çevirmek zorunda.

Wiki References
---------------
Bkz: [[v1_to_v2_migration_lessons]]

Pre-migration parity anchor — the script that produces the "6 catalog spec bit-identical parity" claim on the [[v1_to_v2_migration_lessons]] page.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from datetime import UTC, datetime, timedelta

import nautilus_trader

from backtest import run_composed_backtest
from composer import load_catalog
from data import load_bybit_bars

OUT_PATH = Path(__file__).parent / "regression_baseline.json"


def main() -> None:
    print(f"nautilus_trader=={nautilus_trader.__version__}")
    catalog = load_catalog()
    print(f"catalog: {len(catalog)} specs")

    bars = load_bybit_bars(
        "BTCUSDT",
        interval="1",
        start=datetime.now(UTC) - timedelta(days=7),
        end=datetime.now(UTC),
    )
    print(f"BTCUSDT bars: {len(bars)}")

    results: dict[str, dict] = {}
    for spec in catalog:
        key = f"{spec.name}__{spec.id}"
        print(f"\n[{spec.id}] {spec.name}")
        try:
            r = run_composed_backtest(spec, bars, iteration_id=0, rationale="baseline")
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            results[key] = {"error": f"{type(e).__name__}: {e}"}
            continue

        m = r.metrics

        def _metric(*names, default=None):
            for name in names:
                value = m.get(name)
                if value is not None:
                    return value
            return default

        def _fin(x, ndigits=6):
            return (
                None
                if x is None or (isinstance(x, float) and math.isnan(x))
                else round(float(x), ndigits)
            )

        entry = {
            "id": spec.id,
            "pnl": _fin(_metric("pnl", "pnl_pct"), 2),
            "n_trades": _metric("n_trades", "trade_count", default=0),
            "sharpe": _fin(_metric("sharpe", "sharpe_nautilus")),
            "win_rate": _fin(_metric("win_rate", "winrate")),
            "max_dd": _fin(_metric("max_dd", "drawdown")),
            "equity_last": _fin(r.equity_curve[-1], 2) if r.equity_curve else None,
        }
        results[key] = entry
        _pnl = f"{entry['pnl']:.2f}" if entry["pnl"] is not None else "nan"
        _s = f"{entry['sharpe']:.4f}" if entry["sharpe"] is not None else "nan"
        print(f"  pnl={_pnl} trades={entry['n_trades']} sharpe={_s}")

    payload = {
        "nautilus_version": nautilus_trader.__version__,
        "results": results,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
