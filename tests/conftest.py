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
    and showing up in the /sessions list. Redirect both module bindings to tmp.
    """
    import web.routes.agent_backtest as ab
    import web.routes.sessions as ss

    log_dir = tmp_path / "agent_sessions"
    log_dir.mkdir()
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", log_dir)
    monkeypatch.setattr(ss, "SESSION_LOG_DIR", log_dir)
    yield
