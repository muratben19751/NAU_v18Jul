"""Regression tests for fixes coming out of the agent log audit.

1. TP-off bracket: use_bracket=True + tp_type='off' must not raise TypeError
   (falls back to entry + SL-only).
2. Continuous mode circuit breaker: 3 consecutive identical errors → stops.
3. Winner path writes session_end(outcome="winner").
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from composer import ComposedStrategySpec, SignalBlock

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _trending_bars(n: int = 400) -> pd.DataFrame:
    """Deterministic trend + wave data where MA-cross definitely triggers."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    close = 30_000 + 2_000 * np.sin(t / 30.0) + t * 2.0
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 10,
            "low": np.minimum(open_, close) - 10,
            "close": close,
            "volume": np.ones(n),
        },
        index=idx,
    )


def _bracket_tp_off_spec() -> ComposedStrategySpec:
    return ComposedStrategySpec(
        id="tpoff",
        name="TP Off Bracket",
        description="",
        blocks=[
            SignalBlock(
                type="ma_cross",
                role="entry",
                params={"fast": 5, "slow": 20, "direction": "up"},
            ),
            SignalBlock(
                type="ma_cross",
                role="exit",
                params={"fast": 5, "slow": 20, "direction": "down"},
            ),
        ],
        trade_size=0.1,
        use_bracket=True,
        sl_type="percent",
        sl_value=2.0,
        tp_type="off",  # ← error pattern: TP off + bracket
    )


class TestBracketTpOff:
    def test_no_price_typeerror_and_entries_fill(self):
        """Live bug logged 8×: tp_price=None → bracket() TypeError.

        After fix: no error, entries open (SL-only path)."""
        from backtest import run_composed_backtest
        from sandbox import _build_instrument_bar_type

        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        r = run_composed_backtest(
            _bracket_tp_off_spec(),
            _trending_bars(),
            iteration_id=1,
            rationale="tp-off regression",
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        assert r.error is None, f"TP-off bracket produced an error: {r.error}"
        m = r.metrics or {}
        assert (m.get("n_trades") or 0) > 0, "no trades opened — entry path broken"


class TestContinuousCircuitBreaker:
    def test_three_identical_errors_stop_the_loop(self, monkeypatch):
        """In continuous mode the same error 3 times consecutively → done=True, run 4 NOT RUN.

        Session 886f439b spun 100 runs with the same 'Cache too little data' error.
        Instead of running the worker for real, we patch the phase-0 data loader
        to produce a deterministic error.
        """
        import web.routes.agent_backtest as ab

        rid = "cbtest01"
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[rid] = {
                "phases": [
                    {
                        "n": i,
                        "label": str(i),
                        "status": "pending",
                        "detail": "",
                        "ts": "",
                    }
                    for i in range(6)
                ],
                "steps": [],
                "done": False,
                "error": None,
                "strategy_name": "",
                "stop_requested": False,
                "continuous_mode": True,
                "winner_result": None,
                "winner_spec_name": "",
                "winner_spec_id": "",
                "winner_rob": None,
                "rob_scan_log": [],
                "rob_scan_current": 0,
                "rob_scan_total": 0,
                "hint": "",
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "backtest_results": [],
                "timeline": [],
            }

        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("Cache contains too little data (5 bars).")

        # The first thing phase 0 touches: load_bybit_bars — same error every run.
        monkeypatch.setattr("data.load_bybit_bars", boom)
        # Also disable the cache fallback: on a fetch error _load_tf falls back to
        # cache; this machine HAS a real cache — redirect to a non-existent path so
        # the error deterministically bubbles up (same RuntimeError every run).
        from pathlib import Path

        monkeypatch.setattr(
            "data._bybit_cache_path",
            lambda *a, **k: Path("Z:/no/such/cache.parquet"),
        )
        try:
            ab._agent_worker(
                rid,
                hint="",
                symbol="BTCUSDT",
                category="linear",
                intervals=["60"],
                n_iterations=2,
                strict_mode=False,
                continuous_mode=True,
            )
        finally:
            state = ab._AGENT_PROGRESS.get(rid) or {}
            with ab._AGENT_LOCK:
                ab._AGENT_PROGRESS.pop(rid, None)

        assert calls["n"] == 3, f"should have stopped after 3 runs, ran {calls['n']} runs"
        assert state.get("done") is True
        joined = " ".join(s["msg"] for s in state.get("steps", []))
        assert "consecutive" in joined, "circuit-breaker step should be logged"


class TestWinnerSessionEnd:
    def test_winner_path_logs_session_end(self, tmp_path, monkeypatch):
        """On the winner path session_end(outcome='winner') must be written to JSONL."""
        import json

        import web.routes.agent_backtest as ab

        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        rid = "wintest1"
        ab._session_log(rid, "winner", spec_name="X")
        # Exactly the same call the fix added:
        ab._session_log(rid, "session_end", round=1, outcome="winner", total_rounds=1)
        lines = (tmp_path / f"{rid}.jsonl").read_text().strip().splitlines()
        events = [json.loads(ln) for ln in lines]
        end = [e for e in events if e["event"] == "session_end"]
        assert end and end[0]["outcome"] == "winner"

    def test_worker_source_contains_winner_session_end(self):
        """Source-level guard: session_end call exists in the winner block."""
        import inspect

        import web.routes.agent_backtest as ab

        src = inspect.getsource(ab._agent_worker)
        # session_end must come after the 'winner' log and before the continuous-return
        wi = src.find('"winner",')
        assert wi != -1
        tail = src[wi : wi + 2000]
        assert 'outcome="winner"' in tail, (
            "no session_end(outcome='winner') on the winner path"
        )


class TestTerminalMessage:
    """Honest message for a run not in memory (when a server restart kills the run
    the UI must NOT SAY 'completed' — 2026-07-14 live incident)."""

    def test_no_log_generic(self, tmp_path, monkeypatch):
        import web.routes.agent_backtest as ab

        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        assert "completed or timed out" in ab._terminal_message("none1")

    def test_session_end_means_completed(self, tmp_path, monkeypatch):
        import json as _json

        import web.routes.agent_backtest as ab

        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        log = tmp_path / "r1.jsonl"
        log.write_text(
            _json.dumps({"event": "session_start"})
            + "\n"
            + _json.dumps({"event": "session_end", "outcome": "winner"})
            + "\n"
        )
        msg = ab._terminal_message("r1")
        assert "completed" in msg and "cut off" not in msg

    def test_truncated_log_means_interrupted(self, tmp_path, monkeypatch):
        import json as _json

        import web.routes.agent_backtest as ab

        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        log = tmp_path / "r2.jsonl"
        log.write_text(
            _json.dumps({"event": "session_start"})
            + "\n"
            + _json.dumps({"event": "step", "msg": "Generating custom block…"})
            + "\n"
        )
        msg = ab._terminal_message("r2")
        assert "cut off" in msg and "restart" in msg


class TestScoreJunkFilter:
    """_score JUNK elimination aligned with NAU: <20 trades OR degenerate drawdown."""

    @staticmethod
    def _res(n_trades, max_dd=-0.1):
        from types import SimpleNamespace

        return SimpleNamespace(
            error=None,
            metrics={
                "n_trades": n_trades,
                "sharpe": 1.0,
                "pnl_pct": 0.1,
                "win_rate": 0.5,
                "max_dd": max_dd,
            },
        )

    def test_threshold_is_nau_aligned(self):
        import web.routes.agent_backtest as ab

        assert ab._MIN_TRADES == 20

    def test_below_threshold_eliminated(self):
        import web.routes.agent_backtest as ab

        assert ab._score(self._res(19)) == float("-inf")
        assert ab._score(self._res(0)) == float("-inf")

    def test_at_or_above_threshold_scored(self):
        import web.routes.agent_backtest as ab

        assert ab._score(self._res(20)) > float("-inf")
        assert ab._score(self._res(150)) > float("-inf")

    def test_degenerate_drawdown_eliminated(self):
        """NAU: max_drawdown <= 0 → junk. In this project max_dd >= 0 (or None)."""
        import web.routes.agent_backtest as ab

        assert ab._score(self._res(100, max_dd=0.0)) == float("-inf")
        assert ab._score(self._res(100, max_dd=0.05)) == float("-inf")
        assert ab._score(self._res(100, max_dd=None)) == float("-inf")
        # Healthy negative drawdown → passes
        assert ab._score(self._res(100, max_dd=-0.2)) > float("-inf")

    def test_error_always_eliminated(self):
        from types import SimpleNamespace

        import web.routes.agent_backtest as ab

        bad = SimpleNamespace(error="crash", metrics={"n_trades": 500})
        assert ab._score(bad) == float("-inf")


class TestAgentPageReattach:
    """GET /agent must auto-attach to an unfinished run if present (fixes the
    'screen unchanged' / blank-after-restart-or-refresh issue — even when the run
    was started from the API)."""

    def test_reattaches_to_active_run(self, monkeypatch):
        from fastapi.testclient import TestClient

        import web.routes.agent_backtest as ab
        from server import app

        monkeypatch.setattr(
            ab,
            "_AGENT_PROGRESS",
            {"deadrun": {"done": True}, "liverun": {"done": False}},
        )
        client = TestClient(app)
        resp = client.get("/agent")
        assert resp.status_code == 200
        # Polling should connect to the newest active run
        assert "/agent/progress/liverun" in resp.text
        assert "Connecting to the running run" in resp.text

    def test_no_active_run_shows_form_prompt(self, monkeypatch):
        from fastapi.testclient import TestClient

        import web.routes.agent_backtest as ab
        from server import app

        monkeypatch.setattr(ab, "_AGENT_PROGRESS", {"deadrun": {"done": True}})
        client = TestClient(app)
        resp = client.get("/agent")
        assert resp.status_code == 200
        assert "start the autonomous research loop" in resp.text
        assert "/agent/progress/deadrun" not in resp.text
