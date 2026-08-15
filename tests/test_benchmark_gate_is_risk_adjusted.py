"""Benchmark kapısı risk-ayarlı: Calmar üstünlüğü ASIL ölçü, alfa şart değil.

Kullanıcı kararı (2026-08-15). Gerekçe ölçümden: koşu 1fa9870e'de en iyi aday
`LRC Dip DMI ATR OBV` [1-HOUR] 784 işlemde +%442 yaptı, düşüşü −%26'da tuttu ve
Calmar'da buy&hold'u GEÇTİ (0,292 vs 0,269) — ama yıllık alfası −%6,8 olduğu için
elendi. QQQ 22,7 yılda yılda %14,5 yapmış; long-only bir strateji piyasadan zaman
zaman çıktığı için mutlak getiride kaybeder.

Pencereyi kısaltmak kurtarmıyor (ölçüldü): QQQ'nun her penceresinde buy&hold CAGR
%14-24 arası, 3 yıllık pencerede %24,2 ile daha da zor. Yani sorun pencere değil,
kapının mutlak getiri istemesiydi.

TAKAS AÇIK: bu mod "daha az getiri + çok daha az düşüş" stratejilerini kataloğa
alır. Bu yüzden bir TABAN var — para kaybeden bir strateji, düşüşü küçük diye
geçemez.

Wiki References
---------------
See: [[auto_kapi_ve_geri_bildirim]], [[auto_arama_ekonomisi]].
"""

from __future__ import annotations

import importlib

import pytest

import web.routes.agent_backtest as ab

# Koşu 1fa9870e'nin en iyi adayının GERÇEK metrikleri.
REAL_BEST = {
    "annualized_alpha": -0.06826042375390018,
    "strategy_calmar": 0.29183358170295126,
    "benchmark_calmar": 0.2685081437384318,
    "strategy_cagr": 0.07721808887501025,
}


@pytest.fixture
def absolute_mode(monkeypatch):
    monkeypatch.setenv("AGENT_BENCHMARK_GATE", "absolute")
    importlib.reload(ab)
    yield ab
    monkeypatch.delenv("AGENT_BENCHMARK_GATE", raising=False)
    importlib.reload(ab)


class TestRiskAdjustedMode:
    def test_the_real_candidate_now_passes(self):
        """Çıpa: bu tam olarak elenen adaydı."""
        assert ab.BENCHMARK_GATE_MODE == "risk_adjusted"
        assert ab._benchmark_rejection(REAL_BEST, -16.5) is None

    def test_negative_alpha_alone_no_longer_rejects(self):
        m = {**REAL_BEST, "annualized_alpha": -0.20}

        assert ab._benchmark_rejection(m, -16.5) is None

    def test_losing_money_is_still_rejected(self):
        """Taban: düşüşü küçük diye zarar eden strateji geçemez."""
        m = {**REAL_BEST, "strategy_cagr": -0.02}

        assert ab._benchmark_rejection(m, -16.5) == "not_profitable"

    def test_worse_calmar_is_still_rejected(self):
        """Asıl ölçü hâlâ bir ölçü — Calmar'ı düşük olan geçmez."""
        m = {**REAL_BEST, "strategy_calmar": 0.10}

        assert ab._benchmark_rejection(m, -16.5) == "worse_risk_adjusted"

    def test_unmeasurable_risk_leg_falls_back_to_alpha(self):
        """Risk bacağı yoksa bu modun ölçüsü kalmaz → alfaya dön (fail-closed)."""
        m = {"annualized_alpha": -0.05, "strategy_cagr": 0.08}

        assert ab._benchmark_rejection(m, -16.5) == "negative_alpha"

        ok = {"annualized_alpha": 0.05, "strategy_cagr": 0.08}
        assert ab._benchmark_rejection(ok, 1.0) is None


class TestAbsoluteModeStillAvailable:
    def test_same_candidate_is_rejected_on_alpha(self, absolute_mode):
        """Eski politika bir env ile geri gelmeli — karar geri alınabilir olsun."""
        assert absolute_mode.BENCHMARK_GATE_MODE == "absolute"
        assert absolute_mode._benchmark_rejection(REAL_BEST, -16.5) == "negative_alpha"


class TestBothGatesAgree:
    """Sıralama kapısı ile yayım kapısı ıraksarsa kullanıcı iki farklı cevap görür."""

    def test_holdout_uses_the_same_measure_when_metrics_are_given(self):
        passed, reason = ab._holdout_promotion_verdict(
            n_trades=50,
            pnl_pct=0.4,
            sharpe=0.7,
            excess_pnl_pct=-16.5,
            metrics=REAL_BEST,
        )

        assert passed, reason

    def test_holdout_keeps_the_legacy_rule_without_metrics(self):
        """Metrik verilmeyen çağıranlar kırılmasın."""
        passed, reason = ab._holdout_promotion_verdict(
            n_trades=50, pnl_pct=0.4, sharpe=0.7, excess_pnl_pct=-16.5
        )

        assert not passed
        assert "buy-and-hold" in reason

    def test_holdout_rejects_what_the_alpha_gate_rejects(self):
        losing = {**REAL_BEST, "strategy_cagr": -0.02}

        passed, reason = ab._holdout_promotion_verdict(
            n_trades=50, pnl_pct=0.4, sharpe=0.7, excess_pnl_pct=-16.5, metrics=losing
        )

        assert not passed
        assert "not_profitable" in reason
