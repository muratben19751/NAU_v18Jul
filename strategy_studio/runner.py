"""Deployment runner (STUDIO_SPEC Phase 6, INTEGRATION POINT #5).

Takes a deploy artifact and actually runs it. ``PaperRunner`` builds a real
NautilusTrader ``TradingNode`` in the **sandbox** environment: live Bybit market
data, ``SandboxExecutionClient`` fills. No exchange credentials, no orders
leaving the machine, no money — but a real node, real bars, real strategy
lifecycle, so ``pending → running`` means the node started rather than a timer
fired.

Why sandbox and not live
------------------------
``environment='live'`` is refused here, loudly, rather than quietly running as
paper. Two reasons, and both should be fixed before it is allowed:

* there are no exchange credentials anywhere in this app (only LLM keys), so
  "live" today could only mean paper wearing a live label;
* the deploy gate reads the single-run ``dsr``, which is undeflated PSR — a
  knowingly optimistic number (see ``backtest.probabilistic_sharpe``). Routing
  real orders through an optimistic gate is the wrong order of operations.

Process model
-------------
One node per deployment, each on its own thread with its own asyncio loop, held
in an in-process registry. That registry is the weak point and is treated as
such: it does not survive a restart, so a deployment left `running` in the
database with no node behind it is **reconciled at startup**
(``reconcile_orphans``) instead of lying on the panel.

Wiki References
---------------
Bkz: [[strategy_studio]], [[environment_contexts]], [[strategy_and_actor]]
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Studio timeframe → the Bybit kline code `_make_bybit_bar_type` understands.
_INTERVAL_FOR = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "12h": "720", "1d": "D",
}
_BAR_STEP = {
    "1": "1-MINUTE", "5": "5-MINUTE", "15": "15-MINUTE", "30": "30-MINUTE",
    "60": "1-HOUR", "240": "4-HOUR", "720": "12-HOUR", "D": "1-DAY",
}

# The venue the Bybit adapter registers itself under (one venue for every
# product type; the product is part of the instrument id, unlike the backtest
# path's synthetic BYBIT_LINEAR/BYBIT_SPOT split).
BYBIT_VENUE = "BYBIT"

SUPPORTED_ARTIFACT_SCHEMAS = (2,)


class RunnerError(Exception):
    """The artifact cannot be turned into a running node."""


def _bar_type_str(symbol: str, timeframe: str, product: str = "LINEAR") -> str:
    code = _INTERVAL_FOR.get(timeframe)
    if code is None:
        raise RunnerError(
            f"timeframe '{timeframe}' has no Bybit kline code "
            f"({', '.join(sorted(_INTERVAL_FOR))})")
    return f"{symbol}-{product}.{BYBIT_VENUE}-{_BAR_STEP[code]}-LAST-EXTERNAL"


def _instrument_id_str(symbol: str, product: str = "LINEAR") -> str:
    return f"{symbol}-{product}.{BYBIT_VENUE}"


def build_node_config(artifact: dict, *, trader_id: str, product: str = "LINEAR"):
    """Artifact → ``TradingNodeConfig`` for a sandbox node.

    Pure and import-light enough to unit-test without a network: it builds
    configs, it does not connect. One ``ComposedStrategy`` per instrument, all
    sharing the artifact's single spec (the spec is instrument-free by design).

    Raises:
        RunnerError: unknown artifact schema, a live environment, an unusable
            timeframe, or no instruments.
    """
    from nautilus_trader.adapters.bybit import BybitProductType
    from nautilus_trader.adapters.bybit.config import BybitDataClientConfig
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
    from nautilus_trader.common import Environment
    from nautilus_trader.common.config import InstrumentProviderConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.trading.config import ImportableStrategyConfig

    schema = artifact.get("artifact_schema")
    if schema not in SUPPORTED_ARTIFACT_SCHEMAS:
        raise RunnerError(
            f"artifact schema {schema!r} is not one this runner knows "
            f"({', '.join(map(str, SUPPORTED_ARTIFACT_SCHEMAS))}) — "
            "re-deploy the strategy to regenerate it")

    env = artifact.get("environment")
    if env == "live":
        raise RunnerError(
            "live deployment is not wired: this app holds no exchange "
            "credentials, and the deploy gate still reads the undeflated "
            "single-run DSR. Deploy as 'paper' — running live under a paper "
            "runner would be a live label on a simulated account")
    if env != "paper":
        raise RunnerError(f"unknown environment '{env}'")

    instruments = artifact.get("instruments") or []
    if not instruments:
        raise RunnerError("artifact carries no instruments to trade")

    spec_json = json.dumps(artifact["spec"])
    strategies = [
        ImportableStrategyConfig(
            strategy_path="composer:ComposedStrategy",
            config_path="composer:ComposedStrategyConfig",
            config={
                "instrument_id": _instrument_id_str(i["symbol"], product),
                "bar_type": _bar_type_str(i["symbol"], i["timeframe"], product),
                "spec_json": spec_json,
            },
        )
        for i in instruments
    ]

    # Only the deployed instruments are loaded: load_all would pull Bybit's
    # entire universe on every start.
    load_ids = [_instrument_id_str(i["symbol"], product) for i in instruments]
    capital = float(artifact.get("capital") or 0.0)
    # `product_types=None` means BYBIT_ALL_PRODUCTS in the factory — spot,
    # linear, inverse AND option. Measured: the node never finished connecting
    # and timed out after 60s. Pin it to the product the instrument ids are
    # built for; nothing here trades the others.
    product_type = {
        "LINEAR": BybitProductType.LINEAR,
        "SPOT": BybitProductType.SPOT,
        "INVERSE": BybitProductType.INVERSE,
    }.get(product.upper())
    if product_type is None:
        raise RunnerError(f"unsupported Bybit product '{product}'")

    return TradingNodeConfig(
        environment=Environment.SANDBOX,
        trader_id=trader_id,
        strategies=strategies,
        data_clients={
            BYBIT_VENUE: BybitDataClientConfig(
                # Public market data needs no credentials; leaving them None is
                # what keeps this runner incapable of touching an account.
                api_key=None,
                api_secret=None,
                product_types=[product_type],
                instrument_provider=InstrumentProviderConfig(load_ids=frozenset(load_ids)),
            )
        },
        exec_clients={
            BYBIT_VENUE: SandboxExecutionClientConfig(
                venue=BYBIT_VENUE,
                starting_balances=[f"{capital:.2f} USDT"],
                base_currency="USDT",
                bar_execution=True,
            )
        },
    )


# ── lifecycle ───────────────────────────────────────────────────────────────

# (deploy_id, status, error|None)
StatusSink = Callable[[str, str, str | None], None]


@dataclass
class _Node:
    thread: threading.Thread
    loop: Any
    node: Any


@dataclass
class PaperRunner:
    """Runs deployments as sandbox TradingNodes, one thread each."""

    on_status: StatusSink
    product: str = "LINEAR"
    # Injectable so the lifecycle can be tested without an engine or a network.
    node_factory: Callable[[Any], Any] | None = None
    _nodes: dict[str, _Node] = field(default_factory=dict, init=False)
    # Deployments being torn down on purpose — their exit is not a failure.
    _stopping: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # -- helpers ---------------------------------------------------------
    def _build_node(self, config):
        if self.node_factory is not None:
            return self.node_factory(config)
        from nautilus_trader.adapters.bybit.factories import (
            BybitLiveDataClientFactory,
        )
        from nautilus_trader.adapters.sandbox.factory import (
            SandboxLiveExecClientFactory,
        )
        from nautilus_trader.live.node import TradingNode

        node = TradingNode(config=config)
        node.add_data_client_factory(BYBIT_VENUE, BybitLiveDataClientFactory)
        node.add_exec_client_factory(BYBIT_VENUE, SandboxLiveExecClientFactory)
        node.build()
        return node

    def is_running(self, deploy_id: str) -> bool:
        with self._lock:
            return deploy_id in self._nodes

    def active_ids(self) -> set[str]:
        with self._lock:
            return set(self._nodes)

    # -- transitions -----------------------------------------------------
    BUILD_TIMEOUT = 30.0

    def launch(self, deploy_id: str, artifact: dict) -> None:
        """Start a node for `deploy_id`; report running/failed via on_status.

        Never raises into the caller: this runs as a background task off an
        HTTP response that has already been sent, so a failure has to reach the
        user through the deployment row, not through a traceback nobody sees.
        """
        try:
            config = build_node_config(
                artifact, trader_id=f"STUDIO-{deploy_id[:6].upper()}",
                product=self.product)
        except Exception as e:  # noqa: BLE001 — surfaced on the row
            self.on_status(deploy_id, "failed", str(e))
            return

        built = threading.Event()
        failure: list[str] = []
        # Set once the row has been told the node is up, so the serve thread
        # knows whether an exit still needs reporting.
        announced = threading.Event()

        def _serve() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            node = None
            try:
                # Built INSIDE the thread, after set_event_loop: a TradingNode
                # binds to the loop that is current at construction. Building
                # it on the caller's thread and running it here made every
                # client start against a loop that was not running — the exec
                # engine faked its way through, the data client's `_connect`
                # coroutine was never awaited, and the node timed out after 60s
                # with `DataEngine.check_connected() == False`.
                node = self._build_node(config)
                with self._lock:
                    self._nodes[deploy_id] = _Node(
                        thread=threading.current_thread(), loop=loop, node=node)
            except Exception as e:  # noqa: BLE001
                failure.append(str(e))
                built.set()
                loop.close()
                return
            try:
                built.set()
                loop.run_until_complete(node.run_async())
            except Exception as e:  # noqa: BLE001
                failure.append(str(e))
            finally:
                with self._lock:
                    stopping = deploy_id in self._stopping
                    self._stopping.discard(deploy_id)
                    self._nodes.pop(deploy_id, None)
                # The status goes out BEFORE teardown: dispose() can block, and
                # a hung teardown must not swallow the news that the node is
                # gone. A node can die long after launch() returned, so without
                # this the row keeps a green RUNNING badge until the next
                # restart. A deliberate stop() is exempt — the route records
                # 'stopped' itself.
                if announced.is_set() and not stopping:
                    self.on_status(
                        deploy_id, "failed",
                        failure[0] if failure
                        else "the node exited on its own (see the server log)")
                try:
                    node.dispose()
                except Exception:  # noqa: BLE001 — teardown noise, already reported
                    pass
                loop.close()

        t = threading.Thread(target=_serve, name=f"studio-deploy-{deploy_id[:6]}",
                             daemon=True)
        t.start()
        # Wait for the build, not for the run: run_async only returns when the
        # node is finished. `running` therefore means "the node was constructed
        # and handed to its loop", and anything that kills it later comes back
        # through the `finally` above.
        if not built.wait(timeout=self.BUILD_TIMEOUT):
            self.on_status(
                deploy_id, "failed",
                f"the node did not finish building within {self.BUILD_TIMEOUT:.0f}s")
            return
        if failure:
            self.on_status(deploy_id, "failed", failure[0])
            return
        self.on_status(deploy_id, "running", None)
        announced.set()

    def _strategies(self, deploy_id: str):
        with self._lock:
            entry = self._nodes.get(deploy_id)
        if entry is None:
            raise RunnerError(
                f"deployment {deploy_id[:6]} has no live node "
                "(the server was restarted, or it already stopped)")
        return entry, entry.node.trader.strategies()

    def pause(self, deploy_id: str) -> None:
        """Stop the strategies but keep the node and its data feed up.

        A node-level pause does not exist; stopping the strategies is the
        faithful equivalent — no new orders, subscriptions intact, so resume is
        cheap and does not re-handshake the venue.
        """
        entry, strategies = self._strategies(deploy_id)
        for s in strategies:
            if s.is_running:
                entry.loop.call_soon_threadsafe(s.stop)

    def resume(self, deploy_id: str) -> None:
        """`resume()`, not `start()`.

        A stopped Nautilus component refuses START — measured:
        `InvalidStateTrigger('STOPPED -> START')`, which left the UI with a
        Resume button that did nothing. RESUME is the transition the state
        machine has for exactly this. Unlike `reset()` it keeps the strategy's
        state, so indicators do not restart their warm-up.
        """
        entry, strategies = self._strategies(deploy_id)
        for s in strategies:
            if not s.is_running:
                entry.loop.call_soon_threadsafe(
                    s.resume if hasattr(s, "resume") else s.start)

    def stop(self, deploy_id: str) -> None:
        """Terminal. The thread's `finally` disposes the node and deregisters."""
        with self._lock:
            entry = self._nodes.get(deploy_id)
            if entry is None:
                return  # already gone; the row is set to stopped regardless
            # Marked before the stop is dispatched, so the serve thread reads
            # this exit as deliberate rather than reporting it as a failure.
            self._stopping.add(deploy_id)
        # `node.stop()` is the synchronous wrapper and drives the loop itself —
        # called from inside that same loop it raises "Cannot run the event
        # loop while another loop is running". The coroutine is the thing to
        # schedule.
        stop_async = getattr(entry.node, "stop_async", None)
        if stop_async is None:
            entry.loop.call_soon_threadsafe(entry.node.stop)
        else:
            asyncio.run_coroutine_threadsafe(stop_async(), entry.loop)


def reconcile_orphans(rows: list[dict], active: set[str]) -> list[tuple[str, str]]:
    """Deployments the database thinks are live but no node is behind.

    The node registry is in-process, so a restart leaves `running`/`paused`
    rows with nothing running. Returning them as (deploy_id, reason) rather
    than showing a green RUNNING badge for a node that does not exist.
    """
    orphans = []
    for row in rows:
        if row["status"] in ("running", "paused") and row["deploy_id"] not in active:
            orphans.append((
                row["deploy_id"],
                "runner process restarted — the node behind this deployment is gone",
            ))
    return orphans
