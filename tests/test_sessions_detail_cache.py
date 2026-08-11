"""Oturum detay sayfasının olay önbelleği (DeepR 2026-08-11 [ORTA]).

Bulgu: liste sayfasının özetleri `_SUMMARY_CACHE` ile (mtime_ns, size)
anahtarlı önbellekleniyordu ama DETAY yolunda hiçbir önbellek yoktu — sayfa
her yenilendiğinde tam maliyet yeniden ödeniyor, üstelik `to_thread`
havuzundan bir iş parçacığı o süre boyunca tutuluyordu. Ölçüldü: 267 MB'lık
`6a6ab8e4.jsonl` için 601 ms (sıcak sayfa önbelleğiyle; soğuk diskte kat kat
fazla) → 0,03 ms.

Kapanmış oturum dosyaları hiç değişmez, aktif oturumunki her yazımda değişir;
(mtime_ns, size) anahtarı ikisini de doğru ayırır.

Sınır tarafı: aynı denetimin 4. bulgusu sınırsız bir parse önbelleğiydi, o
yüzden burada kaynak-bayt bütçesi var. Bütçeye sığmayan oturum SESSİZCE
bozulmaz — önbelleğe alınmaz, nedeni loglanır, sayfa doğru çalışmaya devam
eder.

Wiki References
---------------
Bkz: [[nau_performans_denetimi]], [[webapp_module_map]]
"""

from __future__ import annotations

import json
import logging

import pytest

from web.routes import sessions as sm


@pytest.fixture(autouse=True)
def _clean_cache():
    sm._invalidate_detail_cache()
    yield
    sm._invalidate_detail_cache()


def _line(event: str, **fields) -> str:
    return json.dumps({"event": event, **fields})


@pytest.fixture()
def session_log(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "SESSION_LOG_DIR", tmp_path)

    def _write(run_id: str, n_steps: int = 5) -> object:
        p = tmp_path / f"{run_id}.jsonl"
        lines = [_line("session_start", ts="2026-08-01T00:00:00+00:00")]
        lines += [
            _line("step", ts="2026-08-01T00:00:01+00:00", msg=f"s{i}")
            for i in range(n_steps)
        ]
        lines.append(
            _line(
                "backtest_result",
                ts="2026-08-01T00:01:00+00:00",
                round=1,
                spec_name="s1",
                n_trades=3,
                error="",
                score=1.5,
                metrics={"pnl": 1.0, "sharpe": 0.5, "win_rate": 0.5, "max_dd": -0.1},
            )
        )
        lines.append(_line("session_end", ts="2026-08-01T00:02:00+00:00", outcome="ok"))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    return _write


def _count_reads(monkeypatch) -> dict:
    counts = {"full": 0, "headtail": 0}
    real_full, real_ht = sm._read_events, sm._read_events_head_tail

    def _full(run_id, max_lines=None):
        counts["full"] += 1
        return real_full(run_id, max_lines)

    def _ht(run_id, edge_bytes=sm._DETAIL_EDGE_BYTES):
        counts["headtail"] += 1
        return real_ht(run_id, edge_bytes)

    monkeypatch.setattr(sm, "_read_events", _full)
    monkeypatch.setattr(sm, "_read_events_head_tail", _ht)
    return counts


class TestHitAndMiss:
    def test_hit_an_unchanged_session_is_read_once(self, session_log, monkeypatch):
        session_log("aaaa0001")
        counts = _count_reads(monkeypatch)

        first = sm._detail_events("aaaa0001", False)
        second = sm._detail_events("aaaa0001", False)

        assert counts["full"] == 1, "değişmemiş oturum dosyası yeniden okundu"
        assert first[0] is second[0]
        assert len(first[0]) == 8

    def test_miss_a_still_running_session_is_re_read_on_every_request(
        self, session_log, monkeypatch
    ):
        """AKTİF oturum her yazımda (mtime_ns, size) değiştirir → asla bayat."""
        p = session_log("aaaa0002")
        counts = _count_reads(monkeypatch)

        before = sm._detail_events("aaaa0002", False)[0]
        with open(p, "a", encoding="utf-8") as f:
            f.write(
                _line("winner", ts="2026-08-01T00:03:00+00:00", spec_name="w") + "\n"
            )
        after = sm._detail_events("aaaa0002", False)[0]

        assert counts["full"] == 2
        assert len(after) == len(before) + 1
        assert any(e.get("event") == "winner" for e in after)

    def test_a_missing_session_file_is_not_cached(self, session_log, monkeypatch):
        counts = _count_reads(monkeypatch)

        events, heavy, truncated = sm._detail_events("deadbeef", False)

        assert events == [] and heavy == 0 and truncated is False
        assert "deadbeef" not in sm._DETAIL_CACHE
        assert counts["full"] == 1

    def test_the_head_tail_path_is_cached_too(self, session_log, monkeypatch):
        session_log("aaaa0003", n_steps=200)
        counts = _count_reads(monkeypatch)

        sm._detail_events("aaaa0003", True)
        sm._detail_events("aaaa0003", True)

        assert counts["headtail"] == 1
        assert counts["full"] == 0


class TestMemoryBudget:
    def test_an_over_budget_session_is_not_cached_and_says_why(
        self, session_log, monkeypatch, caplog
    ):
        """SINIR: sessizce bozma yok — veri tam döner, neden loglanır."""
        session_log("aaaa0004")
        monkeypatch.setattr(sm, "_DETAIL_CACHE_MAX_SOURCE_BYTES", 10)  # 10 bayt

        with caplog.at_level(logging.WARNING):
            events, _heavy, _trunc = sm._detail_events("aaaa0004", False)

        assert len(events) == 8, "sınır olayları kırpmış"
        assert "aaaa0004" not in sm._DETAIL_CACHE
        assert any("cache SKIPPED" in r.message for r in caplog.records)

    def test_the_oldest_entry_is_evicted_when_the_entry_cap_is_hit(
        self, session_log, monkeypatch
    ):
        monkeypatch.setattr(sm, "_DETAIL_CACHE_MAX_ENTRIES", 2)
        for i in range(3):
            session_log(f"bbbb000{i}")
            sm._detail_events(f"bbbb000{i}", False)

        assert list(sm._DETAIL_CACHE) == ["bbbb0001", "bbbb0002"]

    def test_the_byte_budget_bounds_the_total(self, session_log, monkeypatch):
        p = session_log("cccc0001")
        one = p.stat().st_size
        monkeypatch.setattr(sm, "_DETAIL_CACHE_MAX_SOURCE_BYTES", one * 2)
        for i in range(4):
            session_log(f"cccc000{i}")
            sm._detail_events(f"cccc000{i}", False)

        total = sum(e[2] for e in sm._DETAIL_CACHE.values())
        assert total <= one * 2
        assert len(sm._DETAIL_CACHE) <= 2

    def test_a_zero_entry_cap_disables_caching_entirely(self, session_log, monkeypatch):
        session_log("dddd0001")
        monkeypatch.setattr(sm, "_DETAIL_CACHE_MAX_ENTRIES", 0)

        sm._detail_events("dddd0001", False)

        assert sm._DETAIL_CACHE == {}


class TestSharedEventsStaySafeToRender:
    def test_two_renders_of_a_cached_session_produce_the_same_page(
        self, session_log, monkeypatch
    ):
        """Rota önbellekli olay sözlüklerini `ev_i`/`score` ile damgalıyor;
        damga idempotent olmasaydı ikinci yenileme farklı görünürdü."""
        from fastapi.testclient import TestClient

        from server import app

        session_log("eeee0001")
        counts = _count_reads(monkeypatch)
        client = TestClient(app)

        first = client.get("/sessions/eeee0001")
        second = client.get("/sessions/eeee0001")

        assert first.status_code == second.status_code == 200
        assert counts["full"] == 1, "detay rotası önbelleği kullanmıyor"
        assert first.text == second.text
