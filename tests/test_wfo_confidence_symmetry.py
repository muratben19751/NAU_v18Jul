"""WFO amaç fonksiyonu: güven sönümlemesi NEGATİF skorlarda TERS çalışıyordu.

DeepR 2026-08-11 [ORTA]: `objective_value` skoru `val *= n / (n + K)` ile
sönümlüyor (K=20). Amaç, az işlemle şişmiş skorları bastırmak. Ama çarpan
0<conf<1 olduğu için NEGATİF skorlarda etki tersine dönüyordu: değeri sıfıra
YAKLAŞTIRIYOR, yani İYİLEŞTİRİYORDU. İki aday da fold başına sharpe=-1.0
üretse, 5 işlemli aday -0.20, 200 işlemli aday -0.909 alıyor ve GA turnuvası
(yüksek skor kazanır) İSTATİSTİKSEL OLARAK ANLAMSIZ, KAYBEDEN adayı seçiyordu.
Düşen piyasa dilimlerinde ve erken jenerasyonlarda fold skorlarının çoğu
negatif olduğu için bu istisna değil, tipik durumdu.

Duruş `web/routes/agent_backtest.py::_score` ile AYNI (2026-08-11'de orada
düzeltilen ikiz hata): `k = n/(n+20)`; `base >= 0` ise `× k`, `base < 0` ise
`÷ k`. Az örneklem her iki yönde de "emin değiliz" demektir — "daha az kötü"
değil. İkinci bir varyant yok, tek duruş.

Wiki References
---------------
Bkz: [[auto_arama_ekonomisi]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import pytest

import wfo_optimizer as W


class _Result:
    """objective_value'nun okuduğu yüzey — backtest çalıştırılmaz."""

    def __init__(self, **metrics):
        self.error = None
        self.metrics = metrics


def _sharpe(n: int, value: float) -> _Result:
    return _Result(n_trades=n, sharpe_per_trade=value, pnl_pct=0.0, max_dd=-0.1)


class TestNegativeScoresArePenalisedNotRewarded:
    def test_few_trade_loser_never_outranks_a_many_trade_loser(self):
        """Denetimin somut örneği: ikisi de sharpe=-1.0, biri 5 diğeri 200 işlem."""
        few = W.objective_value(_sharpe(5, -1.0))
        many = W.objective_value(_sharpe(200, -1.0))

        # GA turnuvası YÜKSEK skoru seçer: az işlemli kaybeden KAZANMAMALI.
        assert few < many
        # ×conf sonucu -0.20 idi (sıfıra yaklaşarak iyileşiyordu); ÷conf ile
        # az örneklem negatifte de cezalanır.
        assert few == pytest.approx(-1.0 / (5 / 25))
        assert many == pytest.approx(-1.0 / (200 / 220))

    def test_positive_side_damping_is_unchanged(self):
        """Pozitif tarafta davranış AYNEN korunur — şişmiş skor bastırılır."""
        few = W.objective_value(_sharpe(5, 1.0))
        many = W.objective_value(_sharpe(200, 1.0))

        assert few == pytest.approx(1.0 * (5 / 25))
        assert many == pytest.approx(1.0 * (200 / 220))
        assert few < many

    def test_monotonic_in_trade_count_on_both_sides_of_zero(self):
        """Aynı ham skorda işlem sayısı arttıkça skor sıfırdan UZAKLAŞIR."""
        losers = [W.objective_value(_sharpe(n, -0.5)) for n in (5, 20, 100, 500)]
        winners = [W.objective_value(_sharpe(n, 0.5)) for n in (5, 20, 100, 500)]

        assert losers == sorted(losers)  # -2.5 … -0.5 → artan
        assert winners == sorted(winners)  # 0.1 … 0.5 → artan

    def test_zero_score_is_untouched_by_either_branch(self):
        assert W.objective_value(_sharpe(7, 0.0)) == pytest.approx(0.0)

    def test_nan_fallback_chain_uses_the_same_stance(self):
        """sortino/calmar geri dönüş yolları da simetrik sönümlenmeli."""
        few = _Result(
            n_trades=5, sharpe_per_trade=float("nan"), sortino=-2.0, max_dd=-0.1
        )
        many = _Result(
            n_trades=200, sharpe_per_trade=float("nan"), sortino=-2.0, max_dd=-0.1
        )
        assert W.objective_value(few) < W.objective_value(many)

        # calmar dalı (sortino da yok): pnl_pct negatif → base negatif
        few_c = _Result(
            n_trades=5, sharpe_per_trade=float("nan"), pnl_pct=-0.20, max_dd=-0.10
        )
        many_c = _Result(
            n_trades=200, sharpe_per_trade=float("nan"), pnl_pct=-0.20, max_dd=-0.10
        )
        assert W.objective_value(few_c) < W.objective_value(many_c)

    def test_damping_off_leaves_scores_untouched(self, monkeypatch):
        """K=0 → conf=1: ne çarpma ne bölme bir şey değiştirir."""
        monkeypatch.setattr(W, "WFO_TRADE_CONF_K", 0)
        assert W.objective_value(_sharpe(5, -1.0)) == pytest.approx(-1.0)
        assert W.objective_value(_sharpe(5, 1.0)) == pytest.approx(1.0)
