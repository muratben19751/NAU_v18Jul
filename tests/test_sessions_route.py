"""Session log list/detail bounded-read paths (web/routes/sessions.py).

Regression coverage for two silent-data-loss bugs found in review:
- a completed session's `session_end` pushed past the summary's fixed 128KB
  tail window used to misreport the session as still "running";
- a mid-file structural event (e.g. `winner`) pushed outside the detail
  route's 2MB head/tail windows used to vanish from the replay entirely.
Both are fixed by scanning further (bounded) instead of trusting a fixed
byte window to always contain the events that matter.
"""

from __future__ import annotations

import json

from web.routes import sessions as sessions_mod


def _line(event: str, **fields) -> str:
    return json.dumps({"event": event, **fields})


def test_large_session_summary_finds_session_end_past_fixed_tail_window(tmp_path):
    # A huge backtest_result line sits between the last small structural event
    # and session_end, big enough alone to blow past the old fixed 128KB tail.
    path = tmp_path / "abc.jsonl"
    huge_payload = "x" * (200 * 1024)
    lines = [
        _line("session_start", ts="2026-08-01T00:00:00+00:00", symbol="BTCUSDT"),
        _line("backtest_result", round=1, blob=huge_payload),
        _line(
            "session_end",
            ts="2026-08-01T01:00:00+00:00",
            outcome="stopped",
            completed_rounds=3,
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = sessions_mod._large_session_summary("abc", path, size_mb=1.0)

    assert summary["outcome"] == "stopped"
    assert summary["total_rounds"] == 3


def test_read_events_head_tail_captures_mid_file_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_mod, "SESSION_LOG_DIR", tmp_path)
    path = tmp_path / "def01234.jsonl"
    # Head padding, then a lightweight winner event buried in the middle,
    # then enough padding that head/tail windows (2MB default) can't reach it.
    pad_line = _line("backtest_result", round=1, blob="y" * 100)
    lines = [_line("session_start", ts="t0")]
    lines += [pad_line] * 50
    lines.append(_line("winner", ts="mid", spec_name="my_winner_spec", score=1.23))
    lines += [pad_line] * 50
    lines.append(_line("session_end", ts="t1", outcome="stopped"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events, n_heavy_skipped = sessions_mod._read_events_head_tail(
        "def01234", edge_bytes=64
    )

    winners = [e for e in events if e.get("event") == "winner"]
    assert winners and winners[0]["spec_name"] == "my_winner_spec"
    assert (
        n_heavy_skipped > 0
    )  # the padding backtest_result lines were counted, not parsed
