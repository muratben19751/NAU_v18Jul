"""Characterization tests for GET /studio (web/routes/studio.py::page).

DeepR 2026-08-09 [YÜKSEK]: studio.py reaches directly into 4 other route
modules' private (`_`-prefixed) state (backtest.py, strategy.py,
agent_backtest.py's raw `_AGENT_LOCK`/`_AGENT_PROGRESS`) with zero coverage
of its own — `tests/test_studio_ui_fixes.py` drives `/backtest/*`, and
`tests/studio/*.py` drive the *different* `/studio/{id}` canvas router
(web/routes/strategy_studio.py), never bare `GET /studio`. Before touching
any of the reached-into names, this file locks in current behavior —
especially the active-run-id scan, the one piece involving `_AGENT_LOCK`
(a lock with documented deadlock history, see tests/test_lock_nesting.py).
"""

from __future__ import annotations

import pytest


def _client():
    from fastapi.testclient import TestClient

    from server import app

    return TestClient(app)


@pytest.fixture
def stub_catalog(monkeypatch):
    import web.routes.studio as studio

    monkeypatch.setattr(studio, "load_catalog", lambda: [])
    return studio


@pytest.fixture
def clean_agent_progress():
    """_AGENT_PROGRESS is process-global, shared with every other test in the
    suite. A test asserting "no active run" must not assume it starts empty —
    another test can legitimately leave a not-yet-cleaned-up entry behind
    (observed: this file's own no-active-run tests passed in isolation but
    failed inside the full suite). Snapshot, clear, restore — never mutate
    other tests' state permanently."""
    import web.routes.agent_backtest as ab

    with ab._AGENT_LOCK:
        saved = dict(ab._AGENT_PROGRESS)
        ab._AGENT_PROGRESS.clear()
    try:
        yield
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.clear()
            ab._AGENT_PROGRESS.update(saved)


def _progress_entry(*, done: bool) -> dict:
    return {
        "phases": [],
        "steps": [],
        "done": done,
        "error": None,
        "strategy_name": "",
        "stop_requested": False,
        "continuous_mode": False,
    }


def test_studio_page_renders_200_with_no_active_run(stub_catalog, clean_agent_progress):
    resp = _client().get("/studio")

    assert resp.status_code == 200
    assert "running = false" in resp.text


def test_studio_page_sets_the_draft_session_cookie_when_missing(stub_catalog):
    resp = _client().get("/studio")

    assert "nautlab_sid" in resp.cookies


def test_studio_page_reflects_the_newest_active_run(stub_catalog, monkeypatch):
    import web.routes.agent_backtest as ab

    older_done = "aaaaaaaa"
    newer_active = "bbbbbbbb"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[older_done] = _progress_entry(done=True)
        ab._AGENT_PROGRESS[newer_active] = _progress_entry(done=False)
    try:
        resp = _client().get("/studio")
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(older_done, None)
            ab._AGENT_PROGRESS.pop(newer_active, None)

    assert resp.status_code == 200
    assert "running = true" in resp.text
    assert f"/agent/progress/{newer_active}?view=mission" in resp.text
    assert f"/agent/stop/{newer_active}" in resp.text
    assert older_done not in resp.text


def test_studio_page_ignores_a_run_already_marked_done(
    stub_catalog, clean_agent_progress
):
    import web.routes.agent_backtest as ab

    finished = "cccccccc"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[finished] = _progress_entry(done=True)
    try:
        resp = _client().get("/studio")
    finally:
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop(finished, None)

    assert resp.status_code == 200
    assert "running = false" in resp.text
    assert finished not in resp.text
