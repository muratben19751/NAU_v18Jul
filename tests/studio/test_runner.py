"""Deployment INTEGRATION POINT #5: the paper runner (runner.py).

No network and no engine here. What this repo owns is the artifact → node
config lowering, the refusals, and the lifecycle bookkeeping — the parts that
decide whether the panel tells the truth. The node itself is injected.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from strategy_studio.deploy import DeployConfig, prepare_deployment
from strategy_studio.runner import (
    PaperRunner,
    RunnerError,
    build_node_config,
    reconcile_orphans,
)


@pytest.fixture()
def artifact() -> dict:
    from scripts.seed_studio import build_engine_fixture

    cfg = DeployConfig(environment="paper", instruments="active",
                       capital=25_000.0, kill_switch_daily_pct=3.0,
                       gate_enabled=False, gate_min_objective=0.8)
    return json.loads(prepare_deployment(build_engine_fixture(), None, cfg))


# ── artifact → node config ──────────────────────────────────────────────────


def test_the_deploy_artifact_lowers_onto_a_sandbox_node(artifact):
    from nautilus_trader.common import Environment

    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    assert cfg.environment == Environment.SANDBOX
    assert list(cfg.data_clients) == ["BYBIT"] == list(cfg.exec_clients)
    # One ComposedStrategy per instrument, all sharing the artifact's spec.
    assert len(cfg.strategies) == len(artifact["instruments"])
    s = cfg.strategies[0]
    assert s.strategy_path == "composer:ComposedStrategy"
    assert s.config["instrument_id"] == "BTCUSDT-LINEAR.BYBIT"
    assert s.config["bar_type"] == "BTCUSDT-LINEAR.BYBIT-1-HOUR-LAST-EXTERNAL"
    assert json.loads(s.config["spec_json"]) == artifact["spec"]


def test_the_sandbox_account_starts_with_the_deployed_capital(artifact):
    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    exec_cfg = cfg.exec_clients["BYBIT"]
    assert exec_cfg.starting_balances == ["25000.00 USDT"]
    assert exec_cfg.venue == "BYBIT"


def test_no_credentials_are_configured_anywhere(artifact):
    """The runner's inability to touch an account is structural, not a promise."""
    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    data_cfg = cfg.data_clients["BYBIT"]
    assert data_cfg.api_key is None and data_cfg.api_secret is None


def test_only_the_deployed_instruments_are_loaded(artifact):
    """load_all would pull Bybit's whole universe on every start."""
    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    provider = cfg.data_clients["BYBIT"].instrument_provider
    assert provider.load_all is False
    assert set(provider.load_ids) == {"BTCUSDT-LINEAR.BYBIT"}


# ── refusals ────────────────────────────────────────────────────────────────


def test_live_is_refused_rather_than_quietly_run_as_paper(artifact):
    artifact["environment"] = "live"

    with pytest.raises(RunnerError) as e:
        build_node_config(artifact, trader_id="STUDIO-TEST")
    assert "credentials" in str(e.value) and "paper" in str(e.value)


def test_an_artifact_from_an_unknown_schema_is_refused(artifact):
    """A runner that guesses at missing fields runs a different strategy."""
    artifact["artifact_schema"] = 1        # the counts-only shape

    with pytest.raises(RunnerError, match="schema"):
        build_node_config(artifact, trader_id="STUDIO-TEST")


def test_a_timeframe_the_venue_has_no_kline_for_is_refused(artifact):
    artifact["instruments"] = [{"symbol": "BTCUSDT", "timeframe": "3h"}]

    with pytest.raises(RunnerError, match="3h"):
        build_node_config(artifact, trader_id="STUDIO-TEST")


def test_an_artifact_with_no_instruments_is_refused(artifact):
    artifact["instruments"] = []

    with pytest.raises(RunnerError, match="no instruments"):
        build_node_config(artifact, trader_id="STUDIO-TEST")


# ── lifecycle ───────────────────────────────────────────────────────────────


class _FakeStrategy:
    """Mirrors the Nautilus component state machine where it matters: a
    STOPPED component refuses START and only accepts RESUME."""

    def __init__(self):
        self.is_running = True
        self.stopped_once = False

    def stop(self):
        self.is_running = False
        self.stopped_once = True

    def start(self):
        if self.stopped_once:
            raise RuntimeError("InvalidStateTrigger('STOPPED -> START')")
        self.is_running = True

    def resume(self):
        self.is_running = True


class _FakeNode:
    """Stands in for a TradingNode: run_async blocks until stop()."""

    def __init__(self, config=None, fail: str | None = None):
        self.config = config
        self._fail = fail
        self._stop = None
        self.disposed = False
        self.strategy = _FakeStrategy()

        class _Trader:
            def __init__(self, s):
                self._s = s

            def strategies(self):
                return [self._s]

        self.trader = _Trader(self.strategy)

    async def run_async(self):
        if self._fail:
            raise RuntimeError(self._fail)
        self._stop = asyncio.Event()
        await self._stop.wait()

    def stop(self):
        if self._stop is not None:
            self._stop.set()

    def dispose(self):
        self.disposed = True


def _runner(**kw):
    seen: list[tuple] = []
    r = PaperRunner(on_status=lambda d, s, e: seen.append((d, s, e)), **kw)
    return r, seen


def test_running_means_the_node_started(artifact):
    runner, seen = _runner(node_factory=_FakeNode)

    runner.launch("dep001", artifact)

    assert seen == [("dep001", "running", None)]
    assert runner.is_running("dep001")
    runner.stop("dep001")


def test_a_node_that_refuses_to_build_fails_the_row_instead_of_raising(artifact):
    """launch() runs after the HTTP response is sent — a traceback goes nowhere."""

    def _explode(config):
        raise RuntimeError("venue handshake failed")

    runner, seen = _runner(node_factory=_explode)
    runner.launch("dep002", artifact)   # must not raise

    assert seen == [("dep002", "failed", "venue handshake failed")]
    assert not runner.is_running("dep002")


def test_a_live_artifact_fails_the_row_with_the_reason(artifact):
    artifact["environment"] = "live"
    runner, seen = _runner(node_factory=_FakeNode)

    runner.launch("dep003", artifact)

    deploy_id, status, error = seen[0]
    assert (deploy_id, status) == ("dep003", "failed")
    assert "live deployment is not wired" in error


def test_pause_stops_the_strategies_but_keeps_the_node_up(artifact):
    runner, _seen = _runner(node_factory=_FakeNode)
    runner.launch("dep004", artifact)
    node = runner._nodes["dep004"].node

    runner.pause("dep004")
    _wait(lambda: not node.strategy.is_running)
    assert runner.is_running("dep004"), "the node itself must stay up"

    runner.resume("dep004")
    _wait(lambda: node.strategy.is_running)
    runner.stop("dep004")


def test_stop_tears_the_node_down_and_deregisters_it(artifact):
    runner, _seen = _runner(node_factory=_FakeNode)
    runner.launch("dep005", artifact)
    node = runner._nodes["dep005"].node

    runner.stop("dep005")

    _wait(lambda: not runner.is_running("dep005"))
    assert node.disposed, "the node was dropped without being disposed"


def test_acting_on_a_deployment_with_no_node_says_so(artifact):
    runner, _seen = _runner(node_factory=_FakeNode)

    with pytest.raises(RunnerError, match="no live node"):
        runner.pause("never-launched")


def _wait(predicate, timeout: float = 3.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached within timeout")


# ── reconciliation ──────────────────────────────────────────────────────────


def test_rows_left_live_by_a_restart_are_reported_as_orphans():
    """A green RUNNING badge for a node that does not exist is worse than a
    failure: it looks fine."""
    rows = [
        {"deploy_id": "a", "status": "running"},
        {"deploy_id": "b", "status": "paused"},
        {"deploy_id": "c", "status": "running"},   # this one really is up
        {"deploy_id": "d", "status": "stopped"},
    ]

    orphans = reconcile_orphans(rows, active={"c"})

    assert [d for d, _r in orphans] == ["a", "b"]
    assert all("restarted" in reason for _d, reason in orphans)


def test_a_node_that_dies_after_launch_flips_the_row_out_of_running(artifact):
    """Measured against real Bybit: the data client can fail to connect and the
    node times out ~60s later, long after launch() reported running. Without
    this the panel keeps a green badge until the next restart."""
    node = _FakeNode()
    runner, seen = _runner(node_factory=lambda cfg: node)
    runner.launch("dep006", artifact)
    assert seen == [("dep006", "running", None)]

    # From inside the node's own loop — that is how a connection timeout ends
    # it. Nobody called runner.stop().
    runner._nodes["dep006"].loop.call_soon_threadsafe(node.stop)

    _wait(lambda: len(seen) > 1)
    deploy_id, status, error = seen[-1]
    assert (deploy_id, status) == ("dep006", "failed")
    assert "exited on its own" in error


def test_a_deliberate_stop_is_not_reported_as_a_failure(artifact):
    runner, seen = _runner(node_factory=_FakeNode)
    runner.launch("dep007", artifact)

    runner.stop("dep007")

    _wait(lambda: not runner.is_running("dep007"))
    import time
    time.sleep(0.2)   # give a stray status callback time to arrive
    assert [s for _d, s, _e in seen] == ["running"]


def test_only_the_deployed_product_is_subscribed(artifact):
    """product_types=None means ALL Bybit products in the factory — spot,
    linear, inverse and option. Measured: the node never finished connecting."""
    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    types = cfg.data_clients["BYBIT"].product_types
    assert types is not None and len(types) == 1
    assert types[0].value == "linear"


def test_resume_uses_the_transition_a_stopped_component_accepts(artifact):
    """Measured on a real node: `start()` on a stopped strategy raises
    InvalidStateTrigger('STOPPED -> START'), so the UI's Resume button did
    nothing. RESUME is the transition for this, and it keeps strategy state —
    `reset()` would restart every indicator's warm-up."""
    runner, _seen = _runner(node_factory=_FakeNode)
    runner.launch("dep008", artifact)
    node = runner._nodes["dep008"].node

    runner.pause("dep008")
    _wait(lambda: not node.strategy.is_running)
    runner.resume("dep008")

    _wait(lambda: node.strategy.is_running)
    runner.stop("dep008")
