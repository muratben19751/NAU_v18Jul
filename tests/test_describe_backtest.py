"""/backtest/describe — generate custom Python block from description → chain to /backtest/run.

Does NOT touch Claude or disk: propose_custom_block / save_custom /
register_custom_from_disk / append_to_catalog are mocked. Wiring verified:
entry+exit generation, block registration, spec compilation, and the chain
carrying instrument parameters + the new spec_id.
"""

from __future__ import annotations

import json
import re
import time

import pytest

_VALID_BLOCK = {
    "name": "x",
    "meta": {
        "label": "T",
        "params": {"period": {"type": "int", "min": 2, "max": 50, "default": 14}},
    },
    "code": (
        "def max_lookback(params):\n"
        "    return 5\n\n"
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    return None\n"
    ),
}


@pytest.fixture
def wired(monkeypatch):
    """Cut LLM + disk + catalog side effects; record calls."""
    import agent
    import composer
    import custom_block_store

    calls: list[str] = []
    saved: list[str] = []
    appended: list = []

    def _propose(label, desc, role_hint="entry"):
        calls.append(role_hint)
        return dict(_VALID_BLOCK)

    def _register(name):
        # Behave like the real register: let spec.validate() recognize the block type.
        composer.BLOCK_REGISTRY[name] = {
            "meta": _VALID_BLOCK["meta"],
            "eval": lambda *a, **k: None,
            "max_lookback": lambda p: 5,
            "builtin": False,
        }

    # Default: breakdown fails → worker falls back to the single entry+exit path
    # (behavior expected by existing tests). The multi-block test overrides this.
    def _no_breakdown(desc):
        raise ValueError("no breakdown in this test")

    monkeypatch.setattr(agent, "propose_condition_breakdown", _no_breakdown)
    monkeypatch.setattr(agent, "propose_custom_block", _propose)
    monkeypatch.setattr(
        custom_block_store, "save_custom", lambda n, m, c, prompt="": saved.append(n)
    )
    monkeypatch.setattr(composer, "register_custom_from_disk", _register)
    monkeypatch.setattr(composer, "append_to_catalog", appended.append)
    return {"calls": calls, "saved": saved, "appended": appended}


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _poll(c, gen_id, tries=60):
    for _ in range(tries):
        p = c.get(f"/backtest/describe/progress/{gen_id}")
        if "empty-state" in p.text or 'hx-post="/backtest/run"' in p.text:
            return p
        time.sleep(0.1)
    raise AssertionError("generation did not finish")


def _poll_chain(c, gen_id, tries=60):
    """The chain may be /run OR /sweep (single/multi TF) — expect either."""
    for _ in range(tries):
        p = c.get(f"/backtest/describe/progress/{gen_id}")
        if (
            "empty-state" in p.text
            or 'hx-post="/backtest/run"' in p.text
            or 'hx-post="/backtest/sweep"' in p.text
        ):
            return p
        time.sleep(0.1)
    raise AssertionError("generation did not finish")


class TestDescribeBacktest:
    def test_short_description_rejected_without_calling_claude(self, wired):
        r = _client().post("/backtest/describe", data={"description": "short"})
        assert r.status_code == 400
        assert not wired["calls"], "LLM must not be called for a short description"

    def test_generates_entry_and_exit_then_chains_to_run(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "buy when RSI is below 30, exit with ATR 3x stop",
                "instrument_kind": "Bybit",
                "symbol": "ETHUSDT",
                "category": "linear",
                "interval": "15",
            },
        )
        assert r.status_code == 200
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)

        assert wired["calls"] == ["entry", "exit"]
        assert len(wired["saved"]) == 2  # both blocks written to disk
        spec = wired["appended"][0]  # catalog spec (re-runnable)
        assert [b.role for b in spec.blocks] == ["entry", "exit"]

        # Chain: NEW spec + SAME instrument settings to /backtest/run
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["spec_id"] == spec.id
        assert vals["symbol"] == "ETHUSDT"
        assert vals["interval"] == "15"
        assert vals["instrument_kind"] == "Bybit"

    def test_generation_failure_does_not_chain_a_backtest(self, wired, monkeypatch):
        import agent
        from codegate import GeneratedCodeError

        def _boom(label, desc, role_hint="entry"):
            raise GeneratedCodeError("could not generate block")

        monkeypatch.setattr(agent, "propose_custom_block", _boom)
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "a nonsense description but long enough"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)
        # Error → backtest is NOT CHAINED (a wrong strategy must not silently run).
        assert 'hx-post="/backtest/run"' not in page.text
        assert (
            "Generation failed" in page.text
            or "could be generated" in page.text
            or "GeneratedCodeError" in page.text
        )


class TestDescribeMultiBlock:
    """propose_condition_breakdown → SEPARATE block per condition + block-level OR/AND."""

    def _breakdown(self, entry_logic="OR", exit_logic="OR"):
        def _bd(desc):
            return {
                "label": "Confluence",
                "entry_logic": entry_logic,
                "exit_logic": exit_logic,
                "conditions": [
                    {"role": "entry", "label": "RSI oversold", "desc": "RSI<30 up"},
                    {"role": "entry", "label": "Volume spike", "desc": "vol>2x avg"},
                    {"role": "exit", "label": "ATR stop", "desc": "3x ATR"},
                ],
                "usage": {},
            }

        return _bd

    def test_breakdown_makes_one_block_per_condition_and_keeps_logic(
        self, wired, monkeypatch
    ):
        import agent

        monkeypatch.setattr(
            agent, "propose_condition_breakdown", self._breakdown(entry_logic="AND")
        )
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "buy when RSI below 30 AND volume 2x; exit with ATR stop",
                "symbol": "ETHUSDT",
                "interval": "15",
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll(c, gen_id)

        spec = wired["appended"][0]
        roles = [b.role for b in spec.blocks]
        assert roles.count("entry") == 2 and roles.count("exit") == 1
        # Block-level logic is preserved from the breakdown.
        assert spec.entry_logic == "AND"
        assert spec.exit_logic == "OR"
        # 3 SEPARATE blocks written to disk (not embedded into a single block).
        assert len(wired["saved"]) == 3
        # Chain runs the new spec.
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["spec_id"] == spec.id

    def test_block_names_unique_across_roles(self, wired, monkeypatch):
        # "entry"[0]=="exit"[0]=="e" trap: entry#1 and exit#1 must NOT get the same name.
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", self._breakdown())
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "two entry one exit description long enough"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, gen_id)

        names = wired["saved"]
        assert len(names) == len(set(names)), f"block names collided: {names}"
        spec = wired["appended"][0]
        types = [b.type for b in spec.blocks]
        assert len(types) == len(set(types)), f"type collision in spec: {types}"

    def test_breakdown_failure_falls_back_to_single_pair(self, wired, monkeypatch):
        import agent

        def _boom(desc):
            raise ValueError("no breakdown")

        monkeypatch.setattr(agent, "propose_condition_breakdown", _boom)
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={"description": "single-condition description but long enough"},
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        _poll(c, gen_id)
        spec = wired["appended"][0]
        roles = [b.role for b in spec.blocks]
        assert roles == ["entry", "exit"]  # old 1+1 behavior preserved


class TestDescribeMultiTF:
    """Unified panel: description + multi-TF checkbox → 2+ TF sweep, 1 TF full run."""

    def test_multi_tf_chains_to_sweep_with_csv(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "buy when RSI is below 30, exit with ATR 3x stop",
                "symbol": "ETHUSDT",
                "category": "linear",
                "intervals": ["15", "60", "240"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        # 2+ TF → sweep comparison table, NOT /run.
        assert 'hx-post="/backtest/sweep"' in page.text
        assert 'hx-post="/backtest/run"' not in page.text
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["intervals"] == ["15", "60", "240"]
        # csv fallback: even if hx-vals array encoding does not hold, /sweep reads this.
        assert vals["intervals_csv"] == "15,60,240"
        assert vals["symbol"] == "ETHUSDT"

    def test_single_tf_chains_to_run(self, wired):
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "buy when RSI is below 30, exit with ATR 3x stop",
                "symbol": "BTCUSDT",
                "intervals": ["240"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        # Single TF → full result (/run), NOT sweep.
        assert 'hx-post="/backtest/run"' in page.text
        assert 'hx-post="/backtest/sweep"' not in page.text
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["interval"] == "240"

    def test_invalid_tf_codes_dropped(self, wired):
        # Invalid code ("99") is dropped; remaining 2 valid TFs → sweep.
        c = _client()
        r = c.post(
            "/backtest/describe",
            data={
                "description": "buy when RSI is below 30, exit with ATR 3x stop",
                "intervals": ["99", "15", "60"],
            },
        )
        gen_id = re.search(r"describe/progress/([0-9a-f]+)", r.text).group(1)
        page = _poll_chain(c, gen_id)
        vals = json.loads(re.search(r"hx-vals='([^']+)'", page.text).group(1))
        assert vals["intervals"] == ["15", "60"]
        assert 'hx-post="/backtest/sweep"' in page.text
