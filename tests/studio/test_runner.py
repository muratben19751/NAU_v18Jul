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

    cfg = DeployConfig(
        environment="paper",
        instruments="active",
        capital=25_000.0,
        kill_switch_daily_pct=3.0,
        gate_enabled=False,
        gate_min_objective=0.8,
    )
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


def test_the_deploy_artifact_resolves_to_a_real_composed_strategy(artifact):
    """Drives the actual deploy-time mechanism, not just the string it uses.

    Nautilus's ``StrategyFactory.create()`` resolves ``strategy_path``/
    ``config_path`` via a bare ``importlib.import_module(module) +
    getattr(mod, cls)`` (``nautilus_trader.common.config.resolve_path``) — a
    plain string this repo's own tooling does not track as an import.
    ``test_the_deploy_artifact_lowers_onto_a_sandbox_node`` only asserts the
    string values (``"composer:ComposedStrategy"``); nothing exercises the
    resolution + construction chain itself, so a typo or a renamed/moved
    ``ComposedStrategy``/``ComposedStrategyConfig`` would only surface at a
    real deploy. Permanently valuable regardless of whether either class ever
    moves out of composer.py.
    """
    from nautilus_trader.trading.config import StrategyFactory

    import composer

    cfg = build_node_config(artifact, trader_id="STUDIO-TEST")

    strategy = StrategyFactory.create(cfg.strategies[0])

    assert isinstance(strategy, composer.ComposedStrategy)
    assert isinstance(strategy.config, composer.ComposedStrategyConfig)
    assert str(strategy.config.instrument_id) == "BTCUSDT-LINEAR.BYBIT"
    assert json.loads(strategy.config.spec_json) == artifact["spec"]


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
    artifact["artifact_schema"] = 1  # the counts-only shape

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


class _FakeAccount:
    def __init__(self):
        self.base_currency = None


class _FakePortfolio:
    """The two calls the kill switch's default reader makes on a Portfolio."""

    def __init__(self):
        self.pnls: dict = {}
        self.account_obj = _FakeAccount()

    def total_pnls(self, venue=None):
        return self.pnls

    def account(self, venue=None):
        return self.account_obj


class _FakeCache:
    def __init__(self):
        self.open: list = []
        self.closed: list = []

    def positions_open(self, venue=None):
        return self.open

    def positions_closed(self, venue=None):
        return self.closed


class _FakeNode:
    """Stands in for a TradingNode: run_async blocks until stop()."""

    def __init__(self, config=None, fail: str | None = None):
        self.config = config
        self._fail = fail
        self._stop = None
        self.disposed = False
        self.strategy = _FakeStrategy()
        self.portfolio = _FakePortfolio()
        self.cache = _FakeCache()

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
    runner.launch("dep002", artifact)  # must not raise

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
        {"deploy_id": "c", "status": "running"},  # this one really is up
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

    time.sleep(0.2)  # give a stray status callback time to arrive
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


# ── kill switch ─────────────────────────────────────────────────────────────
#
# The fixture artifact is capital 25,000 USDT with a 3% daily limit, so the
# number that matters throughout is −750.00.


def _armed(artifact, pnl: list[float], deploy_id: str = "dep010", **kw):
    """A launched deployment whose account PnL the test moves via `pnl[0]`."""
    runner, seen = _runner(node_factory=_FakeNode, pnl_reader=lambda _d: pnl[0], **kw)
    runner.launch(deploy_id, artifact)
    return runner, seen


def test_the_kill_switch_pauses_the_deployment_that_breaches_its_limit(artifact):
    """Until 2026-08-17 this was decorative: the modal offered "Pause at −3%
    day", the artifact recorded 3.0, and nothing ever compared it to a PnL."""
    pnl = [0.0]
    runner, seen = _armed(artifact, pnl)
    node = runner._nodes["dep010"].node

    assert runner.check_kill_switches() == []  # the day's baseline is taken here
    pnl[0] = -749.0  # −2.996%: a bad day is not a breach
    assert runner.check_kill_switches() == []

    pnl[0] = -750.01
    assert runner.check_kill_switches() == ["dep010"]

    _wait(lambda: not node.strategy.is_running)
    assert runner.is_running("dep010"), "pause keeps the node and its feed up"
    _d, status, reason = seen[-1]
    assert status == "paused"
    assert "-3.00% on the day" in reason and "limit" in reason
    runner.stop("dep010")


def test_an_unreadable_account_is_not_read_as_a_flat_day(artifact):
    """`None` means "cannot see the account", which is the moment to keep
    watching — not to conclude the day is flat and take it as a baseline."""
    runner, _seen = _runner(node_factory=_FakeNode, pnl_reader=lambda _d: None)
    runner.launch("dep011", artifact)

    assert runner.check_kill_switches() == []
    assert runner._armed["dep011"].day == "", "a None was banked as today's start"
    runner.stop("dep011")


def test_the_day_is_measured_from_its_first_reading(artifact):
    """A node started mid-drawdown must not trip on yesterday's loss: the
    artifact says *daily*, and lifetime PnL is not that."""
    pnl = [-5_000.0]
    runner, _seen = _armed(artifact, pnl)

    assert runner.check_kill_switches() == []
    assert runner.check_kill_switches() == []  # still flat *for today*

    pnl[0] = -5_750.01
    assert runner.check_kill_switches() == ["dep010"]
    runner.stop("dep010")


def test_a_new_utc_day_re_baselines(artifact):
    now = [1_755_000_000.0]
    pnl = [0.0]
    runner, _seen = _armed(artifact, pnl, clock=lambda: now[0])

    runner.check_kill_switches()
    pnl[0] = -700.0  # −2.8%: survives the day
    assert runner.check_kill_switches() == []

    now[0] += 86_400
    runner.check_kill_switches()  # first reading of the new day → baseline −700
    pnl[0] = -1_400.0  # another −2.8%, measured from the new baseline
    assert runner.check_kill_switches() == []

    pnl[0] = -1_451.0
    assert runner.check_kill_switches() == ["dep010"]
    runner.stop("dep010")


def test_the_switch_fires_once_not_on_every_poll(artifact):
    pnl = [0.0]
    runner, seen = _armed(artifact, pnl)
    runner.check_kill_switches()

    pnl[0] = -2_000.0
    assert runner.check_kill_switches() == ["dep010"]
    assert runner.check_kill_switches() == []

    assert [s for _d, s, _e in seen].count("paused") == 1
    runner.stop("dep010")


def test_resume_re_arms_the_switch_from_the_current_balance(artifact):
    """Keeping the old baseline would re-fire on the next poll, making Resume a
    button that does nothing. The cost is stated on the row: resuming grants
    the deployment another `limit_pct` for the same day."""
    pnl = [0.0]
    runner, _seen = _armed(artifact, pnl)
    runner.check_kill_switches()
    pnl[0] = -800.0
    assert runner.check_kill_switches() == ["dep010"]

    runner.resume("dep010")

    assert runner.check_kill_switches() == []  # re-baselined at −800
    pnl[0] = -1_550.01  # another −750 from there
    assert runner.check_kill_switches() == ["dep010"]
    runner.stop("dep010")


def test_off_arms_nothing(artifact):
    artifact["kill_switch_daily_pct"] = None
    runner, seen = _runner(node_factory=_FakeNode, pnl_reader=lambda _d: -1e9)
    runner.launch("dep012", artifact)

    assert runner.check_kill_switches() == []
    assert [s for _d, s, _e in seen] == ["running"]
    runner.stop("dep012")


def test_a_switch_with_no_denominator_says_so_on_the_row(artifact):
    """A percentage needs something to be a percentage *of*. Silently not
    arming would leave the operator believing a switch is watching."""
    artifact["capital"] = 0.0
    runner, seen = _runner(node_factory=_FakeNode)
    runner.launch("dep013", artifact)

    assert seen[-1][1] == "running"
    assert "kill switch INACTIVE" in (seen[-1][2] or "")
    assert "dep013" not in runner._armed
    runner.stop("dep013")


def test_the_monitor_thread_fires_it_with_nobody_calling_check(artifact):
    """The defect being fixed was a control that existed and was never
    evaluated — a `check_kill_switches` nothing calls is the same defect."""
    pnl = [0.0]
    runner, seen = _armed(artifact, pnl, deploy_id="dep014", poll_s=0.02)

    _wait(lambda: runner._armed["dep014"].day != "")  # the thread took a baseline
    pnl[0] = -1_000.0

    _wait(lambda: any(s == "paused" for _d, s, _e in seen))
    runner.stop("dep014")


def test_the_default_reader_reads_the_accounts_currency(artifact):
    """No pnl_reader injected: the path a real node actually takes."""
    from nautilus_trader.model.currencies import USD, USDT
    from nautilus_trader.model.objects import Money

    runner, _seen = _runner(node_factory=_FakeNode)
    runner.launch("dep015", artifact)
    node = runner._nodes["dep015"].node

    assert runner._read_pnl("dep015") == 0.0  # no positions ⇒ genuinely flat

    # `total_pnls` returns {} both for "no positions" and for a failed internal
    # lookup. With positions on the books the empty dict is the second case.
    node.cache.open = ["a position"]
    assert runner._read_pnl("dep015") is None

    node.portfolio.pnls = {USDT: Money(-12.5, USDT), USD: Money(3.0, USD)}
    node.portfolio.account_obj.base_currency = USDT
    assert runner._read_pnl("dep015") == -12.5

    node.portfolio.account_obj.base_currency = None  # two currencies, no account
    assert runner._read_pnl("dep015") is None, "summed across currencies"
    runner.stop("dep015")
