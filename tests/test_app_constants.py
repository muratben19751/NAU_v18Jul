"""app_constants.env_float / env_int (DeepR 2026-08-09 [ORTA]).

Character-for-character identical in backtest_robustness.py,
parallel_exec.py, and wfo_optimizer.py before this -- a bugfix to one
would silently not reach the other two. Consolidated here; each module
now imports it (`from app_constants import env_float as _env_float`)
instead of defining its own copy.
"""

from __future__ import annotations

from app_constants import env_float, env_int


class TestEnvFloat:
    def test_uses_the_env_value_when_set_and_parseable(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_TEST_FLOAT", "3.5")
        assert env_float("NAUTILUS_TEST_FLOAT", 1.0) == 3.5

    def test_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_TEST_FLOAT", raising=False)
        assert env_float("NAUTILUS_TEST_FLOAT", 1.0) == 1.0

    def test_falls_back_to_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_TEST_FLOAT", "")
        assert env_float("NAUTILUS_TEST_FLOAT", 1.0) == 1.0

    def test_falls_back_to_default_when_not_a_number(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_TEST_FLOAT", "not-a-number")
        assert env_float("NAUTILUS_TEST_FLOAT", 1.0) == 1.0


class TestEnvInt:
    def test_uses_the_env_value_when_set_and_parseable(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_TEST_INT", "7")
        assert env_int("NAUTILUS_TEST_INT", 1) == 7

    def test_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NAUTILUS_TEST_INT", raising=False)
        assert env_int("NAUTILUS_TEST_INT", 1) == 1

    def test_falls_back_to_default_when_not_a_number(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_TEST_INT", "not-a-number")
        assert env_int("NAUTILUS_TEST_INT", 1) == 1


class TestConsolidatedAcrossModules:
    """The 3 modules must now share ONE implementation, not 3 copies."""

    def test_backtest_robustness_uses_the_shared_implementation(self):
        import app_constants
        import backtest_robustness

        assert backtest_robustness._env_float is app_constants.env_float

    def test_parallel_exec_uses_the_shared_implementation(self):
        import app_constants
        import parallel_exec

        assert parallel_exec._env_float is app_constants.env_float

    def test_wfo_optimizer_uses_the_shared_implementation(self):
        import app_constants
        import wfo_optimizer

        assert wfo_optimizer._env_float is app_constants.env_float
        assert wfo_optimizer._env_int is app_constants.env_int
