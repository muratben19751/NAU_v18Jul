"""Strategy Studio fixes — duplicate-submit guard, date validation, result JS.

Covers the server side of the 2026-07 Studio bug batch:

- Bug 1 (duplicate submission): one live backtest-family job per session —
  /backtest/run, /backtest/describe and /backtest/sweep return 409 while the
  session already has an unfinished run/gen/sweep, and the registry self-clears
  once the store record is done (or evicted by a restart).
- Bug 2 (result-panel JS): the swapped fragment must not declare top-level
  lexical bindings — a re-swapped `const` throws "Identifier ... has already
  been declared" and kills the whole chart script.
- Bug 4 (date validation): an inverted start/end range is rejected with 400
  before any worker thread / LLM call starts.

No LLM, no disk catalog: ``load_catalog`` is monkeypatched with a stub spec.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.routes.backtest as bt

FRAGMENTS = Path(__file__).resolve().parents[1] / "web" / "templates" / "fragments"


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


@pytest.fixture
def stub_catalog(monkeypatch):
    spec = SimpleNamespace(id="spec-1", name="Stub Strategy", trade_size=0.01)
    monkeypatch.setattr(bt, "load_catalog", lambda: [spec])
    return spec


# ── Bug 4 · date-range validation ────────────────────────────────────────────


def test_invalid_date_range_helper():
    assert bt._invalid_date_range("", "") is None
    assert bt._invalid_date_range("2026-07-01", "") is None
    assert bt._invalid_date_range("", "2026-07-03") is None
    assert bt._invalid_date_range("2026-07-01", "2026-07-03") is None
    assert bt._invalid_date_range("2026-07-01", "2026-07-01") is None
    assert "before" in bt._invalid_date_range("2026-07-03", "2026-07-01")
    assert "format" in bt._invalid_date_range("not-a-date", "2026-07-01")


def test_run_rejects_inverted_dates(stub_catalog):
    c = _client()
    r = c.post(
        "/backtest/run",
        data={
            "spec_id": "spec-1",
            "bybit_start": "2026-07-03",
            "bybit_end": "2026-07-01",
        },
    )
    assert r.status_code == 400
    assert "before the start date" in r.text
    assert r.headers.get("HX-Toast", "").startswith("err|")


def test_describe_rejects_inverted_dates():
    c = _client()
    r = c.post(
        "/backtest/describe",
        data={
            "description": "BUY when RSI(14) drops below 30 and crosses back up",
            "bybit_start": "2026-07-03",
            "bybit_end": "2026-07-01",
        },
    )
    assert r.status_code == 400
    assert "before the start date" in r.text


def test_sweep_rejects_inverted_dates(stub_catalog):
    c = _client()
    r = c.post(
        "/backtest/sweep",
        data={
            "spec_id": "spec-1",
            "intervals": ["15", "60"],
            "bybit_start": "2026-07-03",
            "bybit_end": "2026-07-01",
        },
    )
    assert r.status_code == 400
    assert "before the start date" in r.text


# ── Bug 1 · one live run per session ─────────────────────────────────────────


@pytest.fixture
def busy_session():
    """Register a fake unfinished run for a fixed session id; clean up after."""
    sid = "test-busy-session"
    run_id = "fakebusyrun"
    bt._RUN_STORE.create_evicting(
        run_id,
        {
            "steps": [],
            "done": False,
            "result": None,
            "error": None,
            "spec_name": "Stub",
            "bars_info": {},
            "narrative": "",
        },
    )
    bt._session_set_active(sid, "run", run_id)
    yield sid, run_id
    with bt._RUN_STORE.lock:
        bt._RUN_STORE.raw().pop(run_id, None)
    with bt._ACTIVE_RUNS_LOCK:
        bt._ACTIVE_RUNS.pop(sid, None)


def test_run_conflict_while_session_busy(stub_catalog, busy_session):
    sid, _run_id = busy_session
    c = _client()
    c.cookies.set("nautlab_sid", sid)
    r = c.post("/backtest/run", data={"spec_id": "spec-1"})
    assert r.status_code == 409
    assert "already in progress" in r.text
    assert r.headers.get("HX-Toast", "").startswith("err|")


def test_describe_conflict_while_session_busy(busy_session):
    sid, _run_id = busy_session
    c = _client()
    c.cookies.set("nautlab_sid", sid)
    r = c.post(
        "/backtest/describe",
        data={"description": "BUY when RSI(14) drops below 30 and crosses back up"},
    )
    assert r.status_code == 409


def test_busy_registry_self_clears_when_done(busy_session):
    sid, run_id = busy_session
    assert bt._session_active_kind(sid) == "run"
    with bt._RUN_STORE.lock:
        bt._RUN_STORE.raw()[run_id]["done"] = True
    # A done record no longer counts as active, and the entry is dropped.
    assert bt._session_active_kind(sid) is None
    with bt._ACTIVE_RUNS_LOCK:
        assert sid not in bt._ACTIVE_RUNS


def test_busy_registry_prunes_abandoned_sessions():
    """Past the cap, stale entries (evicted/done store records) are dropped on
    the next insert — the registry cannot grow without bound."""
    stale_sids = [f"stale-{i}" for i in range(bt._ACTIVE_RUNS_MAX)]
    try:
        with bt._ACTIVE_RUNS_LOCK:
            for s in stale_sids:
                # Points at a run id that no store holds → guaranteed stale.
                bt._ACTIVE_RUNS[s] = ("run", "gone-" + s)
        bt._session_set_active("fresh-session", "run", "fresh-run")
        with bt._ACTIVE_RUNS_LOCK:
            assert "fresh-session" in bt._ACTIVE_RUNS
            assert not any(s in bt._ACTIVE_RUNS for s in stale_sids)
    finally:
        with bt._ACTIVE_RUNS_LOCK:
            bt._ACTIVE_RUNS.pop("fresh-session", None)
            for s in stale_sids:
                bt._ACTIVE_RUNS.pop(s, None)


def test_busy_registry_ignores_evicted_record():
    sid = "test-evicted-session"
    bt._session_set_active(sid, "run", "no-such-run")
    try:
        assert bt._session_active_kind(sid) is None
    finally:
        with bt._ACTIVE_RUNS_LOCK:
            bt._ACTIVE_RUNS.pop(sid, None)


def test_other_sessions_are_not_blocked(stub_catalog, busy_session, monkeypatch):
    """The guard is per-session — another sid gets a normal progress panel."""
    import time

    import pandas as pd

    import data

    # Keep the worker cheap and offline: no bars → quick error path.
    monkeypatch.setattr(data, "load_bybit_bars", lambda **k: pd.DataFrame())

    c = _client()
    c.cookies.set("nautlab_sid", "a-different-session")
    r = c.post(
        "/backtest/run",
        data={"spec_id": "spec-1", "symbol": "NOCACHE", "interval": "1"},
    )
    assert r.status_code == 200
    assert "progress-panel" in r.text

    # Wait for the spawned worker to finish (it must not outlive the
    # monkeypatched load_bybit_bars).
    m = re.search(r"/backtest/progress/([0-9a-f]+)", r.text)
    assert m, "progress fragment should reference the run id"
    run_id = m.group(1)
    for _ in range(100):
        raw = bt._RUN_STORE.get(run_id)
        if raw is None or raw.get("done"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("run worker did not finish")


# ── Bug 2 · swapped fragment must not declare top-level const/let ────────────


def test_result_fragment_has_no_toplevel_lexical_declarations():
    """`const`/`let` at script top level breaks on the SECOND #result swap
    (SyntaxError: Identifier already declared) — everything must live inside a
    function or IIFE. Heuristic: a fragment script line declaring const/let at
    indentation ≤ 6 spaces inside <script> is treated as top-level."""
    text = (FRAGMENTS / "backtest_result.html").read_text(encoding="utf-8")
    assert "const _resultEl" not in text
    in_script = False
    depth = 0
    offenders: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("<script"):
            in_script = True
            depth = 0
            continue
        if s.startswith("</script"):
            in_script = False
            continue
        if not in_script:
            continue
        # Brace-depth heuristic: depth 0 = script top level. Braces inside
        # strings/template literals are balanced in this file, so net counting
        # per line is adequate for a regression guard.
        if depth == 0 and re.match(r"^(const|let)\s", s):
            offenders.append(line)
        depth += line.count("{") - line.count("}")
    assert not offenders, f"top-level lexical declarations: {offenders}"
