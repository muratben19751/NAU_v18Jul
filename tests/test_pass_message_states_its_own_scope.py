"""Geçiş cümlesi, kararın KAPSAMINI da söylemeli.

AUTO koşusu 9016d12a (2026-08-18), tur 1: bir aday şu satırla kazanan ilan
edildi —

    ✅ ALL TESTS PASSED! IS/OOS: ✓ Robust · WFO: — · Multi-symbol: — (yetersiz veri)

Aynı satırın içinde iki TİRE var: Walk-Forward'ın tek bir geçerli penceresi
olmamış, çok-sembol testinde beş akranın beşi de az işlemden sayılmamış. Yani
dört ölçütün ikisi HİÇ KOŞMADI ve cümle "hepsi geçti" dedi.

Kural dürüsttü (gevşek modda `evaluated >= 2`), yanlış olan cümleydi: "geçti"
ile "koşulamadı" aynı kovaya girince kanıtın genişliği tam da EN DAR olduğu
anda gizleniyor.

Zarar oluşmadı çünkü mühürlü kapı yayımı reddetti — ama reddeden şey İKİNCİ bir
kapıydı, bu satırın kendisi değil.

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[kesilme_ve_degrade_gorunurlugu]]
"""

from __future__ import annotations

import inspect

import web.routes.agent_backtest as ab


def _rob(*, wfo=True, ms=True, split=True, mc=True) -> dict:
    """Dört ölçütü tek tek açıp kapatabilen bir robustluk payload'ı."""
    out: dict = {}
    if split:
        out["split"] = {"overfitting_label": "✓ Robust"}
    if wfo:
        out["wfo_windows"] = [
            {
                "test_metrics_naive": {
                    "n_trades": 20,
                    "pnl": 100.0,
                    "sharpe": 0.5,
                    "excess_return_fraction": 0.05,
                    "annualized_alpha": 0.05,
                },
                "test_n_trades": 20,
            }
            # WFO payda sınırı 10; sepet onun üstünde olmalı ki bu
            # test WFO'yu değil kapsam mesajını ölçsün.
            for _ in range(12)
        ]
    if ms:
        out["multi_symbol"] = {"generalization_label": "✓ Generalizable"}
    if mc:
        out["mc"] = {"max_dd_p50": -10.0, "max_dd_p95": -20.0}
    return out


def test_the_tally_counts_what_actually_ran():
    """Ölçülen vaka: WFO ve çok-sembol yok → 2/4 değerlendirildi, yine de geçer."""
    t = ab._robustness_tally(_rob(wfo=False, ms=False), strict=False)
    assert t.ok is True
    assert t.evaluated == 2 and t.required == 2 and t.failed == 0


def test_a_full_evaluation_is_still_four():
    t = ab._robustness_tally(_rob(), strict=False)
    assert t.ok is True and t.evaluated == 4


def test_the_bool_wrapper_keeps_its_historical_contract():
    """Testler ve çağıranlar `is True` / `is False` bekliyor — bozulmasın."""
    assert ab._robustness_passed(_rob(), strict=False) is True
    assert ab._robustness_passed({}, strict=False) is False
    assert ab._robustness_passed({"error": "boom"}, strict=False) is False


def test_a_narrow_pass_no_longer_claims_all_tests_passed():
    """Cümle, kaç ölçütün koşabildiğini SÖYLEMELİ."""
    src = inspect.getsource(ab._scan_one_candidate)
    assert "ALL TESTS PASSED" not in src, "iddialı cümle hâlâ orada"
    assert "ALL 4 CRITERIA PASSED" in src
    assert "PASSED on {tally.evaluated}/4 criteria" in src


def test_the_sentence_reads_the_deciding_counter():
    """Sayı ikinci kez hesaplanmamalı — bugünkü ıraksama deseninin ta kendisi."""
    src = inspect.getsource(ab._scan_one_candidate)
    assert "_robustness_tally(" in src, "karar ve kapsam ayrı çağrılardan geliyor"
    assert "tally.evaluated" in src and "tally.required" in src
    assert "passed = tally.ok" in src


def test_a_failed_criterion_still_blocks_regardless_of_scope():
    """Kapsamı raporlamak, kapıyı gevşetmek değil."""
    rob = _rob()
    rob["multi_symbol"] = {"generalization_label": "✗ Symbol specific"}
    t = ab._robustness_tally(rob, strict=False)
    assert t.ok is False and t.failed >= 1


def test_too_few_evaluated_criteria_still_cannot_pass():
    t = ab._robustness_tally(_rob(wfo=False, ms=False, mc=False), strict=False)
    assert t.ok is False and t.evaluated == 1 and t.required == 2
    # Sıkı modda aynı payload üçlü çıtayı da geçemez.
    assert ab._robustness_tally(_rob(wfo=False, ms=False), strict=True).ok is False


def test_strictness_shows_up_in_the_required_field():
    assert ab._robustness_tally(_rob(), strict=True).required == 3
    assert ab._robustness_tally(_rob(), strict=False).required == 2
