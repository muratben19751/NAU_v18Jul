from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest


def _result(*, pnl_pct=0.1, max_dd=-0.1, sharpe=1.0, trades=100):
    return SimpleNamespace(
        error=None,
        metrics={
            "pnl_pct": pnl_pct,
            "max_dd": max_dd,
            "sharpe_per_trade": sharpe,
            "n_trades": trades,
        },
    )


def test_exit_role_is_fail_closed_for_entry_vocabulary():
    from composer import _signal_matches_role

    assert _signal_matches_role("exit", "exit") is True
    assert _signal_matches_role("exit", "long") is False
    assert _signal_matches_role("exit", "short") is False
    assert _signal_matches_role("exit", None) is False


@pytest.mark.parametrize(
    ("role", "returned"), [("entry", "exit"), ("exit", "long"), ("exit", "short")]
)
def test_generated_block_smoke_enforces_role_contract(role, returned):
    import agent

    source = f"""\
def max_lookback(params):
    return 2

def evaluate(state, block, closes, indicators, portfolio):
    return {returned!r}
"""
    with pytest.raises(agent.GeneratedCodeError, match="role contract"):
        agent._test_execute_generated(
            source,
            meta={"params": {}},
            require_max_lookback=True,
            role_hint=role,
        )


def test_generated_block_meta_persists_role(monkeypatch):
    import agent

    class Usage:
        input_tokens = 1
        output_tokens = 1
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    payload = {
        "name": "role_probe",
        "meta": {"label": "Role probe", "params": {}},
        "code": (
            "def max_lookback(params):\n    return 2\n\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    return None\n"
        ),
    }
    monkeypatch.setattr(agent, "_call_claude_for_block", lambda prompt: (payload, {}))
    out = agent.propose_custom_block("x", "x", "exit")
    assert out["meta"]["role"] == "exit"


def test_wfo_requires_half_positive_even_with_positive_penalized_sharpe():
    import web.routes.agent_backtest as ab

    windows = [
        {"test_n_trades": 5, "test_metrics": {"pnl": 1.0 if i < 4 else -1.0}}
        for i in range(10)
    ]
    rob = {
        "split": {"overfitting_label": "✓ Robust"},
        "wfo_windows": windows,
        "oos_sharpe_penalized": 2.0,
        "mc": {"max_dd_p50": -10.0},
    }
    assert ab._robustness_passed(rob, strict=True) is False


def test_multi_symbol_definitive_failure_classifier():
    import web.routes.agent_backtest as ab

    assert ab._multi_symbol_definitive_failure(
        {"generalization_label": "✗ Symbol-specific"}
    )
    assert not ab._multi_symbol_definitive_failure(
        {"generalization_label": "⚠ Limited"}
    )


def test_holdout_promotion_requires_evidence_profit_sharpe_and_excess(monkeypatch):
    import web.routes.agent_backtest as ab

    monkeypatch.setattr(ab, "HOLDOUT_MIN_TRADES", 20)
    assert ab._holdout_promotion_passed(20, 0.1, 0.5, 0.01)
    assert not ab._holdout_promotion_passed(19, 0.1, 0.5, 0.01)
    assert not ab._holdout_promotion_passed(20, -0.1, 0.5, 0.01)
    assert not ab._holdout_promotion_passed(20, 0.1, None, 0.01)
    assert not ab._holdout_promotion_passed(20, 0.1, 0.5, 0.0)


def test_benchmark_is_stamped_and_score_uses_excess_return():
    import web.routes.agent_backtest as ab

    bars = pd.DataFrame({"close": [100.0, 120.0]})
    result = _result(pnl_pct=0.1)
    ab._stamp_buy_hold_benchmark(result, bars)
    assert result.metrics["benchmark_return_pct"] == pytest.approx(0.2)
    assert result.metrics["excess_pnl_pct"] == pytest.approx(-0.1)
    assert ab._score(result) < 0


def test_metrics_thinning_reaches_nested_mtm_curve():
    import web.routes.agent_backtest as ab

    payload = {
        "equity_curve_mtm": [[str(i), float(i)] for i in range(10_000)],
        "n_trades": 30,
    }
    thin = ab._thin_curves(payload, cap=400)
    assert len(thin["equity_curve_mtm"]) == 400
    assert thin["n_trades"] == 30


@pytest.mark.parametrize("status", [401, 402, 403])
def test_terminal_llm_http_statuses_open_the_circuit(status):
    import agent

    exc = RuntimeError("provider failed")
    exc.status_code = status
    assert agent.is_terminal_llm_error(exc)
    assert not agent.is_terminal_llm_error(SimpleNamespace(status_code=429))


def test_explicit_model_is_used_for_cost_accounting(monkeypatch):
    import token_ledger
    import web.routes.agent_backtest as ab

    seen = {}

    def fake_cost(counts, model):
        seen["model"] = model
        return 1.25

    monkeypatch.setattr(token_ledger, "cost_usd", fake_cost)
    model, cost = ab._llm_cost_usd(1, 2, 3, 4, model="or:vendor/model")
    assert (model, cost) == ("or:vendor/model", 1.25)
    assert seen["model"] == "or:vendor/model"


def test_fallback_composition_is_reproducible_per_run_seed():
    import agent

    agent.set_thread_random_seed("run-a")
    first = agent._fallback_composed()
    agent.set_thread_random_seed("run-a")
    replay = agent._fallback_composed()
    assert replay == first


def test_auto_slippage_flag_survives_spec_roundtrip():
    from composer import ComposedStrategySpec, SignalBlock

    spec = ComposedStrategySpec(
        id="slip",
        name="slip",
        description="",
        blocks=[
            SignalBlock(
                type="momentum",
                role="entry",
                params={"lookback": 2, "sign": "positive"},
            )
        ],
        model_slippage=True,
    )
    assert ComposedStrategySpec.from_dict(spec.to_dict()).model_slippage is True


def test_continuous_form_applies_finite_default_budgets(monkeypatch):
    from fastapi.testclient import TestClient

    import web.routes.agent_backtest as ab
    from server import app

    captured = {}

    class ImmediateThread:
        def __init__(self, *, target, kwargs, daemon):
            captured.update(kwargs)

        def start(self):
            return None

    monkeypatch.setattr(ab.threading, "Thread", ImmediateThread)
    response = TestClient(app).post(
        "/agent/run",
        data={"continuous": "1", "max_hours": "0", "max_total_tokens": "0"},
    )
    assert response.status_code == 200
    assert captured["max_hours"] == ab.DEFAULT_CONTINUOUS_MAX_HOURS
    assert captured["max_total_tokens"] == ab.DEFAULT_CONTINUOUS_MAX_TOKENS


def test_known_unadjusted_external_data_is_rejected(monkeypatch):
    from fastapi.testclient import TestClient

    import data
    from server import app

    monkeypatch.delenv("AGENT_ALLOW_UNADJUSTED", raising=False)
    monkeypatch.setattr(data, "_external_bar_dir", lambda *a: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a: False)
    response = TestClient(app).post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2},
    )
    assert response.status_code == 400
    assert "known-unadjusted" in response.text
