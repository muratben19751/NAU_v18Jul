"""Backtest screen — live 'Plan preview' sub-box smoke tests.

propose_condition_breakdown makes a REAL LLM call; following the
test_describe_backtest.py pattern it is monkeypatched (Claude untouched). The
endpoint is read-only: it produces/saves/backtests nothing.
"""

from __future__ import annotations


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


def _breakdown(entry_logic="OR", n_entry=2):
    """Fake RSI + Volume based breakdown producer."""
    conds = [
        {"role": "entry", "label": "RSI oversold", "desc": "RSI<30 turns up"},
        {"role": "entry", "label": "Volume spike", "desc": "volume 2x average"},
    ][:n_entry]
    # third entry for 3+ AND test
    while len(conds) < n_entry:
        conds.append(
            {"role": "entry", "label": f"Condition {len(conds)}", "desc": "extra RSI filter"}
        )
    conds.append({"role": "exit", "label": "ATR stop", "desc": "3x ATR"})

    def _bd(desc):
        return {
            "label": "RSI + Volume",
            "entry_logic": entry_logic,
            "exit_logic": "OR",
            "conditions": conds,
            "usage": {},
        }

    return _bd


class TestPlanPreview:
    def test_short_desc_returns_gentle_prompt_no_llm(self, monkeypatch):
        import agent

        called = []
        monkeypatch.setattr(
            agent,
            "propose_condition_breakdown",
            lambda d: called.append(1) or _breakdown()(d),
        )
        r = _client().post("/backtest/plan", data={"description": "kısa"})
        assert r.status_code == 200
        assert not called  # no LLM call on short input
        assert "a bit more detail" in r.text

    def test_plan_renders_turkish_fragment(self, monkeypatch):
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", _breakdown())
        r = _client().post(
            "/backtest/plan",
            data={"description": "RSI 30 altında ve hacim 2x iken al, ATR stop ile çık"},
        )
        assert r.status_code == 200
        assert "Plan preview" in r.text
        assert "ENTRY" in r.text and "EXIT" in r.text
        # RSI condition should resemble built-in rsi_threshold
        assert "RSI Threshold" in r.text

    def test_and_3plus_warning(self, monkeypatch):
        import agent

        monkeypatch.setattr(
            agent, "propose_condition_breakdown", _breakdown(entry_logic="AND", n_entry=3)
        )
        r = _client().post(
            "/backtest/plan",
            data={"description": "RSI<30 VE hacim 2x VE ADX>25 iken al, ATR ile çık"},
        )
        assert r.status_code == 200
        # Language-independent: 3+ AND warning produces a warning-strip; "AND" token is fixed.
        assert "warning-strip" in r.text
        assert "AND" in r.text

    def test_equity_target_size_precision_warning(self, monkeypatch):
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", _breakdown())
        r = _client().post(
            "/backtest/plan",
            data={
                "description": "RSI 30 altında al, ATR stop ile çık",
                "instrument_kind": "Index",
                "ticker": "QQQ",
            },
        )
        assert r.status_code == 200
        assert "size_precision=0" in r.text

    def test_llm_failure_degrades_gracefully(self, monkeypatch):
        import agent

        def _boom(d):
            raise ValueError("no breakdown")

        monkeypatch.setattr(agent, "propose_condition_breakdown", _boom)
        r = _client().post(
            "/backtest/plan",
            data={"description": "RSI tabanlı bir strateji ama yeterince uzun"},
        )
        assert r.status_code == 200
        assert "local estimate" in r.text  # fallback heading
        assert "ENTRY" in r.text  # entry/exit rows still render

    def test_plan_persists_nothing(self, monkeypatch):
        import agent
        import composer
        import custom_block_store

        saved: list = []
        appended: list = []
        monkeypatch.setattr(agent, "propose_condition_breakdown", _breakdown())
        monkeypatch.setattr(
            custom_block_store, "save_custom", lambda *a, **k: saved.append(1)
        )
        monkeypatch.setattr(composer, "append_to_catalog", appended.append)
        _client().post(
            "/backtest/plan",
            data={"description": "RSI 30 altında al ATR ile çık"},
        )
        assert not saved and not appended  # read-only: writes nothing to disk/catalog

    def test_allow_short_off_warns_on_short_description(self, monkeypatch):
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", _breakdown())
        # allow_short omitted → OFF; description implies short → warning present
        r = _client().post(
            "/backtest/plan",
            data={"description": "üst bantta short/sat, alt bantta al"},
        )
        assert r.status_code == 200
        assert "MARGIN" in r.text  # the allow_short-off warning mentions MARGIN

    def test_allow_short_on_suppresses_short_warning(self, monkeypatch):
        import agent

        monkeypatch.setattr(agent, "propose_condition_breakdown", _breakdown())
        r = _client().post(
            "/backtest/plan",
            data={"description": "üst bantta short/sat, alt bantta al", "allow_short": "on"},
        )
        assert r.status_code == 200
        # With shorts enabled, the short/MARGIN warning must not appear.
        assert "MARGIN" not in r.text

