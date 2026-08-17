"""Motorun SAYILARI için uygulanan bir çıpa.

`regression_baseline.json` bu işi yapıyor GİBİ duruyordu: `capture_baseline.py`
katalogdaki altı spec'i koşup PnL/işlem/sharpe yazıyor ve wiki'deki
"bit-identical parity" iddiası ona dayanıyor. Ama dosyayı OKUYAN hiçbir test,
hiçbir CI adımı yok — ve olamaz da: baseline `datetime.now() - 7 gün`
penceresiyle canlı Bybit'ten çekilmiş (2026-07-23), yani veri penceresi her gün
kayıyor ve o sayılar bir daha asla üretilemez. Yapılandırma gereği
doğrulanamayan bir çıpa, çıpa değildir.

Bu dosya eksik olanı koyuyor: girdisi kendi ürettiği SABİT bir çerçeve olan,
ağsız, cache'siz, deterministik bir sayısal çıpa. Yakaladığı şey backtest
laboratuvarı için asıl önemli olan: pandas/numpy/nautilus yükseltmesinin
Sharpe'ı veya işlem sayısını sessizce oynatması. Zincirin ÇALIŞTIĞINI
`TestBacktestChainOffline` zaten sınıyor; burada sınanan ne ürettiği.

Sayı değişirse test kırılır ve bu DOĞRU davranıştır: değişikliğin bilinçli
olduğunu söyleyen kişi çıpayı da günceller, ve diff'te "motorun cevabı değişti"
yazar. Sessizce kaymasındansa.

Wiki References
---------------
See: [[v1_to_v2_migration_lessons]]
"""

from __future__ import annotations

import hashlib
import sys

import numpy as np
import pandas as pd
import pytest

from composer import ComposedStrategySpec, SignalBlock
from sandbox import run_backtest_guarded

BARS = 3000


def _anchor_frame() -> pd.DataFrame:
    """Sabit tohumlu OHLCV çerçevesi — girdinin kendisi de çıpanın parçası.

    High/low, open/close'un DIŞINDA kalacak şekilde kuruluyor: aksi hâlde
    `_prepare_df` satırların üçte ikisini "OHLC invariant" ihlali diye düşürüyor
    ve çıpa motoru değil, düşürücüyü ölçüyor (ölçüldü: 3000 satırın 974'ü
    kalıyordu).
    """
    rng = np.random.default_rng(20260817)
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
        index=pd.date_range("2026-01-01", periods=BARS, freq="1h", tz="UTC"),
    )


# Çerçevenin kendi parmak izi. Üretici değişirse ÖNCE bu kırılır, yani "motorun
# cevabı değişti" ile "girdiyi değiştirdim" karışmaz.
FRAME_SHA256 = "f26fc7d9"  # ilk 8 hane; aşağıda hesaplanıp karşılaştırılıyor


def _frame_fingerprint(df: pd.DataFrame) -> str:
    payload = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype="float64")
    return hashlib.sha256(payload.tobytes()).hexdigest()[:8]


# Ölçüldü 2026-08-17, nautilus_trader==1.230.0. İki koşuda birebir aynı.
ANCHORS = {
    "ma_cross": {"pnl": 260.6087939, "n_trades": 1, "sharpe": 1.8607466700805135},
    "ma_cross+atr_stop": {
        "pnl": -128.85248699999994,
        "n_trades": 53,
        "sharpe": -1.4907876056435587,
    },
}


def _spec(name: str) -> ComposedStrategySpec:
    entry = SignalBlock(
        type="ma_cross",
        role="entry",
        params={"fast": 10, "slow": 30, "direction": "up"},
    )
    blocks = [entry]
    if name == "ma_cross+atr_stop":
        blocks.append(
            SignalBlock(
                type="atr_stop", role="exit", params={"period": 14, "mult": 2.0}
            )
        )
    return ComposedStrategySpec(
        id=name, name=name, description="", blocks=blocks, trade_size=0.1
    )


def test_the_anchor_frame_has_not_moved():
    """Girdi değişmediyse çıktının değişmesi motorun haberidir."""
    assert _frame_fingerprint(_anchor_frame()) == FRAME_SHA256


# Çıpa DEĞERLERİ Windows'ta ölçüldü. Motorun OS'lar arası bit-birebir aynılığı
# ve `np.random.default_rng` akışının sürümler arası kararlılığı — ikisi de
# doğrulanmadı; NumPy Generator akışı için resmî bir garanti de vermiyor.
# 2026-08-17'de eklenen ubuntu CI ayağı bu dosyayı da koşacak, ve iki
# varsayımdan biri tutmazsa yeni ayak DÜZELTİLEN KODDAN BAĞIMSIZ bir sebeple
# kırmızı yanardı — çıpanın amacı gerileme yakalamak, platform farkını gerileme
# diye sunmak değil.
#
# Bu yüzden değerler ölçüldükleri platforma bağlı, ama motor yolu HER YERDE
# koşuyor: aşağıdaki determinizm ve çerçeve-parmakizi testleri platformdan
# bağımsız ve ubuntu'da da gerçek bir backtest sürüyor. Linux ayağı ilk yeşil
# koşumunu verdiğinde oradaki sayılar da buraya ikinci bir kayıt olarak
# eklenmeli; skip metni bunu söylüyor ve `-ra` her koşumda ekrana basıyor.
_ANCHOR_PLATFORM = "win32"


@pytest.mark.skipif(
    sys.platform != _ANCHOR_PLATFORM,
    reason=(
        f"çıpa değerleri {_ANCHOR_PLATFORM}'da ölçüldü; bu platformun kendi "
        "sayıları henüz kaydedilmedi (motor yolu yine de koşuyor: determinizm "
        "ve çerçeve parmakizi testleri platformdan bağımsız)"
    ),
)
@pytest.mark.parametrize("name", sorted(ANCHORS))
def test_the_engine_still_returns_the_same_numbers(name):
    r = run_backtest_guarded(
        _spec(name),
        _anchor_frame(),
        {"symbol": "BTCUSDT", "category": "linear", "interval": "60"},
    )

    assert r.error is None, r.error
    m = r.metrics or {}
    want = ANCHORS[name]
    assert m.get("n_trades") == want["n_trades"]
    assert m.get("pnl") == pytest.approx(want["pnl"], abs=1e-6)
    assert m.get("sharpe") == pytest.approx(want["sharpe"], rel=1e-9)


def test_two_runs_of_the_same_input_agree():
    """Determinizm iddiasının kendisi. Bir tohum/sıralama sızıntısı bu çıpayı
    "bazen kırılan" bir teste çevirirdi ve o, sayıların kaymasından daha kötü
    olurdu: kimse ona güvenmez."""
    frame = _anchor_frame()
    ctx = {"symbol": "BTCUSDT", "category": "linear", "interval": "60"}

    a = run_backtest_guarded(_spec("ma_cross+atr_stop"), frame, ctx)
    b = run_backtest_guarded(_spec("ma_cross+atr_stop"), frame, ctx)

    assert (a.metrics or {}).get("pnl") == (b.metrics or {}).get("pnl")
    assert (a.metrics or {}).get("sharpe") == (b.metrics or {}).get("sharpe")
