"""Anlatı (narrative) düşüşü sessiz olmasın; STOP hiç düşüş sayılmasın.

Üç yerde aynı desen vardı — `except Exception: <şablon cümlesi döndür>` — ve iki
ayrı kusuru taşıyordu:

  1. **Sessizlik.** LLM hiç konuşmasa bile ekranda gayet normal duran bir cümle
     çıkıyordu: `degraded` bayrağı yok, log yok. Ölçüm sırasında bunu ayırt
     edebilmek için şablon metnini birebir yeniden üretmek gerekti — yani
     dışarıdan ayırt EDİLEMEZ durumdaydı ([[makul_fallback_sessiz_fallbacktan_tehlikelidir]]).
  2. **STOP'u yutması.** `LLMCallCancelled` de `Exception`tır. Koşu iptal
     edilirken anlatı üretiliyorsa iptal, başarılı görünen bir cümleye
     dönüşüyordu — `llm_client._raise_if_llm_control_abort`'un tam olarak
     yasakladığı şey ("Never disguise STOP/budget control flow as a successful
     fallback").

İkincisi bir doğruluk hatasıydı, birincisi gözlemlenebilirlik. Bu dosya ikisini
de üç yüzeyde birden sabitler.

Wiki References
---------------
See: [[kesilme_ve_degrade_gorunurlugu]], [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

import logging

import pytest

from llm_client import LLMCallCancelled

ROW = {
    "strategy": "SMA Cross",
    "pnl": 1240.5,
    "pnl_fmt": "$1,240.50",
    "pnl_pct_fmt": "+12.4%",
    "n_trades": 48,
    "n_wins": 27,
    "n_losses": 21,
    "win_rate": 0.5625,
    "win_rate_fmt": "56.3%",
    "sharpe_fmt": "1.12",
    "sortino_fmt": "1.48",
    "max_dd_fmt": "-8.2%",
    "avg_dur_fmt": "3d 4h",
}
STATE = {"strategy_name": "SMA Cross", "winner_spec_name": "SMA Cross"}


def _sites():
    """(ad, çağrılabilir, yamalanacak modül) üçlüleri — üç anlatı yüzeyi."""
    import web.routes.agent_backtest as ab
    import web.routes.backtest as bt
    import web.routes.lab as lab

    return [
        ("narrative", lambda: bt._generate_narrative(ROW), bt),
        ("lab_narrative", lambda: lab._lab_narrative(ROW, STATE), lab),
        ("winner_narrative", lambda: ab._winner_narrative(ROW, STATE), ab),
    ]


@pytest.mark.parametrize("case", _sites(), ids=lambda c: c[0])
class TestNarrativeFallback:
    def test_ordinary_failure_falls_back_but_LOGS(self, case, monkeypatch, caplog):
        name, call, mod = case

        def _boom(*a, **k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr("agent._get_client", _boom)

        with caplog.at_level(logging.WARNING):
            text = call()

        # Şablona düşmüş olmalı — kullanıcı bir şey görmeli.
        assert text.strip()
        # ...ama operatör de görmeli: sessizlik bu testin konusu.
        # getMessage(): LogRecord'un %-argümanlarını uygulanmış hâli. `.message`
        # yalnız formatlayıcı çalıştıysa dolu olur, `.message % .args` ise çift
        # formatlama yapıp patlar.
        logged = [r.getMessage() for r in caplog.records]
        assert any("LLM anlatısı üretilemedi" in m for m in logged), (
            f"{name}: düşüş log'a geçmedi — {logged}"
        )
        # Sebep de yazılsın: "bir şey oldu" teşhis etmeye yetmez.
        assert any("RuntimeError" in m for m in logged)
        assert any(name in m for m in logged), f"{name}: hangi yüzey olduğu yazmıyor"

    def test_stop_is_NOT_swallowed(self, case, monkeypatch):
        """İptal, başarılı görünen bir cümleye dönüşmemeli."""
        name, call, mod = case

        def _cancelled(*a, **k):
            raise LLMCallCancelled("AUTO stop requested")

        monkeypatch.setattr("agent._get_client", _cancelled)

        with pytest.raises(LLMCallCancelled):
            call()

    def test_budget_abort_is_NOT_swallowed(self, case, monkeypatch):
        """`llm_control_abort` işaretli her istisna da kontrol akışıdır."""
        name, call, mod = case

        class _BudgetAbort(RuntimeError):
            llm_control_abort = True

        def _abort(*a, **k):
            raise _BudgetAbort("budget ceiling")

        monkeypatch.setattr("agent._get_client", _abort)

        with pytest.raises(_BudgetAbort):
            call()
