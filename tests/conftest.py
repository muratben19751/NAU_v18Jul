import os
import sys

import pytest

# Add project root to sys.path so test files can import project modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _isolate_session_logs(tmp_path, monkeypatch):
    """Prevent tests from writing agent session JSONLs to the ACTUAL cache directory.

    _session_log / _tl_* helpers write to the module constant SESSION_LOG_DIR; when tests
    (test_timeline, test_agent_fixes) call these with fake run_ids, tltest*.jsonl was
    leaking into the user's ~/.cache/.../agent_sessions directory
    and showing up in the /sessions list. Redirect every module binding to tmp.

    SESSION_LOG_DIR is defined once in web.shared (DeepR 2026-08-09 [YÜKSEK])
    and re-bound by name in agent_backtest.py/sessions.py (module-level
    imports — each gets its own patchable attribute) and read directly off
    web.shared by tearsheet.py (a local, function-scoped import) — all three
    bindings need patching, or tearsheet.py alone would keep reading the
    real ~/.cache path in tests.
    """
    import web.routes.agent_backtest as ab
    import web.routes.sessions as ss
    import web.shared as shared

    log_dir = tmp_path / "agent_sessions"
    log_dir.mkdir()
    monkeypatch.setattr(shared, "SESSION_LOG_DIR", log_dir)
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", log_dir)
    monkeypatch.setattr(ss, "SESSION_LOG_DIR", log_dir)
    yield
