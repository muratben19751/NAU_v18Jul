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

Kill switch
-----------
``kill_switch_daily_pct`` travels in the artifact and is enforced *here* —
``check_kill_switches`` measures every armed deployment against its own day and
pauses the ones that breach. Until 2026-08-17 nothing read the field: the deploy
modal offered "Pause at −3% day", the artifact recorded it, a test asserted it
was written, and no code anywhere compared it to a PnL. A safety control that is
configured, displayed and never evaluated is worse than none — it is the reason
an operator does not watch the position themselves.

Wiki References
---------------
Bkz: [[strategy_studio]], [[environment_contexts]], [[strategy_and_actor]]
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Studio timeframe → the Bybit kline code `_make_bybit_bar_type` understands.
_INTERVAL_FOR = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "12h": "720",
    "1d": "D",
}
_BAR_STEP = {
    "1": "1-MINUTE",
    "5": "5-MINUTE",
    "15": "15-MINUTE",
    "30": "30-MINUTE",
    "60": "1-HOUR",
    "240": "4-HOUR",
    "720": "12-HOUR",
    "D": "1-DAY",
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
            f"({', '.join(sorted(_INTERVAL_FOR))})"
        )
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
            "re-deploy the strategy to regenerate it"
        )

    env = artifact.get("environment")
    if env == "live":
        raise RunnerError(
            "live deployment is not wired: this app holds no exchange "
            "credentials, and the deploy gate still reads the undeflated "
            "single-run DSR. Deploy as 'paper' — running live under a paper "
            "runner would be a live label on a simulated account"
        )
    if env != "paper":
        raise RunnerError(f"unknown environment '{env}'")

    instruments = artifact.get("instruments") or []
    if not instruments:
        raise RunnerError("artifact carries no instruments to trade")
    # Dotted ids are external-catalog instruments (QQQ.NASDAQ): historical
    # bars only, no Bybit websocket feed — a node built for one would sit
    # silently barless. Backtest/optimize accept them; deployment cannot.
    ext = sorted(i["symbol"] for i in instruments if "." in str(i.get("symbol", "")))
    if ext:
        raise RunnerError(
            f"external catalog instruments are backtest-only ({', '.join(ext)}) "
            "— no live Bybit feed exists for them; deactivate them before "
            "deploying"
        )

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
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset(load_ids)
                ),
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

# How often armed deployments are measured. This is a loss limit, not a stop
# order: it reacts within a poll, not within a tick.
KILL_SWITCH_POLL_S = 5.0

# deploy_id -> the account's PnL for the day so far, in the account currency;
# None when it cannot be read right now. None is NOT zero — reading "unknown"
# as "flat" would leave the switch armed and blind, which is the failure mode
# this whole mechanism exists to prevent.
PnlReader = Callable[[str], float | None]


@dataclass
class _Node:
    thread: threading.Thread
    loop: Any
    node: Any


@dataclass
class _KillSwitch:
    """One deployment's daily loss limit and the day it is measured against."""

    limit_pct: float  # positive magnitude: 3.0 == "pause at −3% on the day"
    capital: float
    day: str = ""  # UTC date the baseline belongs to ("" = not baselined yet)
    baseline: float = 0.0  # account PnL when that day started
    fired: bool = False


@dataclass
class PaperRunner:
    """Runs deployments as sandbox TradingNodes, one thread each."""

    on_status: StatusSink
    product: str = "LINEAR"
    # Injectable so the lifecycle can be tested without an engine or a network.
    node_factory: Callable[[Any], Any] | None = None
    # Same reason, for the kill switch: the breach decision has to be testable
    # without a funded account and a losing day.
    pnl_reader: PnlReader | None = None
    poll_s: float = KILL_SWITCH_POLL_S
    clock: Callable[[], float] = time.time
    _nodes: dict[str, _Node] = field(default_factory=dict, init=False)
    # Deployments being torn down on purpose — their exit is not a failure.
    _stopping: set[str] = field(default_factory=set, init=False)
    _armed: dict[str, _KillSwitch] = field(default_factory=dict, init=False)
    _monitor: threading.Thread | None = field(default=None, init=False)
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
                artifact,
                trader_id=f"STUDIO-{deploy_id[:6].upper()}",
                product=self.product,
            )
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
                        thread=threading.current_thread(), loop=loop, node=node
                    )
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
                    # Disarmed with the node it was watching. A switch left
                    # armed over a dead deployment would read `None` forever
                    # and keep the monitor thread alive for nothing.
                    self._armed.pop(deploy_id, None)
                # The status goes out BEFORE teardown: dispose() can block, and
                # a hung teardown must not swallow the news that the node is
                # gone. A node can die long after launch() returned, so without
                # this the row keeps a green RUNNING badge until the next
                # restart. A deliberate stop() is exempt — the route records
                # 'stopped' itself.
                if announced.is_set() and not stopping:
                    self.on_status(
                        deploy_id,
                        "failed",
                        failure[0]
                        if failure
                        else "the node exited on its own (see the server log)",
                    )
                try:
                    node.dispose()
                except Exception:  # noqa: BLE001 — teardown noise, already reported
                    pass
                loop.close()

        t = threading.Thread(
            target=_serve, name=f"studio-deploy-{deploy_id[:6]}", daemon=True
        )
        t.start()
        # Wait for the build, not for the run: run_async only returns when the
        # node is finished. `running` therefore means "the node was constructed
        # and handed to its loop", and anything that kills it later comes back
        # through the `finally` above.
        if not built.wait(timeout=self.BUILD_TIMEOUT):
            self.on_status(
                deploy_id,
                "failed",
                f"the node did not finish building within {self.BUILD_TIMEOUT:.0f}s",
            )
            return
        if failure:
            self.on_status(deploy_id, "failed", failure[0])
            return
        self.on_status(deploy_id, "running", None)
        announced.set()
        # After the row says running, so an inactive-switch warning lands on a
        # row that exists and is not overwritten by the status above.
        self._arm_kill_switch(deploy_id, artifact)

    def _strategies(self, deploy_id: str):
        with self._lock:
            entry = self._nodes.get(deploy_id)
        if entry is None:
            raise RunnerError(
                f"deployment {deploy_id[:6]} has no live node "
                "(the server was restarted, or it already stopped)"
            )
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
                    s.resume if hasattr(s, "resume") else s.start
                )
        self._rearm_kill_switch(deploy_id)

    def stop(self, deploy_id: str) -> None:
        """Terminal. The thread's `finally` disposes the node and deregisters."""
        with self._lock:
            entry = self._nodes.get(deploy_id)
            self._armed.pop(deploy_id, None)
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

    # -- kill switch -----------------------------------------------------
    def _arm_kill_switch(self, deploy_id: str, artifact: dict) -> None:
        raw = artifact.get("kill_switch_daily_pct")
        if raw is None:
            return  # the operator chose 'off' in the deploy modal
        try:
            limit = abs(float(raw))
            capital = float(artifact.get("capital") or 0.0)
        except (TypeError, ValueError):
            limit, capital = 0.0, 0.0
        if limit <= 0 or capital <= 0:
            # A limit is a percentage OF something; without capital there is no
            # denominator. Say it on the row — an operator who picked "−3% day"
            # must not be left believing a switch is watching when none is.
            self.on_status(
                deploy_id,
                "running",
                f"kill switch INACTIVE: needs a positive limit and capital "
                f"(limit={raw!r}, capital={artifact.get('capital')!r})",
            )
            return
        with self._lock:
            self._armed[deploy_id] = _KillSwitch(limit_pct=limit, capital=capital)
            self._start_monitor()

    def _rearm_kill_switch(self, deploy_id: str) -> None:
        """A manual resume re-arms the switch from HERE, not from this morning.

        Keeping the old baseline would re-fire on the very next poll and make
        the Resume button do nothing visible. The trade-off is deliberate and
        stated on the row when the switch fires: resuming a deployment the
        switch stopped allows it another ``limit_pct`` for the same day.
        """
        with self._lock:
            ks = self._armed.get(deploy_id)
            if ks is None:
                return
            ks.fired = False
            ks.day = ""  # the next reading becomes the new baseline

    def _start_monitor(self) -> None:
        """Start the polling thread if it is not already up. Caller holds `_lock`."""
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="studio-killswitch", daemon=True
        )
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(self.poll_s)
            with self._lock:
                if not self._armed:
                    self._monitor = None  # re-armed later ⇒ a fresh thread
                    return
            try:
                self.check_kill_switches()
            except Exception:  # noqa: BLE001 — one bad tick must not end the watch
                log.exception("kill switch tick failed")

    def check_kill_switches(self) -> list[str]:
        """One measurement pass; returns the deployments it just paused.

        Split from the thread that calls it because *when* to look and *what to
        conclude* are different problems: this half is deterministic and takes
        neither a clock nor a network.
        """
        with self._lock:
            armed = [(d, ks) for d, ks in self._armed.items() if not ks.fired]
        day = datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
        fired: list[str] = []
        for deploy_id, ks in armed:
            pnl = self._read_pnl(deploy_id)
            if pnl is None:
                continue  # unreadable — look again next tick, never assume flat
            with self._lock:
                if ks.day != day:
                    # First reading of a new UTC day: today is measured from
                    # here, not from the node's lifetime PnL. Without this a
                    # node started mid-drawdown would trip on yesterday's loss.
                    ks.day, ks.baseline = day, pnl
                    continue
                pct = (pnl - ks.baseline) / ks.capital * 100.0
                if pct > -ks.limit_pct:
                    continue
                ks.fired = True
            try:
                self.pause(deploy_id)
            except RunnerError as e:
                # Kilit `pause()` çağrılmadan ÖNCE bırakılıyor (o kendi kilidini
                # alacak), yani aradaki pencerede operatör Stop'a basmış ya da
                # düğüm kendi ölmüş olabilir. Böyle bir durumda satırı kimin
                # yazacağı zaten belli: `stop()` yolunda route 'stopped',
                # kendiliğinden ölümde serve thread'i 'failed' yazıyor. Buradan
                # da yazmak, KULLANICININ KENDİ durdurduğu bir deployment'ı hiç
                # olmamış bir arıza mesajıyla kırmızıya çevirirdi.
                with self._lock:
                    still_there = deploy_id in self._nodes
                if not still_there:
                    log.info(
                        "kill switch: %s disappeared before it could be paused "
                        "(deliberate stop or the node exited) — leaving the row "
                        "to whoever owns that transition",
                        deploy_id[:6],
                    )
                    continue
                self.on_status(deploy_id, "failed", f"kill switch could not pause: {e}")
                continue
            self.on_status(
                deploy_id,
                "paused",
                f"kill switch fired: {pct:+.2f}% on the day, limit −{ks.limit_pct:.2f}%. "
                "Strategies stopped; the node and its data feed are still up. "
                "Resume re-arms the switch from the current balance.",
            )
            fired.append(deploy_id)
        return fired

    def _read_pnl(self, deploy_id: str) -> float | None:
        """Account PnL (realized **and** unrealized) in the account currency.

        Unrealized is included on purpose. The deploy modal promises "daily
        loss", and an operator reads that as the account, not as the subset of
        it that happens to be closed — a realized-only switch would watch an
        open position bleed all day and never fire.
        """
        if self.pnl_reader is not None:
            return self.pnl_reader(deploy_id)
        with self._lock:
            entry = self._nodes.get(deploy_id)
        if entry is None:
            return None
        from nautilus_trader.model.identifiers import Venue

        venue = Venue(BYBIT_VENUE)
        node = entry.node
        pnls = node.portfolio.total_pnls(venue)
        if not pnls:
            # An empty dict means "no positions" OR "a lookup failed inside the
            # portfolio" — `Portfolio.total_pnls` returns {} for both. The cache
            # separates them; guessing 0.0 for the second would blind the switch.
            has_positions = node.cache.positions_open(venue=venue) or (
                node.cache.positions_closed(venue=venue)
            )
            return None if has_positions else 0.0
        account = node.portfolio.account(venue)
        base = getattr(account, "base_currency", None) if account is not None else None
        money = pnls.get(base) if base is not None else None
        if money is None and len(pnls) == 1:
            money = next(iter(pnls.values()))
        if money is None:
            # Several currencies and none of them the account's: summing them
            # would be adding apples to pears at an FX rate nobody supplied.
            log.warning(
                "kill switch: %s reports PnL in %d currencies, none the account's",
                deploy_id[:6],
                len(pnls),
            )
            return None
        return money.as_double()


def reconcile_orphans(
    rows: list[dict], active: set[str], *, include_pending: bool = False
) -> list[tuple[str, str]]:
    """Deployments the database thinks are live but no node is behind.

    The node registry is in-process, so a restart leaves `running`/`paused`
    rows with nothing running. Returning them as (deploy_id, reason) rather
    than showing a green RUNNING badge for a node that does not exist.

    ``include_pending`` bir tehlike anahtarı ve VARSAYILANI kapalı (2026-08-17).
    Bir `pending` satır iki farklı şeyin adı olabiliyor: (a) devralınmayı hâlâ
    bekleyen, birkaç saniye önce yaratılmış CANLI bir kayıt, (b) devralma hiç
    olmadan süreci ölmüş bir kalıntı. Aradaki farkı satırın kendisi söylemiyor —
    ÇAĞIRANIN bağlamı söylüyor. Yalnız açılış yolu "(a) benim için imkânsız"
    diyebilir, çünkü süreç yeni doğdu ve ona ait hiçbir `background_tasks`
    devralması uçuşta olamaz. Bu yüzden karar buraya bir zaman aşımı olarak
    değil, çağıranın beyanı olarak gömüldü: periyodik bir çağıran sonradan
    eklenirse güvenli tarafta başlar, `pending` satırları biçmez.
    """
    orphans = []
    statuses = (
        ("running", "paused", "pending") if include_pending else ("running", "paused")
    )
    for row in rows:
        if row["status"] not in statuses or row["deploy_id"] in active:
            continue
        # Sebep ayrı, çünkü olay ayrı: `running` bir düğüm KAYBETTİ, `pending`
        # hiç düğüm görmedi. Operatörün ekranda okuduğu cümle hangisinin
        # olduğunu söylemeli — "yeniden başlat" ile "yeniden dağıt" farklı iş.
        reason = (
            "runner pickup never happened — the process died before this "
            "deployment was handed to the runner"
            if row["status"] == "pending"
            else "runner process restarted — the node behind this deployment is gone"
        )
        orphans.append((row["deploy_id"], reason))
    return orphans
