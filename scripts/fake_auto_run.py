"""Dev helper: inject a synthetic AUTO run into the live server's state.

Lets the cockpit be reviewed in its RUNNING state without burning LLM tokens.
Run against a server started in-process (this script imports the same modules,
so it only works when pointed at the same interpreter — use it via
scripts/check_auto_cockpit.py --fake).
"""

from __future__ import annotations

import time


def inject(run_id: str = "faker001") -> str:
    from web.routes.agent_backtest import _AGENT_LOCK, _AGENT_PROGRESS

    labels = [
        "Loading data",
        "Generating strategy",
        "Backtest loop",
        "Ranking",
        "Robustness scan",
        "Completed",
    ]
    statuses = ["done", "done", "running", "pending", "pending", "pending"]
    details = ["5 set", "ema_cross + rsi", "walk-forward 4 kat", "", "", ""]
    state = {
        "phases": [
            {
                "n": i,
                "label": lbl,
                "status": statuses[i],
                "detail": details[i],
                "ts": "",
            }
            for i, lbl in enumerate(labels)
        ],
        "steps": [
            {"ts": "13:29:36", "msg": "Loading catalog data (QQQ.NASDAQ, 15-MINUTE)…"},
            {"ts": "13:29:41", "msg": "1m cropped to the last 2 years (28,681 bars)"},
            {"ts": "13:29:44", "msg": "Date window 2023-01-01 → 2025-01-01"},
            {
                "ts": "13:29:52",
                "msg": "  ⚙ Generating custom entry block: vol_breakout…",
            },
            {"ts": "13:29:58", "msg": "  ✓ Entry block saved: agent_entry_1"},
            {"ts": "13:30:02", "msg": "[3/5] Backtest [1H]: Vol-breakout + ATR…"},
            {"ts": "13:30:14", "msg": "  ✓ PnL=+412.30 · 318 trades"},
        ],
        "done": False,
        "error": None,
        "strategy_name": "",
        "stop_requested": False,
        "continuous_mode": True,
        "winner_result": None,
        "winner_spec_name": "",
        "winner_spec_id": "",
        "winner_rob": None,
        "winner_holdout": None,
        "rob_scan_log": [],
        "rob_scan_current": 0,
        "rob_scan_total": 0,
        "hint": "",
        "tokens_in": 9000,
        "tokens_out": 2500,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "timeline": [],
        "backtest_results": [
            {
                "iter": 1,
                "round": 1,
                "interval": "60",
                "spec_name": "Donchian breakout",
                "score": 1.12,
                "pnl": 210.0,
                "pnl_pct": 0.021,
                "sharpe": 0.91,
                "profit_factor": 1.24,
                "max_dd": -0.182,
                "win_rate": 0.48,
                "n_trades": 412,
                "error": None,
                "equity": [100, 104, 101, 108, 106, 113, 110, 118, 116, 124],
            },
            {
                "iter": 2,
                "round": 1,
                "interval": "60",
                "spec_name": "EMA-cross + RSI",
                "score": 1.84,
                "pnl": 512.0,
                "pnl_pct": 0.051,
                "sharpe": 1.42,
                "profit_factor": 1.62,
                "max_dd": -0.114,
                "win_rate": 0.55,
                "n_trades": 318,
                "error": None,
                "equity": [100, 106, 104, 115, 112, 126, 123, 138, 135, 152],
            },
        ],
        "brief": {
            "symbol": "BTCUSDT",
            "category": "linear",
            "model": "Fable 5",
            "robustness": "Strict",
            "range": "max",
            "range_start": "",
            "range_end": "",
            "iterations": 5,
            "guidance": "",
            "tfs": ["60"],
            "intervals": ["60"],
            "continuous": True,
        },
        "started_at": time.time() - 129,
        "max_hours": 0.0,
        "max_total_tokens": 0,
    }
    with _AGENT_LOCK:
        _AGENT_PROGRESS[run_id] = state
    return run_id
