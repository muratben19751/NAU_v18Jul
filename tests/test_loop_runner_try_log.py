"""loop_runner._try_log's failure path (DeepR 2026-08-09 [ORTA]).

Sibling code path web/routes/backtest.py's run() worker had the identical
bare `except Exception: pass` around _log_backtest, fixed 2026-08-08 (DeepR
[DÜŞÜK]) to log a warning instead -- this one wasn't. A persistently
failing log write (disk full, permission error) had no observable symptom
beyond /reports quietly staying empty.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

import loop_runner
from composer import ComposedStrategySpec
from state import IterationResult


def _bars() -> pd.DataFrame:
    return pd.DataFrame({"close": [1.0, 2.0]})


def _result() -> IterationResult:
    return IterationResult(
        id=7,
        strategy="composed:test",
        params={},
        metrics={},
        equity_curve=[],
        rationale="",
        error=None,
        timestamp=datetime.now(UTC),
    )


def _spec() -> ComposedStrategySpec:
    return ComposedStrategySpec(id="spec-1", name="Test", description="", blocks=[])


def test_a_log_write_failure_is_logged_not_swallowed_silently(monkeypatch, caplog):
    monkeypatch.setattr(
        "web.routes.backtest._log_backtest",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with caplog.at_level("WARNING"):
        loop_runner._try_log(_spec(), _result(), _bars())  # must not raise

    assert "_log_backtest failed" in caplog.text
    assert "7" in caplog.text  # the iteration id, for correlating with the run


def test_a_successful_log_write_stays_quiet(monkeypatch, caplog):
    monkeypatch.setattr(
        "web.routes.backtest._log_backtest", lambda *a, **k: "2026-08-09T00:00:00"
    )
    result = _result()

    with caplog.at_level("WARNING"):
        loop_runner._try_log(_spec(), result, _bars())

    assert caplog.text == ""
    assert result.log_ts == "2026-08-09T00:00:00"


@pytest.mark.parametrize("bad_result_id", [None, "not-an-int"])
def test_logging_the_failure_itself_never_raises(monkeypatch, caplog, bad_result_id):
    """The warning call reads result.id defensively (getattr with a
    fallback) -- an id that's missing or an unexpected type must not turn
    the failure-logging path into a second, unhandled exception."""
    monkeypatch.setattr(
        "web.routes.backtest._log_backtest",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = _result()
    result.id = bad_result_id

    with caplog.at_level("WARNING"):
        loop_runner._try_log(_spec(), result, _bars())  # must not raise

    assert "_log_backtest failed" in caplog.text
