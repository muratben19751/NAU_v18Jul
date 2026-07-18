import os
import sys

import pytest

# Add project root to sys.path so test files can import project modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _isolate_session_logs(tmp_path, monkeypatch):
    """Testlerin agent session JSONL'lerini GERÇEK cache dizinine yazmasını engelle.

    _session_log / _tl_* helpers modül sabiti SESSION_LOG_DIR'a yazar; testler
    (test_timeline, test_agent_fixes) sahte run_id'lerle bunları çağırınca
    kullanıcının ~/.cache/.../agent_sessions dizinine tltest*.jsonl sızıyordu
    ve /sessions listesinde görünüyordu. Her iki modül bağını da tmp'ye çevir.
    """
    import web.routes.agent_backtest as ab
    import web.routes.sessions as ss

    log_dir = tmp_path / "agent_sessions"
    log_dir.mkdir()
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", log_dir)
    monkeypatch.setattr(ss, "SESSION_LOG_DIR", log_dir)
    yield
