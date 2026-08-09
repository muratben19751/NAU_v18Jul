"""data.py's file lock + atomic-write helpers (DeepR 2026-08-09 [ORTA]).

_cache_lock/_atomic_to_parquet/_atomic_write_json had zero direct tests.
_cache_lock's stale-lock-breaking logic has broken for real once (M121):
the stale threshold used to equal the wait-timeout (60s), and a long
backfill (3 years of 1m bars, ~10 min hold) routinely exceeded that, so a
second process broke a LIVE lock mid-write. The fix raised the stale
threshold far above any legitimate hold; this file locks that in.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import json
import os
import threading
import time

import pandas as pd
import pytest

import data


class TestCacheLockBasics:
    def test_acquire_creates_and_release_removes_the_lock_file(self, tmp_path):
        lock_path = tmp_path / "x.lock"

        with data._cache_lock(lock_path):
            assert lock_path.exists()

        assert not lock_path.exists()

    def test_lock_file_is_removed_even_if_the_body_raises(self, tmp_path):
        lock_path = tmp_path / "x.lock"

        with pytest.raises(RuntimeError):
            with data._cache_lock(lock_path):
                assert lock_path.exists()
                raise RuntimeError("boom")

        assert not lock_path.exists()

    def test_second_acquirer_waits_for_the_first_to_release(self, tmp_path):
        lock_path = tmp_path / "x.lock"
        events: list[str] = []
        released = threading.Event()

        def _holder():
            with data._cache_lock(lock_path):
                events.append("holder-acquired")
                released.wait(timeout=5)
                events.append("holder-released")

        t = threading.Thread(target=_holder)
        t.start()
        # Give the holder a moment to actually acquire before the second
        # attempt starts racing it.
        for _ in range(50):
            if "holder-acquired" in events:
                break
            time.sleep(0.02)

        released.set()  # let the holder finish promptly
        with data._cache_lock(lock_path, timeout=5.0):
            events.append("second-acquired")
        t.join(timeout=5)

        assert events == ["holder-acquired", "holder-released", "second-acquired"]

    def test_timeout_error_when_a_live_lock_cannot_be_acquired_in_time(self, tmp_path):
        lock_path = tmp_path / "x.lock"
        holder_ready = threading.Event()
        release = threading.Event()

        def _holder():
            with data._cache_lock(lock_path):
                holder_ready.set()
                release.wait(timeout=5)

        t = threading.Thread(target=_holder)
        t.start()
        holder_ready.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError):
                with data._cache_lock(lock_path, timeout=0.3):
                    pass
        finally:
            release.set()
            t.join(timeout=5)


class TestStaleLockThreshold:
    """M121: the stale-breaking threshold must stay well above any real
    hold duration -- it used to equal the wait-timeout (60s) and broke
    live long-backfill locks. Age is set directly via os.utime rather than
    actually sleeping."""

    def test_a_lock_younger_than_stale_after_is_never_broken(self, tmp_path):
        lock_path = tmp_path / "x.lock"
        # Simulate the exact M121 shape: a lock held ~65s (over the OLD
        # 60s threshold) but the default stale_after is 1800s.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        old = time.time() - 65
        os.utime(lock_path, (old, old))

        with pytest.raises(TimeoutError):
            with data._cache_lock(lock_path, timeout=0.3):
                pass

        assert lock_path.exists(), "a live (merely 65s-old) lock must not be broken"

    def test_a_lock_older_than_stale_after_is_broken(self, tmp_path):
        lock_path = tmp_path / "x.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        ancient = time.time() - 2000  # > default stale_after=1800
        os.utime(lock_path, (ancient, ancient))

        # A short timeout is enough: the stale check fires once the
        # deadline is reached, not after a long wait.
        with data._cache_lock(lock_path, timeout=0.3, stale_after=1800.0):
            pass  # did not raise TimeoutError -- the abandoned lock was broken


class TestAtomicToParquet:
    def test_happy_path_round_trips(self, tmp_path):
        path = tmp_path / "out.parquet"
        df = pd.DataFrame({"a": [1, 2, 3]})

        data._atomic_to_parquet(df, path)

        assert path.exists()
        pd.testing.assert_frame_equal(pd.read_parquet(path), df)

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        # A dedicated subdir, not tmp_path itself: the autouse
        # _isolate_session_logs fixture also creates tmp_path/agent_sessions,
        # which would otherwise show up in the directory listing below.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        path = out_dir / "out.parquet"
        data._atomic_to_parquet(pd.DataFrame({"a": [1]}), path)

        assert list(out_dir.iterdir()) == [path]

    def test_failure_leaves_no_tmp_and_no_target(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        path = out_dir / "out.parquet"

        def _boom(self, *a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", _boom)

        with pytest.raises(OSError):
            data._atomic_to_parquet(pd.DataFrame({"a": [1]}), path)

        assert list(out_dir.iterdir()) == []

    def test_failure_does_not_clobber_an_existing_file(self, tmp_path, monkeypatch):
        path = tmp_path / "out.parquet"
        data._atomic_to_parquet(pd.DataFrame({"a": ["original"]}), path)

        monkeypatch.setattr(
            pd.DataFrame,
            "to_parquet",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError()),
        )
        with pytest.raises(OSError):
            data._atomic_to_parquet(pd.DataFrame({"a": ["new"]}), path)

        assert pd.read_parquet(path)["a"].tolist() == ["original"]


class TestAtomicWriteJson:
    def test_happy_path_round_trips(self, tmp_path):
        path = tmp_path / "out.json"
        data._atomic_write_json({"a": 1, "b": [1, 2, 3]}, path)

        assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        path = out_dir / "out.json"
        data._atomic_write_json({"a": 1}, path)

        assert list(out_dir.iterdir()) == [path]

    def test_failure_leaves_no_tmp_and_no_target(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        path = out_dir / "out.json"

        with pytest.raises(TypeError):
            data._atomic_write_json({"a": object()}, path)  # not JSON-serializable

        assert list(out_dir.iterdir()) == []

    def test_failure_does_not_clobber_an_existing_file(self, tmp_path):
        path = tmp_path / "out.json"
        data._atomic_write_json({"a": "original"}, path)

        with pytest.raises(TypeError):
            data._atomic_write_json({"a": object()}, path)

        assert json.loads(path.read_text()) == {"a": "original"}
