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


def test_generated_block_rejects_invalid_literal_in_unreached_branch():
    import agent

    source = """\
def max_lookback(params):
    return 2

def evaluate(state, block, closes, indicators, portfolio):
    if closes[-1] < 0:
        return "long"
    return None
"""
    with pytest.raises(agent.GeneratedCodeError, match="line.*long"):
        agent._test_execute_generated(
            source,
            meta={"params": {}},
            require_max_lookback=True,
            role_hint="exit",
        )


def test_generated_block_rejects_dynamic_role_return():
    import agent

    source = """\
def max_lookback(params):
    return 2

def evaluate(state, block, closes, indicators, portfolio):
    signal = "exit"
    return signal
"""
    with pytest.raises(agent.GeneratedCodeError, match="dynamic returns"):
        agent._test_execute_generated(
            source,
            meta={"params": {}},
            require_max_lookback=True,
            role_hint="exit",
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


def test_custom_block_pair_is_saved_in_one_registry_transaction(monkeypatch, tmp_path):
    import json

    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "registry.json")
    blocks = [
        {"name": "pair_entry", "meta": {"role": "entry"}, "code": "x = 1\n"},
        {"name": "pair_exit", "meta": {"role": "exit"}, "code": "x = 2\n"},
    ]
    paths = store.save_custom_batch(blocks)
    registry = json.loads(store.REGISTRY_FILE.read_text(encoding="utf-8"))
    assert {p.name for p in paths} == {"pair_entry.py", "pair_exit.py"}
    assert set(registry) == {"pair_entry", "pair_exit"}


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


def test_is_oos_definitive_failure_classifier():
    import web.routes.agent_backtest as ab

    assert ab._split_definitive_failure(
        {"overfitting_label": "✗ Overfitting suspected"}
    )
    assert not ab._split_definitive_failure(
        {"overfitting_label": "⚠ Caution"}
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


def test_pre_robustness_gate_rejects_absolute_profit_without_alpha():
    import web.routes.agent_backtest as ab

    result = _result(pnl_pct=0.1, sharpe=0.4, trades=636)
    result.metrics.update(pnl=1105.27, excess_pnl_pct=-0.1858498)
    assert not ab._pre_robustness_eligible(result)
    result.metrics["excess_pnl_pct"] = 0.01
    assert ab._pre_robustness_eligible(result)


def test_external_gap_report_fails_on_multiyear_hole():
    import data

    idx = pd.to_datetime(["2004-12-31", "2011-01-03"], utc=True)
    frame = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    gap = data.external_data_gap_report(frame)
    assert gap and gap["days"] > 2000


def test_metrics_thinning_reaches_nested_mtm_curve():
    import web.routes.agent_backtest as ab

    payload = {
        "equity_curve_mtm": [[str(i), float(i)] for i in range(10_000)],
        "n_trades": 30,
    }
    thin = ab._thin_curves(payload, cap=400)
    assert len(thin["equity_curve_mtm"]) == 400
    assert thin["n_trades"] == 30


def test_robustness_payload_is_stored_as_gzip_artifact(monkeypatch, tmp_path):
    import gzip
    import json

    import web.routes.agent_backtest as ab

    monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
    payload = {"wfo_windows": [{"curve": list(range(10_000))}]}
    ref = ab._write_session_artifact("run1", "robustness-r1-c1", payload)
    path = tmp_path / ref["path"]
    assert path.stat().st_size == ref["bytes"]
    assert json.loads(gzip.decompress(path.read_bytes())) == payload


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


def test_llm_observer_sees_each_actual_provider_response():
    import agent

    events = []

    class Usage:
        input_tokens = 11
        output_tokens = 7
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    response = SimpleNamespace(usage=Usage(), model="fake", stop_reason=None)
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: response)
    )
    agent.set_thread_llm_control(lambda: False, events.append)
    try:
        assert agent._create_message_once(client, "probe", max_tokens=50) is response
    finally:
        agent.set_thread_llm_control(None, None)
    assert events[0]["usage"]["input_tokens"] == 11
    assert events[0]["purpose"] == "probe"
    assert events[0]["status"] == "ok"


def test_llm_call_honors_cooperative_cancel_before_provider():
    import agent

    calls = []
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs))
    )
    agent.set_thread_llm_control(lambda: True, None)
    try:
        with pytest.raises(agent.LLMCallCancelled):
            agent._create_message_once(client, "probe", max_tokens=50)
    finally:
        agent.set_thread_llm_control(None, None)
    assert calls == []


def test_llm_budget_admission_runs_before_provider():
    import agent

    calls = []

    class Refused(RuntimeError):
        llm_control_abort = True

    def reject(request):
        assert request["input_token_bound"] >= len("hello")
        assert request["output_token_bound"] == 4000
        raise Refused("budget exhausted")

    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs))
    )
    agent.set_thread_llm_control(lambda: False, None, reject)
    try:
        with pytest.raises(Refused, match="budget exhausted"):
            agent._create_message_once(
                client,
                "probe",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=4000,
            )
    finally:
        agent.set_thread_llm_control(None, None, None)
    assert calls == []


def test_openrouter_usage_includes_provider_cost():
    import agent

    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=5,
        cost=0.0123,
        model_dump=lambda: {"cost": 0.0123},
    )
    assert agent._openrouter_usage_payload(usage) == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "cost_usd": 0.0123,
    }


def test_openrouter_auto_path_uses_killable_process(monkeypatch):
    import agent

    seen = {}

    def fake_run(request, config, timeout):
        seen.update(request=request, config=config, timeout=timeout)
        return {
            "ok": True,
            "text": "done",
            "prompt_tokens": 8,
            "completion_tokens": 2,
            "cost_usd": 0.004,
            "finish_reason": "stop",
        }

    monkeypatch.setattr(agent, "_run_openrouter_killable", fake_run)
    messages = agent._OpenRouterMessages(
        SimpleNamespace(), process_config={"base_url": "x", "api_key": "y"}
    )
    agent.set_thread_llm_control(lambda: False, None, None)
    try:
        response = messages.create(
            model="vendor/model",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=20,
            timeout=7,
        )
    finally:
        agent.set_thread_llm_control(None, None, None)
    assert seen["timeout"] == 7
    assert response.content[0].text == "done"
    assert response.usage.cost_usd == 0.004


def test_route_budget_rejects_conservative_overshoot(monkeypatch):
    import web.routes.agent_backtest as ab

    run_id = "budget-test"
    monkeypatch.setattr(ab, "_session_log", lambda *args, **kwargs: None)
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = {
            "tokens_in": 90,
            "tokens_out": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "max_total_tokens": 100,
        }
    try:
        with pytest.raises(ab.AgentBudgetReached, match="cannot admit"):
            ab._admit_llm_budget(
                run_id,
                {
                    "input_token_bound": 5,
                    "output_token_bound": 10,
                    "total_token_bound": 15,
                },
            )
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)


def test_external_auto_normalizes_candidate_exposure(monkeypatch):
    import web.routes.agent_backtest as ab

    monkeypatch.setattr(ab, "NORMALIZE_EXTERNAL_EXPOSURE", True)
    monkeypatch.setattr(ab, "AGENT_EQUITY_PCT", 95.0)
    spec = SimpleNamespace(
        trade_size_mode="atr_target",
        trade_size_percent=5.0,
        trade_size=0.01,
    )
    audit = ab._clamp_spec_trade_size(spec)
    assert audit["before"]["trade_size_mode"] == "atr_target"
    assert spec.trade_size_mode == "percent_equity"
    assert spec.trade_size_percent == 95.0
    assert spec.trade_size == 1.0


def test_candidate_fingerprint_distinguishes_exact_from_family():
    import web.routes.agent_backtest as ab

    first = ab._proposal_to_spec(
        {
            "blocks": [
                {"type": "momentum", "role": "entry", "params": {"lookback": 5}}
            ]
        }
    )
    second = ab._proposal_to_spec(
        {
            "blocks": [
                {"type": "momentum", "role": "entry", "params": {"lookback": 20}}
            ]
        }
    )
    assert ab._candidate_fingerprint(first) != ab._candidate_fingerprint(second)
    assert ab._candidate_fingerprint(
        first, family=True
    ) == ab._candidate_fingerprint(second, family=True)


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


def test_stale_single_key_unadjusted_override_is_not_enough(monkeypatch):
    from fastapi.testclient import TestClient

    import data
    from server import app

    monkeypatch.setenv("AGENT_ALLOW_UNADJUSTED", "1")
    monkeypatch.delenv("AGENT_RESEARCH_MODE", raising=False)
    monkeypatch.setattr(data, "_external_bar_dir", lambda *a: (object(), object()))
    monkeypatch.setattr(data, "_external_adjusted_flag", lambda *a: False)
    response = TestClient(app).post(
        "/agent/run",
        data={"symbol": "QQQ.NASDAQ", "tfs": ["D"], "n_iterations": 2},
    )
    assert response.status_code == 400
    assert "AGENT_RESEARCH_MODE=1" in response.text
