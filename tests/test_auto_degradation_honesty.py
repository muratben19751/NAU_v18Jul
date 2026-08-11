"""AUTO'nun kendi bozulmaları hakkında DÜRÜST kalması.

2026-08-10 çoklu ajan denetimi üç sessiz yalan buldu ve hepsi aynı sınıftandı:
koşu bozulduğunu biliyordu ama söylemiyordu.

* `fallback_count` gerçek bozulmanın %38'ini sayıyordu — custom-block üretimi
  çöktüğünde ve "önceki spec ile devam" edildiğinde sayaç hiç artmıyordu.
* Faz satırı her kazanansız turda "all iterations failed" diyordu; oysa
  ölçülen koşuda 8 backtest'in 8'i de başarıyla koşmuştu ve eleme tamamen
  alfa kapısındaydı.
* Yanıtsız kalan LLM çağrılarının girdi token'ı hiçbir yere yazılmıyordu;
  250.000'lik tavan fiilen %7,5 aşılmıştı.

Bu testler o üç yalanı pinler.
"""

from __future__ import annotations

import json
import types

import pytest

import llm_dispatch
from web.routes import agent_backtest as ab

# ── Faz satırı: gerçek gerekçe ───────────────────────────────────────────────


def _result(**metrics):
    """Alfa kapısının okuduğu asgari IterationResult ikizi."""
    return types.SimpleNamespace(error=None, metrics=metrics)


def _passing_metrics(**over):
    m = {
        "n_trades": 500,
        "pnl": 1000.0,
        "sharpe_per_trade": 0.05,
        "excess_return_fraction": 0.5,
    }
    m.update(over)
    return m


def test_no_eligible_label_names_the_gate_not_a_crash():
    """Hepsi koştu ama benchmark'ı geçemedi — metin bunu söylemeli."""
    results = [
        (
            None,
            _result(**_passing_metrics(excess_return_fraction=-20.9)),
            "1-HOUR",
        )
        for _ in range(8)
    ]
    label = ab._no_eligible_phase_label(results)
    assert "0/8" in label
    assert "8 ran successfully" in label
    assert "below buy&hold" in label
    # Eski yalan bir daha yazılmasın.
    assert "all iterations failed" not in label


def test_no_eligible_label_separates_crashes_from_gate_rejections():
    crashed = types.SimpleNamespace(error="boom", metrics=None)
    results = [
        (None, crashed, "1-HOUR"),
        (None, _result(**_passing_metrics(n_trades=3)), "1-HOUR"),
        (None, _result(**_passing_metrics(pnl=-5.0)), "1-HOUR"),
    ]
    label = ab._no_eligible_phase_label(results)
    assert "1 crashed" in label
    assert f"<{ab._MIN_TRADES} trades" in label
    assert "PnL ≤ 0" in label
    assert "2 ran successfully" in label


@pytest.mark.parametrize(
    "metrics",
    [
        _passing_metrics(),
        _passing_metrics(n_trades=1),
        _passing_metrics(pnl=-1.0),
        _passing_metrics(sharpe_per_trade=-0.1),
        _passing_metrics(excess_return_fraction=-1.0),
        _passing_metrics(pnl=float("nan")),
    ],
)
def test_gate_and_its_diagnosis_never_diverge(metrics):
    """Kapı ile teşhis tek kaynaktan gelmeli — ayrı yazılsalardı ıraksarlardı."""
    r = _result(**metrics)
    assert ab._pre_robustness_eligible(r) is (ab._ineligibility_reason(r) is None)


# ── Bozulma sayacı: kalıcı kayda geçmeli ─────────────────────────────────────


def test_session_end_carries_the_fallback_count(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
    run_id = "honesty1"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = {
            "steps": [],
            "fallback_count": 3,
            "fallback_reasons": ["TimeoutError", "TruncatedResponse"],
        }
    try:
        ab._session_log(run_id, "session_end", outcome="winless_limit", round=3)
        lines = (tmp_path / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        assert rec["event"] == "session_end"
        assert rec["fallback_count"] == 3
        assert rec["fallback_reasons"] == ["TimeoutError", "TruncatedResponse"]
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)


def test_degraded_is_a_structural_event():
    """Uzun oturumda `steps` taşınca fallback kanıtı elenmemeli."""
    from web.routes import sessions

    assert "degraded" in sessions._STRUCTURAL_EVENTS


# ── Yanıtsız çağrının tahmini girdisi ────────────────────────────────────────


def test_error_usage_is_estimated_and_flagged():
    usage = llm_dispatch._estimated_error_usage(
        {
            "system": "s" * 4000,
            "messages": [{"role": "user", "content": "u" * 8000}],
            "max_tokens": 1000,
        },
        "or:some/model",
        "composed",
    )
    assert usage is not None
    assert usage["estimated"] is True
    assert usage["output_tokens"] == 0
    # ~12k bayt / 4 → binler mertebesi; byte sayısının kendisi DEĞİL.
    assert 2000 < usage["input_tokens"] < 4000


def test_empty_request_estimates_nothing():
    """İçeriksiz bir istekte tahmin üretilmez — sadece çerçeveleme payı kalır."""
    assert llm_dispatch._estimated_error_usage({}, "m", "p") is None
    assert llm_dispatch._estimated_error_usage({"messages": []}, "m", "p") is None


def test_timeout_reaches_the_budget_observer_but_stop_does_not(monkeypatch):
    """Timeout bütçeye yazılır; STOP yazılmaz (prompt gitmemiş olabilir)."""
    from llm_client import LLMCallCancelled

    seen: list[dict] = []
    monkeypatch.setattr(llm_dispatch, "_observe_llm", lambda **e: seen.append(e))
    monkeypatch.setattr(llm_dispatch, "_admit_llm_request", lambda *a, **k: None)
    monkeypatch.setattr(llm_dispatch, "_check_llm_cancelled", lambda: None)
    monkeypatch.setattr(llm_dispatch, "current_model", lambda: "claude-test-model")
    recorded: list[tuple] = []
    monkeypatch.setattr(
        llm_dispatch,
        "_ledger_record_usage",
        lambda usage, model, purpose: recorded.append((usage, model, purpose)),
    )

    kwargs = {
        "system": "s" * 4000,
        "messages": [{"role": "user", "content": "u" * 4000}],
        "max_tokens": 500,
    }

    class _Boom:
        class messages:  # noqa: N801 - taklit edilen SDK yüzeyi
            @staticmethod
            def create(**_):
                raise TimeoutError("deadline exceeded")

    with pytest.raises(TimeoutError):
        llm_dispatch._create_message_once(_Boom(), "composed", **kwargs)

    assert seen[-1]["status"] == "error"
    assert seen[-1]["usage"]["estimated"] is True
    assert seen[-1]["usage"]["input_tokens"] > 0
    assert recorded and recorded[-1][2].endswith(":error-est")

    seen.clear()
    recorded.clear()

    class _Cancelled:
        class messages:  # noqa: N801
            @staticmethod
            def create(**_):
                raise LLMCallCancelled("stopped")

    with pytest.raises(LLMCallCancelled):
        llm_dispatch._create_message_once(_Cancelled(), "composed", **kwargs)

    assert seen[-1]["status"] == "cancelled"
    assert seen[-1]["usage"] is None
    assert not recorded


# ── İki sessiz yol artık sayaca giriyor ──────────────────────────────────────
#
# Bu ikisi davranışsal olarak ancak tam bir AUTO iterasyonu kurularak
# koşturulabilir (LLM istemcisi, worker state, katalog). Kaynak-pini
# `test_regression_anchors.py` üslubunda: ucuz ve tam olarak geri alınmasını
# istemediğimiz satırı hedefliyor.


def test_custom_block_failure_counts_as_degradation():
    import inspect

    src = inspect.getsource(ab._generate_custom_spec)
    tail = src[src.rindex("except Exception") :]
    assert "_mark_degraded" in tail, "custom-block çöküşü sayaca girmiyor"
    assert "_raise_if_llm_control_abort" in tail, "STOP fallback'e indirgeniyor"


def test_reused_previous_spec_counts_as_degradation():
    import inspect

    src = inspect.getsource(ab._propose_next_strategy)
    tail = src[src.rindex("except Exception") :]
    assert "_mark_degraded" in tail, "'önceki spec ile devam' sayaca girmiyor"
    assert "degraded_spec_ids" in tail, "tekrarlanan aday kazanan olabilir"
