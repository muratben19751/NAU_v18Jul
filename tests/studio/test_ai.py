import json
import os

import pytest
from fastapi.testclient import TestClient

SID = "wt-funding-v3"


def _sugg(kind="modify_risk", block="risk", diff=None, rationale="tighten TP"):
    return json.dumps({
        "kind": kind, "block": block,
        "diff": diff if diff is not None else
        {"name": "take_profit_r", "value": 2.2},
        "rationale": rationale,
        "expected": {"dsr_delta": 0.05},
    })


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from server import app as _host
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main
    store = StrategyStore(tmp_path / "t.db")
    store.save(build_fixture())
    monkeypatch.setattr(main, "store", store)
    c = TestClient(_host)
    c.store = store
    c.main = main
    return c


def _mock_llm(client, monkeypatch, responses):
    from strategy_studio.ai import MockLLMClient
    llm = MockLLMClient(responses)
    monkeypatch.setattr(client.main, "LLM", llm)
    return llm


def test_suggest_flow_creates_ghost(client, monkeypatch):
    _mock_llm(client, monkeypatch, [_sugg()])
    r = client.post(f"/studio/{SID}/ai/suggest", data={"ask": "improve"})
    assert r.status_code == 200
    assert "ghost-" in r.text and "Accept" in r.text
    assert 'hx-swap-oob="true"' in r.text          # oob into risk block
    pending = client.store.pending_suggestions(SID)
    assert len(pending) == 1 and pending[0]["block"] == "risk"
    assert pending[0]["trial_metrics"]             # trial backtest ran
    page = client.get(f"/studio/{SID}")
    assert "ghost-" in page.text                   # ghost survives reload


def test_malformed_llm_retry_then_valid(client, monkeypatch):
    llm = _mock_llm(client, monkeypatch, ["not json at all", _sugg()])
    r = client.post(f"/studio/{SID}/ai/suggest", data={})
    assert r.status_code == 200
    assert len(llm.prompts) == 2                   # exactly one retry
    assert "invalid" in llm.prompts[1]


def test_malformed_llm_twice_fails_cleanly(client, monkeypatch):
    _mock_llm(client, monkeypatch, ["garbage", "still garbage"])
    r = client.post(f"/studio/{SID}/ai/suggest", data={})
    assert r.status_code == 422 and "invalid suggestion" in r.text


def test_block_scope_overrides_llm_block(client, monkeypatch):
    _mock_llm(client, monkeypatch, [_sugg(block="entry")])
    client.post(f"/studio/{SID}/ai/suggest", data={"block": "risk"})
    assert client.store.pending_suggestions(SID)[0]["block"] == "risk"


def test_guardrail_rejects_invalid_change(client, monkeypatch):
    _mock_llm(client, monkeypatch, [
        _sugg(kind="add_rule", block="entry",
              diff={"indicator": "no_such_indicator"})])
    r = client.post(f"/studio/{SID}/ai/suggest", data={})
    assert r.status_code == 422 and "guardrails" in r.text
    row = client.store.pending_suggestions(SID)
    assert row == []                               # nothing left in review


def test_accept_applies_to_draft(client, monkeypatch):
    _mock_llm(client, monkeypatch, [_sugg()])
    client.post(f"/studio/{SID}/ai/suggest", data={})
    sid = client.store.pending_suggestions(SID)[0]["id"]
    r = client.post(f"/studio/{SID}/ai/suggestions/{sid}/accept")
    assert r.status_code == 200
    d = client.store.load_draft(SID)
    assert d.risk.take_profit_r.value == 2.2
    assert client.store.get_suggestion(sid)["status"] == "accepted"


def test_dismiss_feeds_rejected_context(client, monkeypatch):
    _mock_llm(client, monkeypatch, [_sugg(rationale="widen stops")])
    client.post(f"/studio/{SID}/ai/suggest", data={})
    sid = client.store.pending_suggestions(SID)[0]["id"]
    client.post(f"/studio/{SID}/ai/suggestions/{sid}/dismiss")
    assert client.store.get_suggestion(sid)["status"] == "rejected"
    assert "widen stops" in client.store.rejected_rationales(SID)
    llm = _mock_llm(client, monkeypatch, [_sugg()])
    client.post(f"/studio/{SID}/ai/suggest", data={})
    assert "widen stops" in llm.prompts[0]         # fed back into the prompt


def test_loop_auto_mode_accepts_and_records(client, monkeypatch):
    _mock_llm(client, monkeypatch, [
        _sugg(diff={"name": "take_profit_r", "value": 2.0}),
        _sugg(diff={"name": "take_profit_r", "value": 2.4}),
    ])
    r = client.post(f"/studio/{SID}/ai/loop/start",
                    data={"max_iterations": "2", "min_trades": "0",
                          "apply_mode": "auto"})
    assert r.status_code == 200
    loop = client.store.latest_loop(SID)
    assert loop["status"] == "done"
    iters = client.store.iterations(SID)
    assert len(iters) == 2
    assert all(i["decision"] in ("accepted", "rejected") for i in iters)
    accepted = [i for i in iters if i["decision"] == "accepted"]
    if accepted:                                   # draft carries the change
        d = client.store.load_draft(SID)
        assert d.risk.take_profit_r.value in (2.0, 2.4)


def test_loop_review_mode_pauses_on_first_review(client, monkeypatch):
    _mock_llm(client, monkeypatch, [_sugg(), _sugg()])
    client.post(f"/studio/{SID}/ai/loop/start",
                data={"max_iterations": "5", "min_trades": "0",
                      "apply_mode": "review"})
    loop = client.store.latest_loop(SID)
    assert loop["status"] == "done" and "paused for review" in loop["note"]
    assert len(client.store.iterations(SID)) == 1
    assert len(client.store.pending_suggestions(SID)) == 1


def test_loop_guardrail_autoreject_even_in_auto_mode(client, monkeypatch):
    from web.routes import strategy_studio as main

    class NoTrades:
        def run(self, compiled):
            m = main.StubBacktestAdapter().run(compiled)
            m.trades = 5
            return m

    # AI trials run on TRIAL_ADAPTER, not the single-run ADAPTER.
    monkeypatch.setattr(main, "TRIAL_ADAPTER", NoTrades())
    _mock_llm(client, monkeypatch, [_sugg()])
    client.post(f"/studio/{SID}/ai/loop/start",
                data={"max_iterations": "1", "min_trades": "100",
                      "apply_mode": "auto"})
    iters = client.store.iterations(SID)
    assert iters[0]["decision"] == "rejected"
    assert "below guardrail" in iters[0]["note"]
    assert client.store.load_draft(SID) is None    # nothing applied


def test_double_loop_blocked_while_running(client, monkeypatch):
    # first loop stays 'running' because bg tasks run after the response;
    # fire both requests before any bg task by using a raw route call
    _mock_llm(client, monkeypatch, [_sugg()])
    from fastapi import BackgroundTasks
    BackgroundTasks()
    client.main.route_loop_start.__wrapped__ if hasattr(
        client.main.route_loop_start, "__wrapped__") else None
    # simpler: create a running loop row directly
    client.store.create_loop("busyloop", SID, "{}")
    r = client.post(f"/studio/{SID}/ai/loop/start",
                    data={"max_iterations": "1"})
    assert r.status_code == 422 and "already running" in r.text


@pytest.mark.skipif(os.environ.get("STUDIO_LLM_SMOKE") != "1",
                    reason="live LLM smoke test disabled")
def test_live_llm_smoke(client):
    r = client.post(f"/studio/{SID}/ai/suggest", data={"ask": "improve"})
    assert r.status_code in (200, 422)
