"""Persistent per-model token ledger — record/aggregate/report.

Verifies: the four usage fields are extracted from both an Anthropic-style
usage object and a plain dict; per-model totals aggregate correctly (fallback
attributed to the model the API actually answered with); torn/blank JSONL lines
are skipped; and record() never raises into the caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import token_ledger


def _usage_obj(i=0, o=0, cr=0, cw=0):
    return SimpleNamespace(
        input_tokens=i,
        output_tokens=o,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cw,
    )


def test_extract_from_object_and_dict():
    a = token_ledger._extract_usage(_usage_obj(10, 20, 5, 3))
    assert a == {"input": 10, "output": 20, "cache_read": 5, "cache_write": 3}
    b = token_ledger._extract_usage(
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 4,
        }
    )
    assert b == {"input": 1, "output": 2, "cache_read": 0, "cache_write": 4}
    assert token_ledger._extract_usage(None) == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_record_and_summary_per_model(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)

    token_ledger.record("claude-fable-5", _usage_obj(100, 40, 10, 5), "strategy")
    token_ledger.record("claude-fable-5", _usage_obj(50, 20, 0, 0), "narrative")
    # Fallback call attributed to Opus (the model that actually answered).
    token_ledger.record("claude-opus-4-8", _usage_obj(200, 80, 30, 0), "custom_block")

    s = token_ledger.summary(ledger)
    fable = s["models"]["claude-fable-5"]
    opus = s["models"]["claude-opus-4-8"]
    assert fable["calls"] == 2
    assert fable["input"] == 150
    assert fable["output"] == 60
    assert fable["total"] == 150 + 60 + 10 + 5
    assert opus["calls"] == 1
    assert opus["total"] == 200 + 80 + 30
    assert s["total"]["calls"] == 3
    assert s["total"]["total"] == fable["total"] + opus["total"]


def test_zero_usage_not_recorded(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record("claude-fable-5", _usage_obj(0, 0, 0, 0), "empty")
    # Nothing written → summary is empty.
    assert token_ledger.summary(ledger)["models"] == {}


def test_torn_lines_skipped(tmp_path):
    ledger = tmp_path / "token_usage.jsonl"
    good = json.dumps(
        {
            "ts": "2026-07-24T00:00:00+00:00",
            "model": "claude-fable-5",
            "input": 10,
            "output": 5,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    ledger.write_text(good + "\n\n{not valid json\n" + good + "\n", encoding="utf-8")
    s = token_ledger.summary(ledger)
    assert s["models"]["claude-fable-5"]["calls"] == 2  # blank + torn skipped


def test_record_never_raises(monkeypatch):
    # Point the ledger at an un-writable path shape; record must swallow the error.
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", Path("\0illegal") / "x.jsonl")
    token_ledger.record("claude-fable-5", _usage_obj(1, 1, 0, 0), "boom")  # no raise


def test_summary_absent_ledger(tmp_path):
    s = token_ledger.summary(tmp_path / "does_not_exist.jsonl")
    assert s["models"] == {}
    assert s["total"]["calls"] == 0


def test_format_table_smoke(tmp_path, monkeypatch):
    ledger = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(token_ledger, "LEDGER_PATH", ledger)
    token_ledger.record("claude-fable-5", _usage_obj(1000, 200, 0, 0), "strategy")
    table = token_ledger.format_table(ledger)
    assert "claude-fable-5" in table
    assert "TOTAL" in table
    # Empty ledger → friendly message, not a crash.
    assert "No token usage" in token_ledger.format_table(tmp_path / "nope.jsonl")
