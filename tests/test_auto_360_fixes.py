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
    monkeypatch.setattr(
        agent, "_call_claude_for_block", lambda prompt, **kwargs: (payload, {})
    )
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


def test_exit_role_literal_is_repaired_without_paid_retry(monkeypatch):
    import agent

    calls = []
    payload = {
        "name": "exit_probe",
        "meta": {"label": "Exit probe", "params": {}},
        "code": (
            "def max_lookback(params):\n    return 2\n\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    if closes[-1] > closes[-2]:\n"
            "        return 'long'\n"
            "    return None\n"
        ),
    }

    def fake_call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return payload, {"output_tokens": 100}

    monkeypatch.setattr(agent, "_call_claude_for_block", fake_call)
    out = agent.propose_custom_block("exit", "exit when price rises", "exit")
    assert len(calls) == 1
    assert calls[0][1]["role_hint"] == "exit"
    assert "return 'exit'" in out["code"]
    assert out["repair"] == "exit_literal_normalized"


def test_custom_block_role_system_prompt_is_role_locked():
    import agent

    exit_prompt = agent._custom_block_system_prompt("exit")
    entry_prompt = agent._custom_block_system_prompt("entry")
    assert "ROLE LOCK — EXIT ONLY" in exit_prompt
    assert 'strings "long" and "short" are forbidden' in exit_prompt
    assert "ROLE LOCK — ENTRY ONLY" in entry_prompt


def test_cli_output_cap_telemetry_marks_advisory_overrun():
    import agent

    telemetry = agent._output_cap_telemetry(
        agent._ClaudeCLIClient("claude"),
        {"output_tokens": 4_369},
        4_000,
    )
    assert telemetry == {
        "output_cap_mode": "advisory_cli",
        "output_cap_requested": 4_000,
        "output_cap_exceeded": True,
        "output_cap_excess_tokens": 369,
    }


def test_custom_block_token_limit_stops_retry(monkeypatch):
    import agent

    calls = []

    def fake_call(prompt, **kwargs):
        calls.append(prompt)
        return {"name": "missing_schema"}, {"output_tokens": 5000}

    monkeypatch.setenv("AGENT_CUSTOM_BLOCK_TOKEN_LIMIT", "4000")
    monkeypatch.setattr(agent, "_call_claude_for_block", fake_call)
    with pytest.raises(agent.GeneratedCodeError, match="token limit reached"):
        agent.propose_custom_block("x", "x", "entry")
    assert len(calls) == 1


def test_custom_block_ceiling_covers_the_measured_output(monkeypatch):
    """İstenen tavan, GÖZLENEN çıktı dağılımının üstünde kalmalı.

    Eski adı `test_custom_block_uses_compact_provider_limits`'ti ve 1.800'ü
    çiviliyordu — "kompakt" kendiliğinden iyi bir şey sanılmıştı. Defter tersini
    söyledi (1.566 çağrı: medyan 1.196, p95 3.268, maks 4.270): tavan çağrıların
    yarısına yakınını kesiyordu ve kesilen her çağrı tam bedelle çöpe gidiyordu.
    Canlı sonucu koşu `d515080e`: `TruncatedResponse` → rastgele kompozisyon.

    Test artık SAYIYI değil KURALI koruyor. Sayı ayarlanabilir; kuralı çiğneyen
    bir ayar — gözlenen maksimumun altına inmek — kırılmalı.
    """
    import agent

    captured = {}

    class Usage:
        input_tokens = 10
        output_tokens = 10
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    response = SimpleNamespace(
        usage=Usage(),
        stop_reason="end_turn",
        model="fake",
        content=[
            SimpleNamespace(
                type="text",
                text='{"name":"x","meta":{"label":"x","params":{}},"code":"def max_lookback(params):\\n return 1\\ndef evaluate(state, block, closes, indicators, portfolio):\\n return None"}',
            )
        ],
    )
    monkeypatch.setattr(agent, "_get_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        agent,
        "_create_message",
        lambda client, **kwargs: captured.update(kwargs) or response,
    )
    agent._call_claude_for_block("x", role_hint="entry")

    # token_usage.jsonl, 2026-08-17 · 1.566 `custom_block` çağrısının en büyüğü.
    MEASURED_MAX_OUTPUT = 4_270
    assert captured["max_tokens"] > MEASURED_MAX_OUTPUT, (
        "tavan gözlenen en uzun çıktının altında — kesilme kaçınılmaz, "
        "her kesilen çağrı tam bedelle çöpe gider"
    )
    assert captured["timeout"] == 75.0


def test_custom_block_timeout_does_not_retry(monkeypatch):
    import agent

    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("provider deadline")

    monkeypatch.setattr(agent, "_call_claude_for_block", timeout)
    with pytest.raises(agent.GeneratedCodeError, match="TimeoutError"):
        agent.propose_custom_block("x", "x", "entry")
    assert len(calls) == 1


def test_openrouter_pin_reports_provider_enforced_output_cap():
    import agent

    telemetry = agent._output_cap_telemetry(
        agent._ClaudeCLIClient("claude"),
        {"output_tokens": 1_800},
        1_800,
        provider_enforced=True,
    )
    assert telemetry["output_cap_mode"] == "provider_enforced"
    assert telemetry["output_cap_exceeded"] is False


def test_custom_block_cap_rejects_truncation_retry_before_second_provider_call(
    monkeypatch,
):
    """The 4k→16k retry must not slip past a block's own token ceiling."""
    import agent

    calls = []

    class Usage:
        input_tokens = 100
        output_tokens = 4_000
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    truncated = SimpleNamespace(usage=Usage(), stop_reason="max_tokens", model="fake")
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kwargs: calls.append(kwargs) or truncated
        )
    )
    monkeypatch.setattr(agent, "_get_client", lambda: client)
    # The first (truncated) response still carries real nonzero usage and is
    # ledger-recorded before the retry's admission check rejects it (Adım 9:
    # _create_message_once reads _ledger_record as llm_dispatch.py's own bare
    # module global now -- patching agent's re-exported copy would not reach
    # it and would silently write a real row to the developer's actual
    # ~/.cache/nautilus_web_app/token_usage.jsonl).
    monkeypatch.setattr("token_ledger.record", lambda *args, **kwargs: None)
    agent.set_thread_llm_control(lambda: False, None, None)
    try:
        with pytest.raises(agent.LLMTokenBudgetExceeded, match="cannot admit"):
            agent._call_claude_for_block(
                "short block description",
                role_hint="entry",
                token_limit=20_000,
            )
    finally:
        agent.set_thread_llm_control(None, None, None)
    assert len(calls) == 1


def test_cli_output_reservation_uses_observed_model_purpose_high_water(monkeypatch):
    """Claude CLI lacks max_tokens: later admissions reserve observed output."""
    import agent
    import llm_client

    # agent.py decomposition Adım 3: _observe_llm/_llm_request_token_bound now
    # live in llm_client.py and read _OBSERVED_OUTPUT_HIGH_WATER as a bare
    # global there -- patching agent's rebound name was inert (nothing reads
    # it), silently relying on whatever the real dict already held for this
    # key from earlier tests instead of starting clean.
    monkeypatch.setattr(llm_client, "_OBSERVED_OUTPUT_HIGH_WATER", {})
    agent._observe_llm(
        model="claude-fable-5",
        purpose="custom_block",
        usage={"output_tokens": 9_640},
    )

    bound = agent._llm_request_token_bound(
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": 4_000},
        model="claude-fable-5",
        purpose="custom_block",
    )

    assert bound["configured_output_tokens"] == 4_000
    assert bound["observed_output_reserve"] == 9_640
    assert bound["output_token_bound"] == 9_640
    assert bound["output_reservation_mode"] == "observed_high_water"


def test_phase_timeout_never_exceeds_remaining_global_deadline():
    import web.routes.agent_backtest as ab

    assert ab._remaining_phase_timeout(100.0, 1.0, 600.0, now=3_650.0) == 50.0
    assert ab._remaining_phase_timeout(100.0, 0.0, 600.0, now=9_999.0) == 600.0
    with pytest.raises(ab.AgentBudgetReached, match="time ceiling"):
        ab._remaining_phase_timeout(100.0, 1.0, 600.0, now=3_700.0)


def test_custom_batch_validator_failure_rolls_back_registry(monkeypatch, tmp_path):
    import json

    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "registry.json")
    store.save_custom("existing", {"role": "entry"}, "x = 1\n")

    def reject():
        raise RuntimeError("spec invalid")

    with pytest.raises(RuntimeError, match="spec invalid"):
        store.save_custom_batch(
            [{"name": "new_entry", "meta": {}, "code": "x = 2\n"}],
            validate=reject,
        )
    registry = json.loads(store.REGISTRY_FILE.read_text(encoding="utf-8"))
    assert set(registry) == {"existing"}
    assert not (tmp_path / "new_entry.py").exists()


def test_save_custom_registry_write_failure_rolls_back_a_new_file(
    monkeypatch, tmp_path
):
    """DeepR 2026-08-09 [DÜŞÜK]: save_custom (single-block) used to write the
    .py file, then separately read+write the registry with no rollback --
    unlike its save_custom_batch sibling above. A failure between the two
    left an orphan file with no registry entry."""
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "registry.json")

    def _boom(reg):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_registry", _boom)

    with pytest.raises(OSError, match="disk full"):
        store.save_custom("orphan_candidate", {}, "x = 1\n")

    assert not (tmp_path / "orphan_candidate.py").exists(), (
        "file was left on disk with no matching registry entry"
    )
    assert store.get_custom("orphan_candidate") is None


def test_save_custom_registry_write_failure_restores_the_previous_code(
    monkeypatch, tmp_path
):
    """Same failure, but overwriting an EXISTING block -- the file must come
    back to its pre-overwrite content, not stay half-updated."""
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "registry.json")
    store.save_custom("existing", {"role": "entry"}, "x = 1\n")

    def _boom(reg):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_registry", _boom)

    with pytest.raises(OSError, match="disk full"):
        store.save_custom("existing", {"role": "entry"}, "x = 2\n")

    assert (tmp_path / "existing.py").read_text(encoding="utf-8") == "x = 1\n"


def test_generate_custom_spec_registers_before_validation(monkeypatch, tmp_path):
    import agent
    import composer
    import custom_block_store as store
    import web.routes.agent_backtest as ab

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path / "sessions")
    monkeypatch.setattr(ab, "_enforce_token_budget", lambda run_id: None)
    monkeypatch.setattr(ab, "_add_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(ab, "_session_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent,
        "_propose_agent_strategy_idea",
        lambda *args, **kwargs: {
            "name": "Custom pair",
            "description": "pair",
            "entry_label": "Entry",
            "entry_desc": "entry",
            "exit_label": "Exit",
            "exit_desc": "exit",
        },
    )

    def block(label, description, role):
        returned = "long" if role == "entry" else "exit"
        return {
            "name": "ignored",
            "meta": {"label": label, "params": {}, "role": role},
            "code": (
                "def max_lookback(params):\n    return 2\n\n"
                "def evaluate(state, block, closes, indicators, portfolio):\n"
                f"    return {returned!r}\n"
            ),
            "usage": {},
        }

    monkeypatch.setattr(agent, "propose_custom_block", block)
    spec = ab._generate_custom_spec("runx", 1, "hint", [], round_num=1)
    try:
        assert spec is not None
        assert spec.validate() is None
        assert all(b.type in composer.BLOCK_REGISTRY for b in spec.blocks)
    finally:
        for name in ("agnt_e_runx_r1_1", "agnt_x_runx_r1_1"):
            composer.unregister_custom_block(name)
            store.delete_custom(name)


def test_legacy_agent_name_backfills_role_and_rejects_wrong_runtime_signal(
    monkeypatch, tmp_path
):
    """A legacy agnt_x file cannot become an entry signal after restart."""
    import composer
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    name = "agnt_x_deadbeef_1"  # legacy shape: no durable role metadata
    store.save_custom(
        name,
        {"label": "legacy", "params": {}},
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    return 'long'\n",
    )
    try:
        composer.register_custom_from_disk(name)
        assert store.get_custom(name)["meta"]["role"] == "exit"
        spec = composer.ComposedStrategySpec(
            id="legacy-role",
            name="legacy",
            description="",
            blocks=[composer.SignalBlock(type=name, role="entry", params={})],
        )
        assert "declared for role 'exit'" in (spec.validate() or "")

        strategy = SimpleNamespace(
            _prev_state={},
            _buf_cap=20,
            _indicators={},
            _volumes=[],
            _highs=[],
            _lows=[],
            portfolio=SimpleNamespace(
                is_net_long=lambda *_: False,
                is_net_short=lambda *_: False,
                is_flat=lambda *_: True,
            ),
        )
        block = SimpleNamespace(params={}, role="exit", type=name)
        assert (
            composer.BLOCK_REGISTRY[name]["eval"](strategy, 0, block, [100.0]) is None
        )
    finally:
        composer.unregister_custom_block(name)
        store.delete_custom(name)


def test_agent_run_cleanup_is_atomic_and_keeps_promoted_dependencies(
    monkeypatch, tmp_path
):
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    keep = "agnt_e_deadbeef_r1_1"
    drop = "agnt_x_deadbeef_r1_1"
    other = "agnt_e_cafebabe_r1_1"
    for name in (keep, drop, other):
        store.save_custom(name, {"params": {}}, "def evaluate():\n    return None\n")

    assert {item["name"] for item in store.list_agent_blocks("deadbeef")} == {
        keep,
        drop,
    }
    assert store.cleanup_agent_run("deadbeef", keep_names={keep}) == [drop]
    assert store.get_custom(keep) is not None
    assert store.get_custom(drop) is None
    assert store.get_custom(other) is not None


def test_delete_custom_batch_removes_files_and_registry_entries(monkeypatch, tmp_path):
    """delete_custom_batch's own atomic-multi-removal contract — previously
    only exercised indirectly (one real removal) through cleanup_agent_run's
    test above (DeepR 2026-08-09 [YÜKSEK]: sibling save_custom_batch has
    rollback coverage, this one had none)."""
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    store.save_custom("block_a", {"role": "entry"}, "x = 1\n")
    store.save_custom("block_b", {"role": "exit"}, "x = 2\n")
    store.save_custom("block_c", {"role": "entry"}, "x = 3\n")

    removed = store.delete_custom_batch(["block_a", "block_b"])

    assert set(removed) == {"block_a", "block_b"}
    assert store.get_custom("block_a") is None
    assert store.get_custom("block_b") is None
    assert not (tmp_path / "store" / "block_a.py").exists()
    assert not (tmp_path / "store" / "block_b.py").exists()
    assert store.get_custom("block_c") is not None  # untouched


def test_delete_custom_logs_when_the_in_memory_unregister_fails(
    monkeypatch, tmp_path, caplog
):
    """DeepR 2026-08-09 [DÜŞÜK]: delete_custom's in-memory unregister used to
    swallow a failure silently -- unlike delete_custom_batch's identical
    try/except right below it (log.exception). The disk-side delete has
    already committed by this point either way; what must not happen is
    losing the signal that BLOCK_REGISTRY is now stale for this name."""
    import composer
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "registry.json")
    store.save_custom("flaky", {"role": "entry"}, "x = 1\n")

    def _boom(name):
        raise RuntimeError("memory registry corrupted")

    monkeypatch.setattr(composer, "unregister_custom_block", _boom)

    with caplog.at_level("ERROR"):
        result = store.delete_custom("flaky")  # must not raise

    assert result is True
    assert store.get_custom("flaky") is None  # disk/registry side still committed
    assert "flaky" in caplog.text
    assert "unregister" in caplog.text.lower()


def test_delete_custom_batch_ignores_names_not_in_the_registry(monkeypatch, tmp_path):
    """Docstring contract: 'Missing names are harmless.'"""
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    store.save_custom("block_a", {"role": "entry"}, "x = 1\n")

    removed = store.delete_custom_batch(["block_a", "never_saved"])

    assert removed == ["block_a"]
    assert store.get_custom("block_a") is None


def test_delete_custom_batch_empty_input_is_a_no_op(monkeypatch, tmp_path):
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")

    assert store.delete_custom_batch([]) == []


def test_delete_custom_batch_rejects_an_invalid_name_without_deleting_anything(
    monkeypatch, tmp_path
):
    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    store.save_custom("block_a", {"role": "entry"}, "x = 1\n")

    with pytest.raises(ValueError, match="invalid custom block name"):
        store.delete_custom_batch(["block_a", "Not Valid!"])

    assert store.get_custom("block_a") is not None  # validation ran before any I/O


def test_delete_custom_batch_rolls_back_on_partial_failure(monkeypatch, tmp_path):
    """Mirrors test_custom_batch_validator_failure_rolls_back_registry above:
    a mid-transaction failure must restore both files and the registry, not
    leave one block deleted and its sibling intact (the exact "orphan" state
    cleanup_agent_run relies on this function to prevent)."""
    from pathlib import Path

    import custom_block_store as store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(store, "REGISTRY_FILE", tmp_path / "store" / "registry.json")
    store.save_custom("block_a", {"role": "entry"}, "x = 1\n")
    store.save_custom("block_b", {"role": "exit"}, "x = 2\n")

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "block_b.py":
            raise OSError("disk error deleting block_b")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(OSError, match="disk error deleting block_b"):
        store.delete_custom_batch(["block_a", "block_b"])

    assert store.get_custom("block_a") is not None
    assert store.get_custom("block_b") is not None
    assert (tmp_path / "store" / "block_a.py").read_text(encoding="utf-8") == "x = 1\n"


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


def test_wfo_requires_positive_excess_not_just_positive_pnl():
    import web.routes.agent_backtest as ab

    windows = [
        {
            "test_n_trades": 5,
            "test_metrics": {
                "pnl": 100.0,
                "excess_return_fraction": -0.01,
                "annualized_alpha": -0.01,
                "sharpe": 1.0,
            },
        }
        # Payda sınırı (WFO_MIN_VALID_WINDOWS=10): 4 pencere artık
        # 'yargılamaya yetmez' diye REDDEDİLİYOR. Bu testler WFO'yu
        # değil MC/kapsam ölçütlerini sınıyor, o yüzden sepet sınırın
        # üstüne çıkarıldı — iddia değişmedi, kurgu güncellendi.
        for _ in range(12)
    ]
    rob = {
        "split": {"overfitting_label": "✓ Robust"},
        "wfo_windows": windows,
        "oos_sharpe_penalized": 1.0,
        "mc": {"max_dd_p50": -10.0},
    }
    assert ab._robustness_passed(rob, strict=True) is False


def test_strict_mc_rejects_unsafe_drawdown_tail():
    import web.routes.agent_backtest as ab

    windows = [
        {
            "test_n_trades": 5,
            "test_metrics": {
                "excess_return_fraction": 0.01,
                "annualized_alpha": 0.01,
                "sharpe": 1.0,
            },
        }
        # Payda sınırı (WFO_MIN_VALID_WINDOWS=10): 4 pencere artık
        # 'yargılamaya yetmez' diye REDDEDİLİYOR. Bu testler WFO'yu
        # değil MC/kapsam ölçütlerini sınıyor, o yüzden sepet sınırın
        # üstüne çıkarıldı — iddia değişmedi, kurgu güncellendi.
        for _ in range(12)
    ]
    rob = {
        "split": {"overfitting_label": "✓ Robust"},
        "wfo_windows": windows,
        "multi_symbol": {"generalization_label": "✓ Generalizable"},
        "mc": {"max_dd_p50": -10.0, "max_dd_p95": -40.0},
    }
    assert ab._robustness_passed(rob, strict=True) is False
    assert ab._robustness_passed(rob, strict=False) is True


def test_strict_mc_requires_tail_metric():
    import web.routes.agent_backtest as ab

    windows = [
        {
            "test_n_trades": 5,
            "test_metrics": {
                "excess_return_fraction": 0.01,
                "annualized_alpha": 0.01,
                "sharpe": 1.0,
            },
        }
        # Payda sınırı (WFO_MIN_VALID_WINDOWS=10): 4 pencere artık
        # 'yargılamaya yetmez' diye REDDEDİLİYOR. Bu testler WFO'yu
        # değil MC/kapsam ölçütlerini sınıyor, o yüzden sepet sınırın
        # üstüne çıkarıldı — iddia değişmedi, kurgu güncellendi.
        for _ in range(12)
    ]
    rob = {
        "split": {"overfitting_label": "✓ Robust"},
        "wfo_windows": windows,
        "multi_symbol": {"generalization_label": "✓ Generalizable"},
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
    assert not ab._split_definitive_failure({"overfitting_label": "⚠ Caution"})


def test_holdout_promotion_requires_evidence_profit_sharpe_and_excess(monkeypatch):
    import web.routes.agent_backtest as ab

    monkeypatch.setattr(ab, "HOLDOUT_MIN_TRADES", 20)
    assert ab._holdout_promotion_verdict(20, 0.1, 0.5, 0.01) == (True, "passed")
    passed, reason = ab._holdout_promotion_verdict(19, 0.1, 0.5, 0.01)
    assert not passed and "19" in reason and "20" in reason
    passed, reason = ab._holdout_promotion_verdict(20, -0.1, 0.5, 0.01)
    assert not passed and "PnL" in reason
    passed, reason = ab._holdout_promotion_verdict(20, 0.1, None, 0.01)
    assert not passed and "Sharpe" in reason
    passed, reason = ab._holdout_promotion_verdict(20, 0.1, 0.5, 0.0)
    assert not passed and "buy-and-hold" in reason


def test_holdout_promotion_reason_respects_require_flags(monkeypatch):
    # Regression: the displayed reason must agree with the same flags that
    # gate the pass/fail boolean — previously the reason text hardcoded all
    # three checks as required regardless of the HOLDOUT_REQUIRE_* config.
    import web.routes.agent_backtest as ab

    monkeypatch.setattr(ab, "HOLDOUT_MIN_TRADES", 20)
    monkeypatch.setattr(ab, "HOLDOUT_REQUIRE_POSITIVE_SHARPE", False)
    passed, reason = ab._holdout_promotion_verdict(20, 0.1, None, 0.01)
    assert passed and reason == "passed"


def test_benchmark_is_stamped_and_score_uses_excess_return():
    import web.routes.agent_backtest as ab

    bars = pd.DataFrame({"close": [100.0, 120.0]})
    result = _result(pnl_pct=0.1)
    ab._stamp_buy_hold_benchmark(result, bars)
    assert result.metrics["benchmark_return_fraction"] == pytest.approx(0.2)
    assert result.metrics["excess_return_fraction"] == pytest.approx(-0.1)
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


def test_external_gap_report_fails_on_large_same_session_intraday_hole():
    import data

    idx = pd.to_datetime(
        ["2025-01-02 14:30", "2025-01-02 15:30", "2025-01-02 20:00"], utc=True
    )
    frame = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=idx)
    gap = data.external_data_gap_report(frame, granularity="1-HOUR")
    assert gap == {
        "kind": "intraday",
        "from": "2025-01-02T15:30:00+00:00",
        "to": "2025-01-02T20:00:00+00:00",
        "gap_minutes": 270,
        "max_allowed_minutes": 180,
    }


def test_metrics_thinning_reaches_nested_mtm_curve():
    import web.routes.agent_backtest as ab

    payload = {
        "equity_curve_mtm": [[str(i), float(i)] for i in range(10_000)],
        "n_trades": 30,
    }
    thin = ab._thin_curves(payload, cap=400)
    assert len(thin["equity_curve_mtm"]) == 400
    assert thin["n_trades"] == 30


def test_session_curve_cap_bounds_top_level_and_nested_curves():
    import web.routes.agent_backtest as ab

    assert ab._SESSION_CURVE_CAP == 120
    values = list(range(2_000))
    dates = [str(i) for i in values]
    curve, curve_dates = ab._thin_pair(values, dates, cap=ab._SESSION_CURVE_CAP)
    metrics = ab._thin_curves(
        {"equity_curve_mtm": [[str(i), float(i)] for i in values]},
        cap=ab._SESSION_CURVE_CAP,
    )
    assert len(curve) == len(curve_dates) == ab._SESSION_CURVE_CAP
    assert len(metrics["equity_curve_mtm"]) == ab._SESSION_CURVE_CAP
    assert curve[0] == 0 and curve[-1] == 1_999


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


def test_llm_observer_sees_each_actual_provider_response(monkeypatch):
    import agent

    # Real nonzero usage below would otherwise write a real row into the
    # developer's actual ~/.cache/nautilus_web_app/token_usage.jsonl (this
    # test drives the real agent._create_message_once (llm_dispatch.py,
    # Adım 9), which calls llm_dispatch._ledger_record, unmocked).
    monkeypatch.setattr("token_ledger.record", lambda *a, **k: None)

    events = []

    class Usage:
        input_tokens = 11
        output_tokens = 7
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    response = SimpleNamespace(usage=Usage(), model="fake", stop_reason=None)
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
    agent.set_thread_llm_control(lambda: False, events.append)
    try:
        assert agent._create_message_once(client, "probe", max_tokens=50) is response
    finally:
        agent.set_thread_llm_control(None, None)
    assert events[0]["usage"]["input_tokens"] == 11
    assert events[0]["purpose"] == "probe"
    assert events[0]["status"] == "ok"
    assert events[0]["output_cap_mode"] == "provider_enforced"


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


def test_create_message_once_applies_generic_timeout_for_non_cli_client():
    # The cross-backend safety-net default (NAUTILUS_LLM_CALL_TIMEOUT) must
    # still apply to a direct SDK/OpenRouter client — that path previously had
    # no timeout at all.
    import agent

    calls = []
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kwargs: (
                calls.append(kwargs),
                SimpleNamespace(usage=None, stop_reason=None, model="fake"),
            )[1]
        )
    )
    agent._create_message_once(client, "probe", max_tokens=50)
    assert calls[0]["timeout"] == pytest.approx(120.0)


def test_create_message_once_skips_generic_timeout_for_cli_client():
    # Regression: injecting the generic 120s default into the CLI client's
    # kwargs used to silently cap its own 300s ceiling (NAUTILUS_CLI_TIMEOUT)
    # via a min() in _ClaudeCLIMessages.create — a slow high-effort CLI call
    # that used to fit under 300s would now hit a spurious 120s timeout.
    import agent

    calls = []
    client = agent._ClaudeCLIClient("fake-claude-cli")
    client.messages.create = lambda **kwargs: (
        calls.append(kwargs),
        SimpleNamespace(usage=None, stop_reason=None, model="fake"),
    )[1]
    agent._create_message_once(client, "probe", max_tokens=50)
    assert "timeout" not in calls[0]


def test_cli_deadline_defaults_to_own_ceiling_not_generic_default(monkeypatch):
    import subprocess

    import agent

    seen = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(kw),
            SimpleNamespace(returncode=1, stdout="", stderr=""),
        )[1],
    )
    monkeypatch.delenv("NAUTILUS_CLI_TIMEOUT", raising=False)
    with pytest.raises(Exception):
        agent._ClaudeCLIMessages("claude").create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
        )
    assert seen["timeout"] == pytest.approx(300.0)


def test_openrouter_client_default_retries_matches_sdk_default(monkeypatch):
    # Regression: this default used to be silently 0 (no retry), turning a
    # transient connection blip or 502/503 into a user-visible failure on the
    # synchronous (non-AUTO) call path, which has no other retry/backoff.
    import openai

    import agent

    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("NAUTILUS_OPENROUTER_SDK_RETRIES", raising=False)
    agent._build_openrouter_client()
    assert captured["max_retries"] == 2


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


def test_cli_response_preserves_cli_reported_cost():
    import agent

    response = agent._CLIResponse(
        "ok",
        {"input_tokens": 2, "output_tokens": 3},
        cost_usd=0.1234,
    )
    assert response.usage.cost_usd == pytest.approx(0.1234)
    assert agent._usage_dict(response)["cost_usd"] == pytest.approx(0.1234)


def test_session_summary_prefers_completed_rounds_over_token_snapshot(
    monkeypatch, tmp_path
):
    import json

    import web.routes.sessions as sessions

    monkeypatch.setattr(sessions, "SESSION_LOG_DIR", tmp_path)
    sessions._SUMMARY_CACHE.clear()
    events = [
        {
            "ts": "2026-08-06T12:00:00+00:00",
            "event": "session_start",
            "run_id": "counter",
        },
        {
            "ts": "2026-08-06T12:10:00+00:00",
            "event": "token_snapshot",
            "run_id": "counter",
            "round": 3,
        },
        {
            "ts": "2026-08-06T12:10:01+00:00",
            "event": "session_end",
            "run_id": "counter",
            "round": 3,
            "started_round": 3,
            "completed_rounds": 2,
            "total_rounds": 2,
            "outcome": "budget",
        },
    ]
    (tmp_path / "counter.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    assert sessions._session_summary("counter")["total_rounds"] == 2


def test_large_session_summary_reads_bounded_head_and_tail(monkeypatch, tmp_path):
    import json

    import web.routes.sessions as sessions

    monkeypatch.setattr(sessions, "SESSION_LOG_DIR", tmp_path)
    monkeypatch.setattr(sessions, "_SESSION_SUMMARY_FULL_SCAN_MAX_BYTES", 128)
    sessions._SUMMARY_CACHE.clear()
    path = tmp_path / "large001.jsonl"
    head = json.dumps(
        {
            "ts": "2026-08-06T12:00:00+00:00",
            "event": "session_start",
            "symbol": "QQQ.NASDAQ",
            "n_iterations": 4,
        }
    )
    tail = json.dumps(
        {
            "ts": "2026-08-06T12:12:00+00:00",
            "event": "session_end",
            "outcome": "budget",
            "completed_rounds": 2,
        }
    )
    path.write_text(head + "\n" + ("x" * 512) + "\n" + tail + "\n", encoding="utf-8")

    summary = sessions._session_summary("large001")
    assert summary["symbol"] == "QQQ.NASDAQ"
    assert summary["outcome"] == "budget"
    assert summary["total_rounds"] == 2
    assert summary["n_backtest"] == "?"


def test_openrouter_auto_path_uses_killable_process(monkeypatch):
    import agent
    import openrouter_backend

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

    monkeypatch.setattr(openrouter_backend, "_run_openrouter_killable", fake_run)
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


def _budget_state(run_id, monkeypatch, **over):
    import web.routes.agent_backtest as ab

    logged: list[dict] = []
    monkeypatch.setattr(
        ab, "_session_log", lambda _r, kind, **kw: logged.append({"kind": kind, **kw})
    )
    state = {
        "tokens_in": 10,
        "tokens_out": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "max_total_tokens": 1_000_000,
        "provider_cost_usd": 0.0,
        **over,
    }
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = state
    return ab, logged


def test_admission_stops_spending_once_the_money_cap_is_reached(monkeypatch):
    """`_budget_breach` parayı zaten sayıyordu — ama TURLAR ARASI. Bir turun
    içinde onlarca sağlayıcı çağrısı var ve giriş kontrolü yalnız token'a
    bakıyordu; tavan aşıldıktan sonra koşu turun sonuna kadar harcamaya devam
    edebiliyordu. Tavan, aşıldığını öğrendiğin an değil, bir sonraki harcamayı
    durdurduğun an tavandır."""
    run_id = "cost-cap-reached"
    ab, logged = _budget_state(
        run_id, monkeypatch, max_total_cost_usd=5.0, provider_cost_usd=5.01
    )
    try:
        with pytest.raises(ab.AgentBudgetReached, match=r"cost ceiling"):
            ab._admit_llm_budget(run_id, {"total_token_bound": 15})
        assert any(e["kind"] == "llm_budget_rejected" for e in logged), (
            "reddedilen çağrı deftere yazılmadı"
        )
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)


def test_admission_lets_a_run_under_the_money_cap_through(monkeypatch):
    run_id = "cost-cap-ok"
    ab, _logged = _budget_state(
        run_id, monkeypatch, max_total_cost_usd=5.0, provider_cost_usd=1.25
    )
    try:
        ab._admit_llm_budget(run_id, {"total_token_bound": 15})  # yükselmemeli
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)


def test_a_blind_run_is_not_stopped_by_an_unmeasurable_cost(monkeypatch):
    """Maliyet hiçbir modelde okunamıyorsa para tavanı tetiklenemez; o durumda
    tek koruma token tavanıdır (bkz. `_budget_breach` körlük şartı). Giriş
    kontrolü de aynı şeyi yapmalı — 0.0'ı "tavan aşıldı" saymak, ölçemediğimiz
    için koşuyu kesmek olurdu."""
    run_id = "cost-cap-blind"
    ab, _logged = _budget_state(run_id, monkeypatch, max_total_cost_usd=0.01)
    try:
        ab._admit_llm_budget(run_id, {"total_token_bound": 15})
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
        {"blocks": [{"type": "momentum", "role": "entry", "params": {"lookback": 5}}]}
    )
    second = ab._proposal_to_spec(
        {"blocks": [{"type": "momentum", "role": "entry", "params": {"lookback": 20}}]}
    )
    assert ab._candidate_fingerprint(first) != ab._candidate_fingerprint(second)
    assert ab._candidate_fingerprint(first, family=True) == ab._candidate_fingerprint(
        second, family=True
    )


def test_candidate_fingerprint_canonicalizes_commutative_block_order():
    import web.routes.agent_backtest as ab

    a = ab._proposal_to_spec(
        {
            "entry_logic": "AND",
            "exit_logic": "OR",
            "blocks": [
                {"type": "momentum", "role": "entry", "params": {"lookback": 10}},
                {"type": "rsi", "role": "entry", "params": {"period": 14}},
                {
                    "type": "atr_stop",
                    "role": "exit",
                    "params": {"period": 14, "mult": 3},
                },
                {"type": "take_profit", "role": "exit", "params": {"percent": 5}},
            ],
        }
    )
    b = ab._proposal_to_spec(
        {
            "entry_logic": "AND",
            "exit_logic": "OR",
            "blocks": [
                {"type": "take_profit", "role": "exit", "params": {"percent": 5}},
                {"type": "rsi", "role": "entry", "params": {"period": 14}},
                {
                    "type": "atr_stop",
                    "role": "exit",
                    "params": {"period": 14, "mult": 3},
                },
                {"type": "momentum", "role": "entry", "params": {"lookback": 10}},
            ],
        }
    )
    assert ab._candidate_fingerprint(a) == ab._candidate_fingerprint(b)
    assert ab._candidate_fingerprint(a, family=True) == ab._candidate_fingerprint(
        b, family=True
    )


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
    # Testin amacı "0 gönderince SONSUZ bütçe oluşmasın" — değer değişti, amaç
    # değil. 2026-08-15'ten beri iki tavan var: token artık faturanın vekili
    # değil kaçak-döngü emniyeti, parayı ayrı bir tavan sınırlıyor. Sayıyı
    # sabitlemek yerine sözleşmeyi sabitliyoruz.
    assert captured["max_total_tokens"] == ab._continuous_token_cap()
    assert 0 < captured["max_total_tokens"] < float("inf")
    # Asıl fatura koruması: para tavanı gerçekten devrede olmalı.
    assert ab._default_cost_cap() > 0


def test_blank_budget_boxes_behave_like_zero(monkeypatch):
    """AUTO brief'i MAX HOURS/TOKENS kutularını BOŞ açıyor.

    Boş bir number input'u "" gönderir; alanlar `float`/`int` olarak
    bildirildiğinde bu 422 idi, yani START hiçbir koşu başlatmadan düşüyordu.
    Boş = "formdan tavan koyma" (0 ile aynı yol), bozuk değer ise 400.
    """
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
    # Sahte thread koşmadığı için aldığı AUTO slotunu asla bırakmaz; taze bir
    # semafor olmadan bu test, aynı numarayı yapan komşusundan sonra 429 alır
    # (ve kendi sızıntısını sonrakine bırakır).
    monkeypatch.setattr(
        ab,
        "_AUTO_RUN_SLOTS",
        ab.threading.BoundedSemaphore(ab._MAX_CONCURRENT_AUTO_RUNS),
    )
    client = TestClient(app)
    response = client.post(
        "/agent/run",
        data={"continuous": "1", "max_hours": "", "max_total_tokens": ""},
    )
    assert response.status_code == 200
    assert captured["max_hours"] == ab.DEFAULT_CONTINUOUS_MAX_HOURS
    assert captured["max_total_tokens"] == ab._continuous_token_cap()

    bad = client.post("/agent/run", data={"max_hours": "iki saat"})
    assert bad.status_code == 400
    assert "invalid budget" in bad.text


def test_negative_budget_is_rejected_not_silently_maximized(monkeypatch):
    """NEGATİF de bozuktur — ve sessiz sonucu tam olarak korkulan şeydi.

    `float("-1")` istisna atmaz, "0 = güvenli azami" yolu ise `> 0` diye bakar:
    negatif bir değer kullanıcının yazdığı tavanı kaldırıp koşuyu SERT TAVANA
    kadar açıyordu. Tarayıcı `min=` ile engelliyor ama bu kapı zaten elle
    kurulmuş istekler için var.
    """
    from fastapi.testclient import TestClient

    import web.routes.agent_backtest as ab
    from server import app

    started = []

    class ImmediateThread:
        def __init__(self, *, target, kwargs, daemon):
            started.append(kwargs)

        def start(self):
            return None

    monkeypatch.setattr(ab.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        ab,
        "_AUTO_RUN_SLOTS",
        ab.threading.BoundedSemaphore(ab._MAX_CONCURRENT_AUTO_RUNS),
    )
    client = TestClient(app)

    for payload in ({"max_hours": "-1"}, {"max_total_tokens": "-5000"}):
        bad = client.post("/agent/run", data=payload)

        assert bad.status_code == 400, payload
        assert "invalid budget" in bad.text
    assert not started, "reddedilen bütçeyle koşu başlatıldı"


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


def test_session_log_failure_marks_live_audit_degraded(tmp_path, monkeypatch):
    """An unavailable audit directory must be visible without stopping AUTO."""
    import web.routes.agent_backtest as ab

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    run_id = "deadbeef"
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", blocked)
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = {
            "audit_degraded": False,
            "audit_error": None,
        }
    try:
        ab._session_log(run_id, "step", msg="audit probe")
        with ab._AGENT_LOCK:
            state = ab._AGENT_PROGRESS[run_id]
            assert state["audit_degraded"] is True
            assert "Session audit log unavailable" in state["audit_error"]
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(run_id, None)
