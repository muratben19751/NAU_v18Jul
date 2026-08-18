"""Ekrandaki WFO oranı, kararı veren oran olmalı.

AUTO koşusu 8aa18365 (2026-08-18), tur 3: bir aday dört ölçüt de yeşil
görünürken elendi.

    ❌ Failed — IS/OOS: ✓ Robust · WFO: 37/60 · MC median DD: -21.5% ·
                Multi-symbol: ✓ Generalizable

Artefakt açılıp sayıldığında sebep çıktı: satır KÂRLI pencereleri sayıyordu
(37/60 = %62, çıtayı geçer), kapı ise AL-TUT'U GEÇEN pencereleri
(28/60 = %47, çıtanın altında). Aynı listeden, aynı adla, iki farklı ölçüm —
ve 22 yıllık boğada sistematik ıraksıyorlar.

Aynı turdaki İKİNCİ aday da aynı sebeple düştü (alfa 0/2, ekranda 1/2) ve o da
görünmüyordu; yani 2/2.

Bu depoda ilke zaten yazılıydı — `_holdout_promotion_verdict`: "gerekçe metni
boolean ile TAM OLARAK aynı bayraklardan türetilmelidir, yoksa ikisi ıraksar".

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[nau_holdout_dogrulama_turu_2026_08_18]]
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from auto.robustness import WFO_MIN_TRADES, wfo_verdict

ROOT = Path(__file__).resolve().parents[1]


def _window(*, n_trades: int, pnl: float, excess: float | None, sharpe: float = 0.5):
    metrics = {"n_trades": n_trades, "pnl": pnl, "sharpe": sharpe}
    if excess is not None:
        metrics["excess_return_fraction"] = excess
    return {"test_metrics_naive": metrics, "test_n_trades": n_trades}


def _basket(n_alpha: int, n_profit_only: int = 0, n_loss: int = 0):
    """Alfalı / sadece kârlı / zararlı pencerelerden bir sepet."""
    out = [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=100.0, excess=0.05)
        for _ in range(n_alpha)
    ]
    out += [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=100.0, excess=-0.05)
        for _ in range(n_profit_only)
    ]
    out += [
        _window(n_trades=WFO_MIN_TRADES + 1, pnl=-100.0, excess=-0.20)
        for _ in range(n_loss)
    ]
    return out


def test_the_measured_case_now_fails_visibly():
    """Ölçülen vaka: 37 kârlı / 28 alfalı / 60 geçerli → ret, gerekçesiyle."""
    v = wfo_verdict(_basket(n_alpha=28, n_profit_only=9, n_loss=23))
    assert v.valid == 60
    assert v.pnl_positive == 37, "vaka yeniden üretilemedi"
    assert v.alpha_positive == 28
    assert v.alpha_ratio == pytest.approx(28 / 60)
    assert not v.ok
    assert "28/60" in v.reason and "<50%" in v.reason


def test_the_displayed_ratio_is_the_deciding_ratio():
    """Asıl düzeltme bu: ekrandaki sayı kararın sayısı."""
    v = wfo_verdict(_basket(n_alpha=28, n_profit_only=9, n_loss=23))
    assert v.display == "28/60"
    assert v.display != f"{v.pnl_positive}/{v.valid}"


def test_a_profitable_but_alphaless_basket_does_not_pass():
    """Boğada kâr etmek yeterli değil — hepsi kârlı, hiçbiri al-tut'u geçmiyor."""
    v = wfo_verdict(_basket(n_alpha=0, n_profit_only=10))
    assert v.pnl_positive == 10 and v.alpha_positive == 0
    assert not v.ok and v.display == "0/10"


def test_enough_alpha_passes():
    v = wfo_verdict(_basket(n_alpha=6, n_profit_only=0, n_loss=4))
    assert v.ok and v.reason == "passed" and v.display == "6/10"


def test_missing_excess_is_fail_closed():
    """Ölçülemeyen alfa olumlu sayılmaz — arşiv/slim satırlar kapıyı açmasın."""
    windows = _basket(n_alpha=9, n_profit_only=0)
    windows.append(_window(n_trades=WFO_MIN_TRADES + 1, pnl=10.0, excess=None))
    v = wfo_verdict(windows)
    assert not v.ok and "excess" in v.reason


def test_a_nonpositive_penalized_sharpe_still_blocks():
    """Alfa yeterliyken bile kararsız bir seri geçmemeli."""
    v = wfo_verdict(_basket(n_alpha=8, n_profit_only=0, n_loss=2), penalized=-0.3)
    assert not v.ok and "Sharpe" in v.reason
    # ...ve pozitifken engellemez.
    assert wfo_verdict(_basket(n_alpha=8, n_loss=2), penalized=0.3).ok


def test_too_few_trades_is_not_measured_rather_than_failed():
    """Ölçülemeyen ile başarısız ayrı: biri atlanır, diğeri düşürür."""
    thin = [_window(n_trades=WFO_MIN_TRADES - 1, pnl=1.0, excess=0.1)]
    v = wfo_verdict(thin)
    assert not v.measured and v.display == "—"
    assert str(WFO_MIN_TRADES) in v.reason
    assert not wfo_verdict([]).measured


def test_all_three_consumers_read_one_source():
    """Üç yerde üç ayrı sayım, tam da ıraksamanın sebebiydi."""
    import auto.robustness as ar
    import web.routes.agent_backtest as ab

    suite = inspect.getsource(ar.run_full_robustness)
    assert "wfo_verdict(" in suite, "adım satırı kararı okumuyor"

    gate = inspect.getsource(ab._robustness_tally)
    assert "_wfo_verdict(" in gate, "kapı yardımcıyı kullanmıyor"
    assert "positive_ratio" not in gate, "kapıda ikinci bir sayım kalmış"

    scan = inspect.getsource(ab._scan_one_candidate)
    assert "_wfo_verdict(" in scan and ".display" in scan
    assert 'get("pnl", 0) > 0' not in scan, "elenme satırı hâlâ PnL sayıyor"


def test_the_rejection_reason_reaches_the_operator():
    """Sessiz `failed += 1` dört yeşil ölçüt ve açıklamasız bir ❌ üretiyordu."""
    import web.routes.agent_backtest as ab

    gate = inspect.getsource(ab._robustness_tally)
    assert re.search(r"✗ Walk-Forward: \{_wf\.reason\}", gate), (
        "WFO reddi operatöre yazılmıyor"
    )


def test_the_profitable_count_survives_as_a_diagnostic():
    """Kârlılık sayısı karar vermiyor ama kayboluyor da değil.

    İkisi arasındaki fark, "boğayı mı taşıyor yoksa alfa mı üretiyor"
    sorusunun doğrudan cevabı — atmak teşhis kaybı olurdu.
    """
    import auto.robustness as ar

    suite = inspect.getsource(ar.run_full_robustness)
    assert "merely profitable" in suite
    v = wfo_verdict(_basket(n_alpha=2, n_profit_only=6, n_loss=2))
    assert v.pnl_positive == 8 and v.alpha_positive == 2
