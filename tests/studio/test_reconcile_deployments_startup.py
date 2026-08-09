"""strategy_studio._reconcile_deployments_at_startup (DeepR 2026-08-09 [ORTA]).

A stale `running` deployment row after a restart is a green badge for
something not actually running -- reconcile_deployments()'s own docstring
calls this "worse than a failure, because it looks fine." The startup
guard around it used to be a silent `except: pass`; if reconciliation
itself failed, the app booted straight into that exact misleading state
with zero trace anywhere.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import web.routes.strategy_studio as ss


def test_a_reconciliation_failure_is_logged_not_swallowed_silently(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(ss, "_reconcile_deployments", _boom)

    with caplog.at_level("WARNING"):
        ss._reconcile_deployments_at_startup()  # must not raise

    assert "reconciliation failed" in caplog.text


def test_a_successful_reconciliation_stays_quiet(monkeypatch, caplog):
    monkeypatch.setattr(ss, "_reconcile_deployments", lambda: None)

    with caplog.at_level("WARNING"):
        ss._reconcile_deployments_at_startup()

    assert caplog.text == ""
