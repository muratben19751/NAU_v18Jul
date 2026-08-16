"""Koşu susarsa NEREDE takıldığını gösteren thread dökümü alınsın.

2026-08-15/16'da üç AUTO koşusu sonuçsuz durdu: süreç ayakta kaldı (pm2 restart
sayacı değişmedi), `session_end` hiç yazılmadı, nabız kesildi. Nerede takıldığını
gösteren tek bir kayıt yoktu — teşhis üç ayrı hipoteze kalıyordu
(`_run_openrouter_killable`'ın `proc.start()`'ı / nabız thread'inin sessiz ölümü /
başka bir şey) ve hiçbirini ayırt edecek kanıt üretilemiyordu.

Bu watchdog tahmini kanıtla değiştirir: sessizlik eşiği aşılınca TÜM thread'lerin
yığın izi dosyaya yazılır. Koşuyu etkilemez — yalnız okur, hiçbir şeyi öldürmez.

Wiki References
---------------
See: [[auto_mission_control]], [[auto_360_canli_review_iyilestirmeleri]].
"""

from __future__ import annotations

import time

import pytest

import web.routes.agent_backtest as ab


@pytest.fixture
def run_state(tmp_path, monkeypatch):
    """Canlı görünen bir koşu + izole oturum dizini."""
    monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
    monkeypatch.setattr(ab, "_STALL_DUMP_SEC", 0.05)
    run_id = "stall-test"
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS[run_id] = {"done": False}
    ab._LAST_LOG_AT.pop(run_id, None)
    yield run_id, tmp_path
    with ab._AGENT_LOCK:
        ab._AGENT_PROGRESS.pop(run_id, None)
    ab._LAST_LOG_AT.pop(run_id, None)


def _wait_for(path, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


class TestDumpsOnSilence:
    def test_writes_all_thread_stacks_when_the_run_goes_quiet(self, run_state):
        run_id, tmp = run_state
        # Damgayı geçmişe koy: koşu "sessiz" sayılsın.
        ab._LAST_LOG_AT[run_id] = time.monotonic() - 60

        ab._start_stall_watchdog(run_id)
        dump = tmp / f"{run_id}.stall.txt"

        assert _wait_for(dump), "sessizlikte döküm alınmadı"
        text = dump.read_text(encoding="utf-8", errors="replace")
        assert "STALL DUMP #1" in text
        assert run_id in text
        # faulthandler TÜM thread'leri döker — takılan thread'i bulmanın tek yolu.
        assert "Thread" in text or "Current thread" in text

    def test_a_finished_run_is_not_dumped(self, run_state):
        run_id, tmp = run_state
        ab._LAST_LOG_AT[run_id] = time.monotonic() - 60
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS[run_id]["done"] = True

        ab._start_stall_watchdog(run_id)
        dump = tmp / f"{run_id}.stall.txt"

        assert not _wait_for(dump, timeout=1.0), "biten koşu için döküm alınmamalı"

    def test_an_active_run_is_not_dumped(self, run_state):
        """Yazan bir koşu sessiz değildir — gürültü üretmemeli."""
        run_id, tmp = run_state
        ab._LAST_LOG_AT[run_id] = time.monotonic()  # az önce yazdı

        ab._start_stall_watchdog(run_id)
        dump = tmp / f"{run_id}.stall.txt"

        assert not _wait_for(dump, timeout=1.0)


class TestSessionLogStampsActivity:
    def test_a_write_refreshes_the_idle_clock(self, tmp_path, monkeypatch):
        """Damga `_session_log`'dan gelmeli, yoksa watchdog kör kalır."""
        monkeypatch.setattr(ab, "SESSION_LOG_DIR", tmp_path)
        run_id = "stamp-test"
        ab._LAST_LOG_AT.pop(run_id, None)

        ab._session_log(run_id, "step", msg="merhaba")

        assert run_id in ab._LAST_LOG_AT
        ab._LAST_LOG_AT.pop(run_id, None)
