"""Motorun sayıları için YENİDEN ÜRETİLEBİLİR bir çıpa üretir.

Bu betik 2026-08-21'de yeniden yazıldı. Öncesinde bir çıpa gibi duruyor ama
olamıyordu: penceresi ``datetime.now() - 7 gün`` ile canlı borsadan çekiliyordu,
yani veri her gün kayıyor ve yazılan sayılar bir daha asla üretilemiyordu.
Spec'ler de değişebilen bir katalogdan geliyordu — ölçüldü, bu kurulumda
``load_catalog()`` sıfır spec döndürüyor, yani betik boş bir sonuç yazardı.
Yapılandırma gereği doğrulanamayan bir çıpa, çıpa değildir.

Şimdi üç şey de sabit:

* **Çerçeve** kendi üretiliyor — sabit tohum, ağsız, önbeleksiz
  (:func:`anchor_frame`). Girdinin parmak izi de çıpanın parçası.
* **Spec'ler** burada tanımlı (:func:`anchor_specs`), katalogdan değil.
* **Yürütme yolu** tek (:func:`run_anchor`) ve kökene yazılıyor. Bu oturumda
  ölçüldü: aynı spec ve aynı barlar iki yürütme yolunda farklı pnl veriyor,
  dolayısıyla "hangi yol" çıpanın kimliğinin parçası.

Ve asıl eksik kapatıldı: dosyayı **okuyan bir test var** —
``tests/test_regression_baseline_is_an_anchor.py``. O test bu modüldeki AYNI
üç fonksiyonu içeri alıyor; çerçeve/spec/koşucu ikinci bir kopya olarak orada
tanımlanmıyor, çünkü iki kopya kaçınılmaz olarak ayrışır.

Tarihsel kayıt
--------------
Eski dosya ``regression_baseline_2026-07-23_historical.json`` adıyla duruyor.
Silinmedi çünkü bir bulgunun kanıtı: 496 kaydın 479'u ölçülebilir, bunların
466'sı zararda ve **461'i (%99) POZİTİF Sharpe taşıyor** (+0,30 … +71,91).
Bugünkü kod bu deseni üretmiyor — aynı büyüklükte bir kripto penceresinde
zararda olan dört koşunun dördünde de Sharpe negatif çıktı. Yani o dosya,
üretildiği tarihte var olup sonradan düzeltilmiş bir işaret/şişme kusurunun
fotoğrafı (kodda H610 notu ~725× büyüklüğünde böyle bir şişmeyi anlatıyor).
Yeniden üretilemeyen bir taban çizgisi çıpa değil fotoğraftır ve fotoğraf o
günün kusurlarını da kaydeder.

Kullanım
--------
    python capture_baseline.py          # regression_baseline.json'ı yazar

Sayılar kasıtlı olarak değiştiyse betiği yeniden koşan kişi çıpayı da
günceller ve diff'te "motorun cevabı değişti" yazar — sessizce kaymasındansa.

Wiki References
---------------
Bkz: [[v1_to_v2_migration_lessons]], [[yillıklastirma_veriden_okunur_2026_08_21]]
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

OUT_PATH = Path(__file__).parent / "regression_baseline.json"

BARS = 3000
FRAME_SEED = 20260821
FRAME_START = "2026-01-01"
FRAME_FREQ = "1h"

#: Çıpanın yürütme yolu. ``run_backtest_guarded`` yerleşik bloklar için
#: süreç-içi koşar, ağa çıkmaz ve robustluk boru hattının kullandığı yoldur.
EXECUTION_PATH = "sandbox.run_backtest_guarded"

#: Barların bağlamı. Kripto 1-SAAT: yıllıklaştırma tabanı buradan türer ve
#: sentetik bir varsayılana bırakılmaz (bkz. backtest._periods_per_year).
RECIPE = {"symbol": "BTCUSDT", "category": "linear", "interval": "60"}


def anchor_frame() -> pd.DataFrame:
    """Sabit tohumlu OHLCV çerçevesi — girdinin kendisi de çıpanın parçası.

    High/low, open/close'un DIŞINDA kalacak şekilde kuruluyor: aksi hâlde
    ``_prepare_df`` satırların çoğunu "OHLC invariant" ihlali diye düşürüyor ve
    çıpa motoru değil düşürücüyü ölçüyor.
    """
    rng = np.random.default_rng(FRAME_SEED)
    close = 30_000.0 * np.exp(rng.normal(0, 0.0015, BARS).cumsum())
    open_ = np.concatenate([[close[0]], close[:-1]])
    hi_base, lo_base = np.maximum(open_, close), np.minimum(open_, close)
    return pd.DataFrame(
        {
            "open": open_,
            "high": hi_base * (1 + np.abs(rng.normal(0, 0.0008, BARS))),
            "low": lo_base * (1 - np.abs(rng.normal(0, 0.0008, BARS))),
            "close": close,
            "volume": np.full(BARS, 100.0),
        },
        index=pd.date_range(FRAME_START, periods=BARS, freq=FRAME_FREQ, tz="UTC"),
    )


def frame_fingerprint(df: pd.DataFrame) -> str:
    """Çerçevenin parmak izi (sha256'nın ilk 12 hanesi).

    Üretici değişirse ÖNCE bu kırılır, yani "girdiyi değiştirdim" ile "motorun
    cevabı değişti" karışmaz.
    """
    payload = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype="float64")
    return hashlib.sha256(payload.tobytes()).hexdigest()[:12]


#: Çıpa spec'leri — KATALOGDAN DEĞİL, buradan. Katalog değişebilir (ve bu
#: kurulumda boş); çıpanın girdisi değişmemeli. Parametreler çerçeve üzerinde
#: her spec'in gerçekten İŞLEM AÇMASI için seçildi: sıfır ya da tek işlemli bir
#: satır neredeyse hiçbir şeyi çivilemez.
_SPEC_DEFS: dict[str, dict] = {
    "ma_cross": {
        "entry": ("ma_cross", {"fast": 10, "slow": 30, "direction": "up"}),
        "exit": ("ma_cross", {"fast": 10, "slow": 30, "direction": "down"}),
    },
    "ma_cross+atr_stop": {
        "entry": ("ma_cross", {"fast": 10, "slow": 30, "direction": "up"}),
        "exit": ("atr_stop", {"period": 14, "mult": 2.0}),
    },
    "rsi_threshold+ma_cross": {
        "entry": ("rsi_threshold", {"period": 14, "threshold": 30.0, "cross": "below"}),
        "exit": ("ma_cross", {"fast": 10, "slow": 30, "direction": "down"}),
    },
    "bollinger_break": {
        "entry": (
            "bollinger_break",
            {"period": 20, "k": 2.0, "side": "lower", "mode": "legacy"},
        ),
        "exit": (
            "bollinger_break",
            {"period": 20, "k": 2.0, "side": "upper", "mode": "legacy"},
        ),
    },
    "ema_cross": {
        "entry": ("ema_cross", {"fast": 12, "slow": 26, "direction": "up"}),
        "exit": ("ema_cross", {"fast": 12, "slow": 26, "direction": "down"}),
    },
    "price_breakout+atr_stop": {
        "entry": ("price_breakout", {"lookback": 20, "direction": "high"}),
        "exit": ("atr_stop", {"period": 14, "mult": 2.0}),
    },
    "momentum+atr_stop": {
        "entry": ("momentum", {"lookback": 10, "sign": "positive"}),
        "exit": ("atr_stop", {"period": 14, "mult": 2.0}),
    },
}


def validate_spec_defs() -> None:
    """Parametre adlarının/değerlerinin KATALOGDA gerçekten olduğunu doğrula.

    Bu koruma bir kez gerçekten gerekti: çıpa spec'leri elle yazılırken
    `price_breakout` için `direction="up"` (doğrusu "high"), `bollinger_break`
    için `std`/`direction` (doğrusu `k`/`side`/`mode`) ve `momentum` için
    `period`/`threshold` (doğrusu `lookback`/`sign`) yazılmıştı. Sistem
    geçersiz parametreyi SESSİZCE yok sayıp varsayılanı kullanıyor: biri sıfır
    işlem açtı (görüldü), ötekiler işlem açtı ama YAZILANI değil VARSAYILANI
    çiviliyordu — sessiz ve fark edilmesi çok daha zor.
    """
    from composer import BLOCK_CATALOG

    for name, parts in _SPEC_DEFS.items():
        for role, (btype, params) in parts.items():
            if btype not in BLOCK_CATALOG:
                raise ValueError(f"{name}/{role}: katalogda olmayan blok {btype!r}")
            schema = BLOCK_CATALOG[btype]["params"]
            for pname, pval in params.items():
                if pname not in schema:
                    raise ValueError(
                        f"{name}/{role} ({btype}): {pname!r} diye bir parametre yok "
                        f"— geçerliler: {sorted(schema)}"
                    )
                spec = schema[pname]
                if spec["type"] == "enum":
                    if pval not in spec["options"]:
                        raise ValueError(
                            f"{name}/{role} ({btype}.{pname}): {pval!r} geçersiz "
                            f"— seçenekler: {spec['options']}"
                        )
                elif not (spec["min"] <= pval <= spec["max"]):
                    raise ValueError(
                        f"{name}/{role} ({btype}.{pname}): {pval!r} aralık dışı "
                        f"[{spec['min']}, {spec['max']}]"
                    )


def anchor_specs() -> dict[str, object]:
    """Çıpa spec'leri, adlarına göre. Katalogdan bağımsız ve deterministik."""
    from composer import ComposedStrategySpec, SignalBlock

    validate_spec_defs()
    out: dict[str, object] = {}
    for name, parts in _SPEC_DEFS.items():
        blocks = [
            SignalBlock(type=btype, role=role, params=dict(params))
            for role, (btype, params) in parts.items()
        ]
        out[name] = ComposedStrategySpec(
            id=name, name=name, description="", blocks=blocks, trade_size=0.1
        )
    return out


def spec_definitions() -> dict[str, dict]:
    """Spec'lerin OYNAK ALAN İÇERMEYEN tanımı — dosyaya bu yazılır.

    ``ComposedStrategySpec.to_dict()`` her çağrıda ``created_at``'e o anın
    damgasını basıyor; onu dosyaya yazmak her yeniden üretimde anlamsız bir
    diff çıkarırdı (ölçüldü — aynı davranış bir testi de aralıklı kırmıştı).
    """
    return {
        name: {
            role: {"type": btype, "params": dict(params)}
            for role, (btype, params) in parts.items()
        }
        for name, parts in _SPEC_DEFS.items()
    }


def run_anchor(spec, frame: pd.DataFrame):
    """Çıpanın TEK yürütme yolu. Yazan da okuyan da buradan geçer."""
    from sandbox import run_backtest_guarded

    return run_backtest_guarded(spec, frame, dict(RECIPE))


def _num(x):
    """Sayıyı TAM hassasiyetle döndür; NaN/sonsuz -> None.

    Yuvarlanmıyor: 10 haneye yuvarlamak küçük değerlerde çıpanın kendi
    toleransının (rel=1e-9) altına inmiyordu — ölçüldü, sharpe=0,0244685026
    satırı kendi kaydına karşı kırıldı. JSON float'ları Python'da birebir
    gidip geliyor, yani tam hassasiyeti saklamanın maliyeti yok.
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def measure() -> dict:
    """Çıpayı ölç ve dosyaya yazılacak sözlüğü döndür."""
    import nautilus_trader

    frame = anchor_frame()
    results: dict[str, dict] = {}
    for name, spec in anchor_specs().items():
        r = run_anchor(spec, frame)
        if r.error:
            results[name] = {"error": str(r.error)[:300]}
            print(f"  {name:<32} HATA: {str(r.error)[:60]}")
            continue
        m = r.metrics or {}
        results[name] = {
            "pnl": _num(m.get("pnl")),
            "n_trades": int(m.get("n_trades") or 0),
            "sharpe": _num(m.get("sharpe")),
            "sharpe_per_trade": _num(m.get("sharpe_per_trade")),
            "max_dd": _num(m.get("max_dd")),
            "win_rate": _num(m.get("win_rate")),
        }
        e = results[name]

        def _f(v, w, d=4):
            return f"{v:>{w}.{d}f}" if isinstance(v, float) else f"{'—':>{w}}"

        print(
            f"  {name:<32} pnl={_f(e['pnl'], 12)}  n={e['n_trades']:>4}  "
            f"sharpe={_f(e['sharpe'], 10)}"
        )
    return {
        # Köken: bu sayıların hangi koşulda ölçüldüğü. Eksikse çıpa bir sonraki
        # kırılmada "ne değişti" sorusunu cevaplayamaz.
        "provenance": {
            "nautilus_version": nautilus_trader.__version__,
            "python": platform.python_version(),
            "platform": sys.platform,
            "execution_path": EXECUTION_PATH,
            "recipe": dict(RECIPE),
            "frame": {
                "bars": BARS,
                "seed": FRAME_SEED,
                "start": FRAME_START,
                "freq": FRAME_FREQ,
                "fingerprint": frame_fingerprint(frame),
            },
        },
        "specs": spec_definitions(),
        "results": results,
    }


def main() -> None:
    import nautilus_trader

    print(f"nautilus_trader=={nautilus_trader.__version__}")
    print(f"çerçeve: {BARS} bar, tohum {FRAME_SEED}, parmak izi ", end="")
    print(frame_fingerprint(anchor_frame()))
    print(f"yürütme yolu: {EXECUTION_PATH}\n")
    payload = measure()
    OUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n→ {OUT_PATH.name} yazıldı ({len(payload['results'])} spec)")


if __name__ == "__main__":
    main()
