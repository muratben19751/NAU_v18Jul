"""An AUTO run must be reviewable after the fact — from its log alone.

The session log recorded twenty event types but still could not answer the two
questions a review opens with: *which code produced this*, and *what did the
model actually see*. These tests pin the three pieces that close that gap and,
just as importantly, pin the honesty rules: absence is stated rather than
rendered as a zero, and a credential is never written to an audit file.
"""

from __future__ import annotations

import json
import time

import pytest

import auto_review
import llm_dispatch
import nau_provenance


class TestProvenance:
    def test_credentials_are_recorded_as_set_never_as_value(self):
        env = {
            "NAU_ANTHROPIC_KEY": "sk-secret-value",
            "NAUTILUS_OPENROUTER_TOKEN": "or-secret",
            "NAU_DB_PASSWORD": "hunter2",
            "NAU_DATA_DIR": "/tmp/data",
        }
        out = nau_provenance.env_overrides(env)
        assert out["NAU_ANTHROPIC_KEY"] == "<set>"
        assert out["NAUTILUS_OPENROUTER_TOKEN"] == "<set>"
        assert out["NAU_DB_PASSWORD"] == "<set>"
        # An audit log is read by more people than a shell is; the *fact* of an
        # override is behaviour, the value is a credential.
        assert "sk-secret-value" not in json.dumps(out)
        assert out["NAU_DATA_DIR"] == "/tmp/data"

    def test_only_this_app_s_env_is_recorded(self):
        out = nau_provenance.env_overrides(
            {"PATH": "/usr/bin", "HOME": "/home/x", "NAU_STRICT": "1"}
        )
        assert out == {"NAU_STRICT": "1"}

    def test_unknown_cleanliness_is_not_reported_as_clean(self, monkeypatch):
        """`git status` failing means *unknown*, and unknown ≠ committed."""

        def _fake(*args, cwd):
            return "abc123def456" if args[0] == "rev-parse" else None

        monkeypatch.setattr(nau_provenance, "_git", _fake)
        assert nau_provenance.git_revision()["dirty"] is None

    def test_a_missing_git_never_breaks_the_run(self, monkeypatch):
        monkeypatch.setattr(nau_provenance, "_git", lambda *a, **k: None)
        rev = nau_provenance.git_revision()
        assert rev == {"available": False}
        # The whole record must still be produced — provenance is evidence, not
        # a precondition for starting research.
        assert nau_provenance.run_provenance()["python"]


class _Resp:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]


class TestTranscriptCapture:
    @pytest.fixture
    def kwargs(self):
        return {
            "system": "sen bir strateji üreticisisin",
            "messages": [
                {"role": "user", "content": "bir fikir ver"},
                {"role": "assistant", "content": [{"type": "text", "text": "tamam"}]},
            ],
        }

    def test_no_observer_means_no_work(self, kwargs, monkeypatch):
        """Outside an AUTO run there is nobody to hand a transcript to."""
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: False)
        assert llm_dispatch._transcript_fields(kwargs, _Resp("x")) == {}

    def test_prompt_and_completion_are_captured_with_roles(self, kwargs, monkeypatch):
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: True)
        out = llm_dispatch._transcript_fields(kwargs, _Resp("cevap metni"))
        assert "### system" in out["prompt"] and "### user" in out["prompt"]
        assert "bir fikir ver" in out["prompt"]
        assert out["completion"] == "cevap metni"
        assert out["prompt_chars"] == len(out["prompt"])

    def test_a_failed_call_still_records_what_was_asked(self, kwargs, monkeypatch):
        """No response, but the question is the only record of why it failed."""
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: True)
        out = llm_dispatch._transcript_fields(kwargs)
        assert out["prompt"] and "completion" not in out

    def test_clipping_keeps_both_ends_and_states_the_true_length(self, monkeypatch):
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: True)
        monkeypatch.setattr(llm_dispatch, "_TRANSCRIPT_MAX_CHARS", 100)
        kwargs = {"messages": [{"role": "user", "content": "A" * 400 + "Z" * 400}]}
        out = llm_dispatch._transcript_fields(kwargs)
        # The head is the system instruction, the tail is the actual request —
        # cutting from one end throws away half the evidence.
        assert out["prompt"].startswith("### user\nA")
        assert out["prompt"].endswith("Z" * 50)
        assert "atlandı" in out["prompt"]
        assert out["prompt_chars"] > len(out["prompt"])

    def test_the_kill_switch_is_honoured(self, kwargs, monkeypatch):
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: True)
        monkeypatch.setattr(llm_dispatch, "_TRANSCRIPT_ENABLED", False)
        assert llm_dispatch._transcript_fields(kwargs, _Resp("x")) == {}

    def test_an_unreadable_prompt_never_breaks_the_llm_call(self, monkeypatch):
        monkeypatch.setattr(llm_dispatch, "llm_observer_installed", lambda: True)

        class Exploding:
            def __iter__(self):
                raise RuntimeError("boom")

        assert llm_dispatch._transcript_fields({"messages": Exploding()}) == {}


class TestTranscriptOffload:
    @pytest.fixture
    def state(self):
        from web.routes.agent_backtest import _WorkerState

        return _WorkerState()

    def test_text_leaves_the_jsonl_line_and_identity_stays(self, state):
        from web.routes.agent_backtest import _offload_transcript

        event = {
            "purpose": "idea",
            "prompt": "P" * 50,
            "completion": "C" * 20,
            "prompt_chars": 50,
            "completion_chars": 20,
        }
        out = _offload_transcript("aaaa1111", state, event)
        # Inline, these strings would be re-parsed by every reader of the log.
        assert "prompt" not in out and "completion" not in out
        assert out["transcript"]["sha256"] and out["transcript"]["bytes"] > 0
        assert out["transcript"]["clipped"] is False

    def test_a_clipped_transcript_says_so(self, state):
        from web.routes.agent_backtest import _offload_transcript

        out = _offload_transcript(
            "aaaa1111",
            state,
            {"purpose": "idea", "prompt": "P" * 10, "prompt_chars": 9000},
        )
        # Mistaking a clipped prompt for the whole one means reviewing an
        # instruction the model never received.
        assert out["transcript"]["clipped"] is True

    def test_an_event_without_a_transcript_is_untouched(self, state):
        from web.routes.agent_backtest import _offload_transcript

        event = {"purpose": "idea", "usage": {"input_tokens": 5}}
        assert _offload_transcript("aaaa1111", state, event) is event

    def test_the_observer_installed_on_the_run_is_the_one_that_offloads(self):
        """The seam: telemetry from llm_dispatch → artifact → one JSONL line."""
        import web.routes.agent_backtest as ab

        _, observer = ab._make_llm_control("dddd4444", 0.0, ab._WorkerState())
        observer({"purpose": "idea", "prompt": "P" * 40, "prompt_chars": 40})
        line = json.loads(
            (ab.SESSION_LOG_DIR / "dddd4444.jsonl").read_text(encoding="utf-8").strip()
        )
        assert line["event"] == "llm_usage"
        assert "prompt" not in line and line["transcript"]["sha256"]
        assert (ab.SESSION_LOG_DIR / "dddd4444_artifacts").is_dir()

    def test_a_failed_artifact_write_still_logs_the_call(self, state, monkeypatch):
        import web.routes.agent_backtest as ab

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(ab, "_write_session_artifact", _boom)
        out = ab._offload_transcript("aaaa1111", state, {"prompt": "x", "purpose": "i"})
        # Evidence about the run must never become part of the run's failure.
        assert "transcript" not in out and "prompt" not in out


class TestLiveHeartbeat:
    """The silent failure is the absence of events, not their content.

    A hung LLM call, a swelling process, a round burning its budget in minutes —
    none of them write anything. A fixed-interval pulse turns "nothing happened"
    from an unmeasurable gap into a measured one.
    """

    @pytest.fixture
    def running(self):
        import web.routes.agent_backtest as ab

        state = {
            "steps": [{"ts": 0.0, "msg": "loading bars"}],
            "phases": [
                {"label": "Backtest loop", "status": "running", "detail": "3/5"}
            ],
            "timeline": [
                {"key": "llm1", "lane": "llm", "label": "idea", "t0": 0.0, "t1": None},
                {"key": "d1", "lane": "data", "label": "bars", "t0": 0.0, "t1": 5.0},
            ],
            "tokens_in": 1000,
            "tokens_cache_read": 5000,
            "max_total_tokens": 10_000,
            "provider_cost_usd": 0.42,
        }
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS["eeee5555"] = state
        yield state
        with ab._AGENT_LOCK:
            ab._AGENT_PROGRESS.pop("eeee5555", None)

    def test_the_snapshot_answers_where_is_it_stuck(self, running):
        import web.routes.agent_backtest as ab

        snap = ab._heartbeat_snapshot(running, ab._WorkerState())
        assert [s["key"] for s in snap["open_spans"]] == ["llm1"]  # closed one drops
        # The single field that answers "is it hung": how long the oldest open
        # piece of work has been open. t0=0.0 is a real epoch, not "missing" —
        # `float(t0 or now)` would report an ancient span as brand new, hiding
        # a stall exactly where you go looking for one.
        assert snap["stalled_s"] > 1e9
        assert snap["phase"].startswith("Backtest loop")
        assert snap["last_step"] == "loading bars"

    def test_the_budget_is_reported_in_the_unit_the_ceiling_uses(self, running):
        import web.routes.agent_backtest as ab

        snap = ab._heartbeat_snapshot(running, ab._WorkerState())
        # 1000 input + 5000 cache-read × 0.1 = 1500 input-equivalent, not 6000.
        # Reporting raw tokens here would disagree with the cap that stops the run.
        assert snap["budget_used"] == 1500
        assert snap["budget_pct"] == 15.0
        assert snap["provider_cost_usd"] == 0.42

    def test_the_pulse_stops_itself_when_the_run_ends(self, running):
        import web.routes.agent_backtest as ab

        thread = ab._start_heartbeat(
            "eeee5555",
            ab._WorkerState(),
            continuous_mode=False,
            max_hours=1.0,
            interval=0.05,
        )
        time.sleep(0.12)
        with ab._AGENT_LOCK:
            running["done"] = True
        thread.join(timeout=2.0)
        # A daemon left beating forever is worse than no telemetry: the worker
        # has a dozen exits and one of them would always forget to stop it.
        assert not thread.is_alive()
        beats = [
            json.loads(ln)
            for ln in (ab.SESSION_LOG_DIR / "eeee5555.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        assert beats and all(b["event"] == "run_heartbeat" for b in beats)
        assert beats[-1]["final"] is True

    def test_a_vanished_run_does_not_leave_a_beating_thread(self):
        import web.routes.agent_backtest as ab

        thread = ab._start_heartbeat(
            "ffff6666",  # never registered in _AGENT_PROGRESS
            ab._WorkerState(),
            continuous_mode=True,
            max_hours=1.0,
            interval=0.05,
        )
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert not (ab.SESSION_LOG_DIR / "ffff6666.jsonl").exists()

    def test_resident_memory_is_read_or_honestly_absent(self):
        rss = nau_provenance.process_rss_bytes()
        assert rss is None or rss > 1_000_000


class TestEffectiveConfig:
    def test_the_gates_that_decided_the_run_are_recorded(self):
        import web.routes.agent_backtest as ab

        cfg = ab._effective_run_config()
        # Each of these is env-overridable, so two runs with identical logs can
        # have applied different gates.
        for key in ("min_trades", "winless_round_limit", "holdout_days"):
            assert key in cfg
        # The budget counter's UNIT, not just its ceiling: change the weights
        # and the same cap means a different run length.
        assert cfg["token_usage_weights"]["tokens_cache_read"] == 0.1


def _log(tmp_path, run_id: str, events: list[dict]):
    path = tmp_path / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    return path


_START = {
    "ts": "2026-08-14T10:00:00+00:00",
    "event": "session_start",
    "symbol": "QQQ",
    "intervals": ["1-HOUR"],
    "model": "claude-opus-5",
    "n_iterations": 5,
    "search_seed": "abcd1234",
}
_END = {
    "ts": "2026-08-14T10:30:00+00:00",
    "event": "session_end",
    "outcome": "budget",
    "completed_rounds": 1,
}


class TestReviewDocument:
    def test_absence_is_stated_not_rendered_as_zero(self, tmp_path):
        """ "0 aday geçti" and "kapı hiç koşmadı" are different runs."""
        _log(
            tmp_path,
            "run00001",
            [
                _START,
                {
                    "event": "backtest_result",
                    "n_trades": 90,
                    "metrics": {"sharpe": 1.0},
                },
                _END,
            ],
        )
        md = auto_review.build_review("run00001", tmp_path)
        assert "hiçbir eleme kapısı karar üretmedi" in md
        assert "robustluk taraması HİÇ başlamadı" in md

    def test_a_run_without_provenance_says_which_code_is_unknown(self, tmp_path):
        _log(tmp_path, "run00002", [_START, _END])
        md = auto_review.build_review("run00002", tmp_path)
        assert "`run_env` kaydı YOK" in md
        assert "provenance kaydı yok" in md  # ve tekrar edilemezlik gerekçesi

    def test_provenance_surfaces_the_commit_and_the_dirty_tree(self, tmp_path):
        _log(
            tmp_path,
            "run00003",
            [
                _START,
                {
                    "event": "run_env",
                    "git": {
                        "short": "9f121c9f1",
                        "branch": "main",
                        "dirty": True,
                        "dirty_files": 5,
                    },
                    "packages": {"nautilus_trader": "1.230.0"},
                    "env_overrides": {"NAU_STRICT": "1"},
                    "python": "3.12.10",
                },
                _END,
            ],
        )
        md = auto_review.build_review("run00003", tmp_path)
        assert "9f121c9f1" in md and "1.230.0" in md
        # A dirty tree means the SHA alone cannot reproduce the run — that is
        # an alarm, not a footnote.
        assert "KİRLİ ağaçtan çıktı" in md

    def test_repeated_alarms_are_counted_not_repeated(self, tmp_path):
        _log(
            tmp_path,
            "run00004",
            [
                _START,
                *[
                    {"event": "degraded", "what": "custom block", "reason": "boom"}
                    for _ in range(4)
                ],
                _END,
            ],
        )
        md = auto_review.build_review("run00004", tmp_path)
        alarm_lines = [ln for ln in md.splitlines() if "degraded — custom block" in ln]
        # One alarm repeated four times would push every other alarm off screen.
        assert len(alarm_lines) == 1 and "×4" in alarm_lines[0]

    def test_a_missing_transcript_is_called_out_as_a_blind_spot(self, tmp_path):
        _log(
            tmp_path,
            "run00005",
            [
                _START,
                {
                    "event": "llm_usage",
                    "purpose": "idea",
                    "usage": {"input_tokens": 10},
                },
                _END,
            ],
        )
        md = auto_review.build_review("run00005", tmp_path)
        assert "Transkript yok" in md
        assert "aynı prompt tekrar kurulamaz" in md

    def test_a_transcript_is_pointed_at_rather_than_inlined(self, tmp_path):
        _log(
            tmp_path,
            "run00006",
            [
                _START,
                {
                    "event": "llm_usage",
                    "purpose": "idea",
                    "usage": {"input_tokens": 10},
                    "transcript": {"path": "llm_0001_idea.json.gz", "clipped": True},
                },
                _END,
            ],
        )
        md = auto_review.build_review("run00006", tmp_path)
        assert "_artifacts/" in md and "kırpıldı" in md

    def test_step_messages_are_counted_not_carried(self, tmp_path):
        _log(
            tmp_path,
            "run00007",
            [_START, *[{"event": "step", "msg": "x"} for _ in range(30)], _END],
        )
        md = auto_review.build_review("run00007", tmp_path)
        # 90% of the file is narration; counting it keeps the document readable.
        assert "30 adım mesajı" in md

    def test_a_corrupt_line_does_not_lose_the_rest_of_the_run(self, tmp_path):
        path = tmp_path / "run00008.jsonl"
        path.write_text(
            json.dumps(_START) + "\n{ this is not json\n" + json.dumps(_END) + "\n",
            encoding="utf-8",
        )
        assert "budget" in auto_review.build_review("run00008", tmp_path)

    def test_write_review_is_atomic_and_repeatable(self, tmp_path):
        _log(tmp_path, "run00009", [_START, _END])
        first = auto_review.write_review("run00009", sessions_dir=tmp_path)
        again = auto_review.write_review("run00009", sessions_dir=tmp_path)
        assert first == again == tmp_path / "run00009_review.md"
        assert first.read_text(encoding="utf-8").startswith("# AUTO review")
        # A half-written document is worse than none: the temp file must be gone.
        assert not list(tmp_path.glob("*.tmp"))


class TestEveryFinishedRunLeavesAReview:
    def test_session_end_writes_the_document_next_to_the_log(self):
        import web.routes.agent_backtest as ab

        ab._session_log("bbbb2222", "session_start", symbol="QQQ")
        ab._session_log("bbbb2222", "session_end", outcome="winner")
        review = ab.SESSION_LOG_DIR / "bbbb2222_review.md"
        # Asked-for documents are documents nobody reads.
        assert review.exists() and "AUTO review" in review.read_text(encoding="utf-8")

    def test_an_interrupted_run_is_still_reviewable_over_http(self):
        """The route rebuilds from the log, so a run that never reached
        session_end — and therefore never wrote a review file — has one too."""
        from fastapi.testclient import TestClient

        import server
        import web.routes.agent_backtest as ab

        (ab.SESSION_LOG_DIR / "abcdef01.jsonl").write_text(
            json.dumps(_START) + "\n", encoding="utf-8"
        )
        assert not (ab.SESSION_LOG_DIR / "abcdef01_review.md").exists()
        client = TestClient(server.app)
        r = client.get("/sessions/abcdef01/review")
        assert r.status_code == 200 and "AUTO review" in r.text
        assert "<table>" in r.text  # rendered, not a wall of pipes
        raw = client.get("/sessions/abcdef01/review?raw=1")
        assert "markdown" in raw.headers["content-type"]
        assert raw.text.startswith("# AUTO review")

    def test_the_review_route_refuses_a_path_shaped_run_id(self):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        assert client.get("/sessions/..%2F..%2Fetc/review").status_code in (400, 404)
        assert client.get("/sessions/00000000/review").status_code == 404

    def test_a_broken_review_does_not_mark_the_audit_log_degraded(self, monkeypatch):
        """A failed *review* is not a failed *audit log*."""
        import web.routes.agent_backtest as ab

        monkeypatch.setattr(
            ab, "_write_run_review", lambda run_id: (_ for _ in ()).throw(OSError("x"))
        )
        with pytest.raises(OSError):
            ab._session_log("cccc3333", "session_end", outcome="winner")
        assert (ab.SESSION_LOG_DIR / "cccc3333.jsonl").exists()
