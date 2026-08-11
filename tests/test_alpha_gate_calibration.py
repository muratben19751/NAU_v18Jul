"""Alfa kapısının kalibrasyonu: pencere-bağımsız, aynı maliyet tabanlı, risk-ayarlı.

Eski kapı KÜMÜLATİF buy&hold farkı istiyordu ve bu bir araştırma kararı değil,
veri penceresinin yan etkisiydi. Ölçüm (koşu 44cb54e2, 2026-08-10): QQQC'nin
22,7 yıllık serisinde buy&hold %2093 yaptığı için 12 adayın 12'si elendi —
9'u kârlı, en iyisi Sharpe 0,79 / +196.880 USD olmasına rağmen. Aynı arama
1 yıllık bir katalogda kolayca kazanan bulurdu.

Yerine iki koşul: yıllıklandırılmış alfa (iki taraf da net) VE risk-ayarlı
üstünlük (Calmar). Bu testler o sözleşmeyi pinler.
"""

from __future__ import annotations

import math
import types

import pandas as pd
import pytest

from app_constants import (
    BENCHMARK_ROUND_TRIP_COST_FRACTION,
    annualized_return,
    max_drawdown_fraction,
    stamp_buy_hold_benchmark,
    window_years,
)
from web.routes import agent_backtest as ab

# ── Yıllıklandırma çekirdeği ─────────────────────────────────────────────────


def test_annualized_return_is_compound_not_linear():
    # 22,74 yılda %2093 → yılda ~%14,6 (basit bölme %92 derdi).
    cagr = annualized_return(20.93, 22.74)
    assert cagr == pytest.approx(0.1454, abs=5e-4)


def test_annualized_return_is_window_independent():
    """Aynı YILLIK oran, farklı pencerelerde aynı CAGR'ı vermeli."""
    for years in (1.0, 5.0, 22.74):
        total = 1.12**years - 1.0
        assert annualized_return(total, years) == pytest.approx(0.12, abs=1e-9)


@pytest.mark.parametrize(
    "total,years",
    [(0.5, 0.0), (0.5, -1.0), (float("nan"), 5.0), (0.5, float("inf"))],
)
def test_annualized_return_refuses_degenerate_input(total, years):
    assert annualized_return(total, years) is None


def test_total_loss_is_minus_one_not_a_crash():
    """-100% getiri: negatif tabanın kesirli kuvveti ComplexWarning üretirdi."""
    assert annualized_return(-1.0, 5.0) == -1.0
    assert annualized_return(-1.5, 5.0) == -1.0


def test_window_years_reads_the_index_span():
    idx = pd.date_range("2020-01-01", "2030-01-01", periods=100, tz="UTC")
    df = pd.DataFrame({"close": range(100)}, index=idx)
    assert window_years(df) == pytest.approx(10.0, abs=0.02)


def test_window_years_none_without_a_time_index():
    df = pd.DataFrame({"close": [1, 2, 3]})
    assert window_years(df) is None


def test_max_drawdown_is_the_worst_peak_to_trough():
    assert max_drawdown_fraction([100, 200, 50, 300]) == pytest.approx(-0.75)
    assert max_drawdown_fraction([100, 110, 120]) == pytest.approx(0.0)
    assert max_drawdown_fraction([]) is None


# ── Damgalama: yeni alanlar ve maliyet tabanı ────────────────────────────────


def _bars(first: float, last: float, years: float = 10.0, low: float | None = None):
    start = pd.Timestamp("2020-01-01", tz="UTC")
    idx = pd.date_range(start, start + pd.Timedelta(days=365.25 * years), periods=3)
    mid = low if low is not None else (first + last) / 2
    return pd.DataFrame({"close": [first, mid, last]}, index=idx)


def test_stamp_adds_annualized_fields():
    m = {"pnl_pct": 1.0, "max_dd": -0.2}
    # Aradaki dip 80: benchmark'ın da bir düşüşü olsun, yoksa risk bacağı
    # (benchmark_max_dd) sıfır çıkar ve Calmar karşılaştırması anlamsızlaşır.
    stamp_buy_hold_benchmark(m, _bars(100.0, 300.0, low=80.0))
    assert m["window_years"] == pytest.approx(10.0, abs=0.02)
    # benchmark brüt %200, gidiş-dönüş maliyeti düşülüp yıllıklandırılır
    assert m["benchmark_cagr"] == pytest.approx(
        annualized_return(2.0 - BENCHMARK_ROUND_TRIP_COST_FRACTION, m["window_years"]),
        abs=1e-9,
    )
    assert m["strategy_cagr"] == pytest.approx(
        annualized_return(1.0, m["window_years"]), abs=1e-9
    )
    assert m["annualized_alpha"] == pytest.approx(
        m["strategy_cagr"] - m["benchmark_cagr"], abs=1e-12
    )
    assert m["benchmark_max_dd"] < 0
    assert "strategy_calmar" in m and "benchmark_calmar" in m


def test_benchmark_leg_pays_costs_too():
    """'Aynı maliyet tabanı' iddiası ancak iki taraf da öderse doğrudur."""
    m = {"pnl_pct": 1.0, "max_dd": -0.2}
    stamp_buy_hold_benchmark(m, _bars(100.0, 300.0))
    gross = annualized_return(2.0, m["window_years"])
    assert m["benchmark_cagr"] < gross


def test_dividend_yield_is_recorded_even_when_zero():
    """Sayılmıyor olması, görünmez olması demek değil."""
    m = {"pnl_pct": 0.5, "max_dd": -0.1}
    stamp_buy_hold_benchmark(m, _bars(100.0, 150.0))
    assert "benchmark_dividend_yield_annual" in m


def test_stamp_is_a_noop_without_a_time_index():
    m = {"pnl_pct": 1.0, "max_dd": -0.2}
    stamp_buy_hold_benchmark(m, pd.DataFrame({"close": [100.0, 200.0]}))
    assert "annualized_alpha" not in m
    # Eski kümülatif alanlar yine de damgalanır (geriye uyumluluk).
    assert m["excess_return_fraction"] == pytest.approx(0.0)


# ── Kapı davranışı ───────────────────────────────────────────────────────────


def _result(**metrics):
    base = {"n_trades": 500, "pnl": 1000.0, "sharpe_per_trade": 0.05}
    base.update(metrics)
    return types.SimpleNamespace(error=None, metrics=base)


def test_positive_alpha_with_better_calmar_passes():
    r = _result(
        annualized_alpha=0.03,
        strategy_calmar=0.9,
        benchmark_calmar=0.3,
        excess_return_fraction=-20.0,  # kümülatif fark ARTIK belirleyici değil
    )
    assert ab._ineligibility_reason(r) is None
    assert ab._pre_robustness_eligible(r) is True


def test_positive_alpha_but_worse_calmar_is_rejected():
    r = _result(annualized_alpha=0.03, strategy_calmar=0.1, benchmark_calmar=0.3)
    assert ab._ineligibility_reason(r) == "worse_risk_adjusted"


def test_negative_alpha_is_rejected_even_when_cumulative_looks_fine():
    r = _result(
        annualized_alpha=-0.001,
        strategy_calmar=9.0,
        benchmark_calmar=0.1,
        excess_return_fraction=5.0,
    )
    assert ab._ineligibility_reason(r) == "negative_alpha"


def test_alpha_without_risk_fields_passes_on_alpha_alone():
    """Risk bacağı ölçülemediyse uydurma — alfa ile geçir."""
    r = _result(annualized_alpha=0.02)
    assert ab._ineligibility_reason(r) is None


def test_no_benchmark_at_all_is_fail_closed():
    """Ne yıllık alfa ne kümülatif fark: ölçülmemiş üstünlük geçemez."""
    assert ab._ineligibility_reason(_result()) == "no_benchmark"


def test_legacy_records_fall_back_to_the_cumulative_rule():
    """Yıllıklandırma alanı yoksa sessizce kapıyı AÇMAK kabul edilemez."""
    assert ab._ineligibility_reason(_result(excess_return_fraction=-1.0)) == (
        "below_benchmark"
    )
    assert ab._ineligibility_reason(_result(excess_return_fraction=0.5)) is None


def test_gate_and_diagnosis_stay_one_source_under_the_new_rule():
    for m in (
        {"annualized_alpha": 0.03, "strategy_calmar": 0.9, "benchmark_calmar": 0.3},
        {"annualized_alpha": 0.03, "strategy_calmar": 0.1, "benchmark_calmar": 0.3},
        {"annualized_alpha": -0.01},
        {"excess_return_fraction": -2.0},
    ):
        r = _result(**m)
        assert ab._pre_robustness_eligible(r) is (ab._ineligibility_reason(r) is None)


def test_phase_label_names_the_new_rejection_reasons():
    results = [
        (None, _result(annualized_alpha=-0.05), "1-HOUR"),
        (
            None,
            _result(annualized_alpha=0.01, strategy_calmar=0.1, benchmark_calmar=0.9),
            "4-HOUR",
        ),
    ]
    label = ab._no_eligible_phase_label(results)
    assert "no annualized alpha" in label
    assert "worse Calmar than buy&hold" in label
    assert "2 ran successfully" in label


# ── Skor: güven çarpanı simetrik ─────────────────────────────────────────────


def _scored(n_trades: int, pnl_pct: float, max_dd: float = -0.2) -> float:
    return ab._score(
        types.SimpleNamespace(
            error=None,
            metrics={
                "n_trades": n_trades,
                "pnl_pct": pnl_pct,
                "max_dd": max_dd,
                "sharpe_per_trade": pnl_pct,
            },
        )
    )


def test_low_sample_is_penalised_on_the_negative_side_too():
    """Eskiden az işlemli KÖTÜ aday, çok işlemli kötü adayı geçiyordu."""
    few, many = _scored(27, -0.5), _scored(500, -0.5)
    assert few < many, "az örneklem negatif tarafta da cezalanmalı"


def test_positive_side_multiplier_is_unchanged():
    few, many = _scored(27, 0.5), _scored(500, 0.5)
    assert few < many, "pozitif tarafta güven çarpanı eskisi gibi davranmalı"


def test_symmetric_confidence_is_reciprocal():
    """base<0 için bölme, base>0 için çarpma — aynı k, ters yön."""
    n = 30
    k = n / (n + 20)
    pos, neg = _scored(n, 0.4), _scored(n, -0.4)
    # Aynı |base| büyüklüğünde negatif skor, pozitifin 1/k² katı olmalı.
    assert abs(neg) > abs(pos)
    assert abs(neg) / abs(pos) == pytest.approx(1 / k**2, rel=0.02)


def test_score_stays_finite_at_zero_trades_via_junk_rule():
    assert _scored(0, 0.1) == float("-inf")


# ── Peer sepeti: dikiş farkındalığı ──────────────────────────────────────────


def test_stitched_series_excludes_its_components(monkeypatch):
    monkeypatch.setattr(
        "data.external_manifest",
        lambda: {
            "QQQC": {"source": "stitch:QQQ+QQQQ"},
            "QQQ": {"source": "massive:rest_aggs"},
            "QQQQ": {"source": "massive:rest_aggs"},
            "SPY": {"source": "massive:rest_aggs"},
        },
    )
    assert ab._peer_exclusions("QQQC.NASDAQ") == {"QQQC", "QQQ", "QQQQ"}


def test_component_excludes_the_stitch_that_contains_it(monkeypatch):
    """İki yönlü: QQQ koşusunda QQQC de peer olamaz."""
    monkeypatch.setattr(
        "data.external_manifest",
        lambda: {"QQQC": {"source": "stitch:QQQ+QQQQ"}, "QQQ": {"source": "rest"}},
    )
    assert ab._peer_exclusions("QQQ.NASDAQ") == {"QQQ", "QQQC"}


def test_unrelated_symbol_excludes_only_itself(monkeypatch):
    monkeypatch.setattr(
        "data.external_manifest",
        lambda: {"QQQC": {"source": "stitch:QQQ+QQQQ"}, "SPY": {"source": "rest"}},
    )
    assert ab._peer_exclusions("SPY.ARCA") == {"SPY"}


def test_dotted_ticker_keeps_its_dot():
    """'BRK.A.NASDAQ' → 'BRK.A'; ilk noktadan bölmek manifest kaydını kaybettirir."""
    assert ab._bare_ticker("BRK.A.NASDAQ") == "BRK.A"
    assert ab._bare_ticker("QQQ.NASDAQ") == "QQQ"


def test_manifest_failure_still_excludes_self(monkeypatch):
    def _boom():
        raise RuntimeError("manifest unreadable")

    monkeypatch.setattr("data.external_manifest", _boom)
    assert ab._peer_exclusions("QQQC.NASDAQ") == {"QQQC"}


# ── Gerçek koşu verisiyle uçtan uca anlam kontrolü ───────────────────────────


def test_yesterdays_best_candidate_gets_a_readable_verdict():
    """Koşu 44cb54e2'nin en iyi adayı: +82.091 USD, 208 işlem, 22,7 yıl.

    Eski kapı 'below_benchmark' (kümülatif -12,90) diyordu — yorumlanamaz bir
    sayı. Yeni kapı 'yılda 4,3 puan geride' diyor; aynı ret, okunabilir gerekçe.
    """
    years = 22.74
    bench_cagr = annualized_return(21.10 - BENCHMARK_ROUND_TRIP_COST_FRACTION, years)
    strat_cagr = annualized_return(8.2091, years)
    alpha = strat_cagr - bench_cagr
    assert -0.05 < alpha < -0.04
    r = _result(
        n_trades=208,
        annualized_alpha=alpha,
        strategy_calmar=strat_cagr / 0.35,
        benchmark_calmar=bench_cagr / 0.53,
    )
    assert ab._ineligibility_reason(r) == "negative_alpha"
    assert math.isfinite(alpha)
