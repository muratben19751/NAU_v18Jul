"""Killable process sandbox for backtests that execute custom user/LLM code.

Custom signal blocks run their ``evaluate()`` once *per bar* inside the backtest
loop, so a per-bar IPC hop is infeasible — the process boundary must wrap the
*whole* backtest instead. ``run_backtest_guarded``:

  * Fast path — if every block is builtin, runs ``run_composed_backtest``
    in-process with zero overhead (the common case).
  * Sandbox path — if any block is custom, spawns ONE killable child process
    that runs the entire backtest. Custom code still runs full-speed in-process
    *within that child*; isolation and killability come from the parent owning
    the child. A wall-clock timeout hard-kills runaway/infinite-loop code, and
    any failure comes back as ``IterationResult(error=...)`` — the same contract
    every existing call site already handles.

Windows-safe by construction: uses the ``spawn`` start method and passes only
primitives across the boundary (spec dataclass, pandas DataFrame, a string
``recipe``). The child rebuilds the pyo3-backed Nautilus instrument/bar_type
from the recipe, so no Rust object is ever pickled. The child imports the light
``sandbox`` module (not ``server``), so the FastAPI app/lifespan never re-runs.

Headless (pm2) runs spawn children via ``pythonw.exe`` — console-subsystem
children froze at console init before Python booted, hanging Process.start()
forever with the timeout watchdog never armed (see the module-level
``set_executable`` block and ``_child_stdio_guard``).

Wiki References
---------------
See: [[webapp_module_map]] (sandbox satırı + pm2 spawn donması vakası),
[[crash_only_design]] (parent her koşulda hayatta kalır; çocuk öldürülebilir).
"""

from __future__ import annotations

import atexit
import multiprocessing as mp
import os as _os
import queue as _queue
import sys as _sys
import threading as _threading
import time as _time
import traceback
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_TIMEOUT_S = 180.0
# Robustness runs many backtests (multi-symbol + IS/OOS + walk-forward + Monte
# Carlo), so it gets a much longer wall-clock budget than a single backtest.
ROBUSTNESS_TIMEOUT_S = 900.0

# Graceful-exit protection (sandbox.py:335): daemon=False children (robustness/
# manual-suite — own their own ProcessPool) hang multiprocessing's atexit join
# on a GRACEFUL server exit; since the parent is alive (stuck in join) the
# parent-liveness watchdog isn't triggered either → shutdown deadlocks.
# Track running non-daemon children and terminate them BEFORE mp's atexit
# (atexit LIFO — this register runs first because it happens AFTER the mp import).
_LIVE_NONDAEMON_CHILDREN: set = set()
_CHILDREN_GUARD = _threading.Lock()


def _terminate_live_children() -> None:
    with _CHILDREN_GUARD:
        procs = list(_LIVE_NONDAEMON_CHILDREN)
    for p in procs:
        try:
            if p.is_alive():
                p.terminate()
        except Exception:
            pass


atexit.register(_terminate_live_children)

# ── Headless (pm2) spawn: children via pythonw.exe ───────────────────────────
# 2026-08-01: under pm2, console-subsystem (python.exe) spawn children froze at
# console/loader init BEFORE Python ever booted (~2MB WS, 0 CPU, forever). The
# parent then blocks inside Process.start() → reduction.dump (writing the
# pickled payload into a pipe nobody reads), and because the timeout watchdog
# below is armed only AFTER start(), the run hung with no timeout and no log
# line. pythonw.exe (GUI subsystem — no console to attach) boots instantly in
# the same pm2 environment (probe: 50MB payload end-to-end in 0.06s). Scope:
# only when running under pm2 (PM2_HOME marker) — console sessions (pytest,
# driver) keep the normal interpreter. Kill-switch: NAUTILUS_SPAWN_PYW=0.
# Children of pythonw boot with NO stdio — see _child_stdio_guard.
if (
    _os.name == "nt"
    and _os.environ.get("PM2_HOME")
    and _os.environ.get("NAUTILUS_SPAWN_PYW", "1") != "0"
):
    _pyw = Path(_sys.exec_prefix) / "pythonw.exe"
    if _pyw.exists():
        mp.set_executable(str(_pyw))


def _child_stdio_guard() -> None:
    """Make child stdio safe in both spawn flavors.

    pythonw children boot with ``sys.stdout``/``sys.stderr`` = None AND invalid
    C-level fds 1/2 — a Python ``print()`` or a Rust/C write from the Nautilus
    engine would otherwise crash or error. Bind both layers to devnull. Console
    children instead get UTF-8 (Turkish/glyph progress text vs cp125x).
    """
    for name, fd in (("stdout", 1), ("stderr", 2)):
        stream = getattr(_sys, name)
        if stream is None:
            f = open(_os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives for the child's lifetime
            setattr(_sys, name, f)
            try:
                _os.dup2(f.fileno(), fd)
            except OSError:
                pass
            continue
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _derive_base(symbol: str) -> str:
    return symbol[:-4] if symbol.upper().endswith("USDT") else symbol[:3]


def _build_instrument_bar_type(recipe: dict, bars_df=None):
    """Rebuild the Nautilus instrument + bar_type from a string recipe.

    Recipe shapes (see ``run_backtest_guarded`` docstring):
      Bybit (default): {"symbol", "interval", "category"[, "base"]}
      External:        {"source": "external", "instrument_id", "granularity"}
      Index:           {"source": "index", "ticker", "granularity"}

    ``bars_df`` is the frame the caller is about to backtest. It is optional
    and used only on the Bybit path, to derive a price precision for symbols
    missing from ``backtest._BYBIT_SPECS`` — without it a sub-cent coin
    (PEPEUSDT, SHIBUSDT…) gets the 2-decimal default and every price becomes
    0.00 (DeepR 2026-08-11 [YÜKSEK]). Every caller has the frame in hand, so
    passing it costs nothing; omitting it keeps the old behaviour plus the
    hard `_bars_from_df` guard.
    """
    if recipe.get("source") == "external":
        from backtest import _make_external_bar_type
        from data import external_instrument_object

        instrument = external_instrument_object(recipe["instrument_id"])
        if instrument is None:
            raise RuntimeError(
                f"external instrument not found in catalog: {recipe['instrument_id']}"
            )
        bar_type = _make_external_bar_type(instrument.id, recipe["granularity"])
        return instrument, bar_type

    if recipe.get("source") == "index":
        from backtest import _make_index_bar_type, _make_index_instrument

        instrument = _make_index_instrument(recipe["ticker"])
        bar_type = _make_index_bar_type(instrument.id, recipe["granularity"])
        return instrument, bar_type

    from backtest import _make_bybit_bar_type, _make_bybit_instrument, price_scale_hint

    symbol = recipe["symbol"]
    instrument = _make_bybit_instrument(
        symbol=symbol,
        base=recipe.get("base") or _derive_base(symbol),
        category=recipe.get("category", "linear"),
        price_hint=price_scale_hint(bars_df),
    )
    bar_type = _make_bybit_bar_type(instrument.id, recipe["interval"])
    return instrument, bar_type


def has_custom_block(spec) -> bool:
    """True if any block is not a builtin (or is unknown → treat as custom)."""
    from composer import BLOCK_REGISTRY

    for b in spec.blocks:
        reg = BLOCK_REGISTRY.get(b.type)
        if reg is None or not reg.get("builtin", False):
            return True
    return False


def _error_result(iteration_id: int, rationale: str, msg: str):
    from state import IterationResult

    return IterationResult(
        id=iteration_id,
        strategy="",
        params={},
        metrics={},
        equity_curve=[],
        rationale=rationale,
        error=msg,
        timestamp=datetime.now(UTC),
    )


def _child_entry(q, payload):
    """Top-level target for the spawned process. Imports only light modules
    (never ``server``). Puts ('result'|'error'|'progress', payload) on the queue.
    """

    # M8: if the parent is hard-killed (pm2 delete+start / TerminateProcess)
    # the daemon flag does NOT kill the child (atexit does not run) — without a
    # watchdog runaway custom code would keep burning the core as an orphan process.
    _start_parent_watchdog()

    # Progress strings contain Turkish/glyph chars; a fresh Windows child that
    # never imported server needs UTF-8 stdout or print() crashes on cp125x.
    _child_stdio_guard()

    try:
        spec, bars_df, recipe, iteration_id, rationale = payload
        from backtest import run_composed_backtest

        instrument, bar_type = _build_instrument_bar_type(recipe, bars_df)

        def _progress(msg):
            try:
                q.put(("progress", msg))
            except Exception:
                pass

        result = run_composed_backtest(
            spec,
            bars_df,
            iteration_id=iteration_id,
            rationale=rationale,
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
            progress_fn=_progress,
            initial_capital=recipe.get("initial_capital"),
            commission_bps_override=recipe.get("commission_bps_override"),
        )
        q.put(("result", result))
    except Exception as e:  # pragma: no cover - defensive
        q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def _run_in_child(target, payload, progress_fn, timeout_s: float, *, daemon=True):
    """Spawn ``target(q, payload)`` in a killable process and wait up to
    ``timeout_s`` seconds. Returns ``(result, error_str)`` — exactly one is set.

    Relays ('progress', msg) queue items to ``progress_fn`` live. On deadline the
    child is terminate()'d then kill()'d; the parent always survives. ``q.get``
    runs BEFORE ``join`` so a large result can't deadlock the pipe.

    ``daemon=False`` is required for children that spawn their OWN worker pool
    (daemonic processes are not allowed to have children — the pool's first
    submit would raise AssertionError). Such children must run a parent-liveness
    watchdog so they never outlive the server (see ``_start_parent_watchdog``).
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=target, args=(q, payload), daemon=daemon)
    proc.start()
    # watch the non-daemon child — on graceful-exit it is terminated via atexit
    # (otherwise mp's atexit join would deadlock shutdown). Released in finally.
    _tracked = not daemon
    if _tracked:
        with _CHILDREN_GUARD:
            _LIVE_NONDAEMON_CHILDREN.add(proc)

    deadline = _time.monotonic() + timeout_s
    result = None
    err = None
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break
        try:
            tag, data = q.get(timeout=min(remaining, 1.0))
        except _queue.Empty:
            if not proc.is_alive():
                # Race: the child may have flushed the result to the pipe and
                # exited between the get() timeout and this is_alive() check →
                # do one last short drain so a delivered result isn't trashed as 'crashed'.
                try:
                    while True:
                        tag, data = q.get(timeout=0.5)
                        if tag == "progress":
                            if progress_fn:
                                try:
                                    progress_fn(data)
                                except Exception:
                                    pass
                            continue
                        if tag == "result":
                            result = data
                        elif tag == "error":
                            err = data
                        break
                except _queue.Empty:
                    pass
                break  # died without delivering a result
            continue
        if tag == "progress":
            if progress_fn:
                try:
                    progress_fn(data)
                except Exception:
                    pass
        elif tag == "result":
            result = data
            break
        elif tag == "error":
            err = data
            break

    if result is None and err is None:
        if proc.is_alive():
            proc.terminate()
            proc.join(2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(1.0)
            err = f"sandbox: timed out after {timeout_s:.0f}s, terminated"
        else:
            err = f"sandbox: worker crashed (exitcode={proc.exitcode})"

    if proc.is_alive():
        proc.terminate()
        proc.join(1.0)
    if _tracked:
        with _CHILDREN_GUARD:
            _LIVE_NONDAEMON_CHILDREN.discard(proc)
    try:
        q.close()
        q.cancel_join_thread()
    except Exception:
        pass
    return result, err


def _start_parent_watchdog() -> None:
    """Daemon thread: self-exit when the parent (web server) dies.

    Non-daemon mp children (needed so they can own a worker pool) would
    otherwise survive a server exit; this closes that orphan window.
    """
    import os
    import threading
    import time as _t

    parent = mp.parent_process()

    def _watch() -> None:
        while True:
            if parent is None or not parent.is_alive():
                os._exit(1)
            _t.sleep(1.0)

    threading.Thread(target=_watch, daemon=True).start()


def _robustness_child(q, payload):
    """Child target: rebuild the Nautilus instrument, run the agent's full
    robustness suite, relay its progress steps, and return the result dict.

    Suite artık `auto.robustness` (web'den bağımsız alan katmanı) — bu çocuk
    süreç eskiden `import web.routes.agent_backtest as ab` yapıp `ab._IPC_Q = q`
    ile başka bir modülün private global'ini set ediyordu, yani HTTP servis
    etmeyen bir worker sırf bu çağrı için tüm FastAPI router ağacını
    yüklüyordu (DeepR 2026-08-11 [YÜKSEK], `sandbox -> web.routes.agent_backtest
    -> sandbox` döngüsü). İlerleme aktarımı artık açık bir `progress_fn`:
    `_relay` eski `_add_step`'in child dalıyla birebir aynı — aynı ``progress``
    etiketi, aynı yutulan istisna.

    Spawned NON-daemonic (so it may own a parallel_exec.BacktestPool);
    the parent watchdog guarantees it never outlives the server.
    """

    _start_parent_watchdog()

    _child_stdio_guard()

    def _relay(msg: str) -> None:
        # Tag matches _run_in_child's ("progress", ...) branch. A dead/closed
        # queue must never abort the suite — audit relay is best effort.
        try:
            q.put(("progress", msg))
        except Exception:
            pass

    try:
        spec, bars_df, recipe, trades, symbol, interval = payload
        instrument, bar_type = _build_instrument_bar_type(recipe, bars_df)
        from auto.robustness import run_full_robustness

        rob = run_full_robustness(
            spec,
            bars_df,
            instrument,
            bar_type,
            instrument.id.venue,
            trades,
            symbol=symbol,
            interval=interval,
            category=recipe.get("category", "linear"),
            source=recipe.get("source", "bybit"),
            progress_fn=_relay,
        )
        q.put(("result", rob))
    except Exception as e:  # pragma: no cover - defensive
        q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def cleanup_stale_snapshots(max_age_h: float = 2.0) -> int:
    """L23: sweeps the ``nautilus_rob_*`` temp snapshot directories orphaned by
    robustness children killed on timeout.

    Cleanup normally happens in the child's finally — but TerminateProcess (the
    900s timeout kill) gives finally no chance and Windows does not auto-sweep
    %TEMP%. The age filter (mtime) removes the risk of deleting the ACTIVE
    snapshot of another suite running concurrently. Returns the number of
    directories deleted.
    """
    import shutil
    import tempfile
    import time as _t

    removed = 0
    cutoff = _t.time() - max_age_h * 3600.0
    try:
        for d in Path(tempfile.gettempdir()).glob("nautilus_rob_*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def run_robustness_guarded(
    spec,
    bars_df,
    recipe: dict,
    trades: list,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1",
    progress_fn=None,
    timeout_s: float = ROBUSTNESS_TIMEOUT_S,
) -> dict:
    """Run the agent's full robustness suite in a killable child process.

    The suite fires many Nautilus backtests (each holds the GIL); running it in
    the web-server process would freeze the async event loop. Isolated here it
    can't, and the timeout kills a runaway suite. Returns the robustness dict,
    or ``{"error": ...}`` on timeout/crash. ``progress_fn`` gets live steps.
    """
    payload = (spec, bars_df, recipe, trades, symbol, interval)
    # daemon=False: the child owns a ProcessPoolExecutor (daemonic processes
    # cannot have children). Orphan safety comes from its parent watchdog.
    cleanup_stale_snapshots()  # L23: leftovers from previous timeout-kills
    result, err = _run_in_child(
        _robustness_child, payload, progress_fn, timeout_s, daemon=False
    )
    if err is not None:
        return {"error": err}
    return result


def _manual_suite_child(q, payload):
    """Child target: the suite of the manual /robustness page (WFO + IS/OOS +
    full backtest + Monte Carlo) — all in a single killable child.

    This suite previously ran RAW in a daemon thread inside the server process;
    since Nautilus backtests hold the GIL the event loop froze (an exact copy of
    the bug fixed in the agent). The returned dict carries the pieces the route
    expects: {wfo_windows, wfo_summary, split, mc, full_error}.
    """

    _start_parent_watchdog()

    _child_stdio_guard()

    try:
        spec, bars_df, recipe, params = payload
        instrument, bar_type = _build_instrument_bar_type(recipe, bars_df)

        def prog(msg):
            try:
                q.put(("progress", msg))
            except Exception:
                pass

        from backtest import STARTING_CASH, run_composed_backtest
        from backtest_robustness import (
            run_insample_oos_split,
            run_monte_carlo,
            run_walk_forward,
            wfo_aggregate,
        )

        prog(
            f"Walk-Forward starting · train={params['train_months']}month "
            f"test={params['test_months']}month"
        )
        wfo_windows = run_walk_forward(
            spec,
            bars_df,
            instrument,
            bar_type,
            instrument.id.venue,
            train_months=params["train_months"],
            test_months=params["test_months"],
            step_months=params["test_months"],
            n_optimize=params["n_optimize"],
            objective=params["objective"],
            progress_fn=prog,
        )
        wfo_summary = wfo_aggregate(wfo_windows)

        prog(
            f"In/Out-of-Sample split · %{int(params['split_pct'] * 100)} / "
            f"%{int((1 - params['split_pct']) * 100)}"
        )
        split_result = run_insample_oos_split(
            spec,
            bars_df,
            instrument,
            bar_type,
            instrument.id.venue,
            split_pct=params["split_pct"],
            progress_fn=prog,
        )

        prog("Running full backtest for Monte Carlo…")
        full_result = run_composed_backtest(
            spec,
            bars_df,
            iteration_id=999,
            rationale="Robustness full run",
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        # DeepR 2026-08-11 [YÜKSEK]: these two outcomes are NOT the same thing
        # and must never render the same way. "0 trades" is a fact about the
        # strategy; a crashed full backtest is a fact about the infrastructure,
        # and reporting it as "no trade data" told the user their strategy was
        # useless while the truth was that nothing ever ran. `failed` is the
        # flag the UI branches on; `error` stays human-readable.
        if full_result.error:
            mc_result = {
                "error": f"Full backtest failed: {full_result.error}",
                "failed": True,
            }
            prog(f"⚠ Full backtest failed — Monte Carlo skipped: {full_result.error}")
        elif not full_result.trades:
            mc_result = {
                "error": "The backtest produced no trades — Monte Carlo needs "
                "at least one trade.",
                "failed": False,
            }
        else:
            prog(
                f"Monte Carlo · {params['n_sims']} simulations · "
                f"{len(full_result.trades)} trade"
            )
            mc_result = run_monte_carlo(
                full_result.trades,
                n_sims=params["n_sims"],
                starting_cash=STARTING_CASH,
                progress_fn=prog,
            )

        q.put(
            (
                "result",
                {
                    "wfo_windows": wfo_windows,
                    "wfo_summary": wfo_summary,
                    "split": split_result,
                    "mc": mc_result,
                    "full_error": full_result.error,
                },
            )
        )
    except Exception as e:  # pragma: no cover - defensive
        q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def run_manual_suite_guarded(
    spec,
    bars_df,
    recipe: dict,
    params: dict,
    *,
    progress_fn=None,
    timeout_s: float = ROBUSTNESS_TIMEOUT_S,
) -> dict:
    """Run the manual robustness suite in a killable child (the server won't freeze).

    ``params`` = {train_months, test_months, n_optimize, objective, split_pct,
    n_sims}. Returns ``{"error": ...}`` on timeout/crash.
    """
    payload = (spec, bars_df, recipe, params)
    result, err = _run_in_child(
        _manual_suite_child, payload, progress_fn, timeout_s, daemon=False
    )
    if err is not None:
        return {"error": err}
    return result


def _legacy_backtest_child(q, payload):
    """Child target: legacy STRATEGY_REGISTRY backtest (loop 'agent' mode)."""

    _start_parent_watchdog()  # M8: so it isn't orphaned on hard-kill
    _child_stdio_guard()
    try:
        strategy_name, params, bars_df, iteration_id, rationale = payload
        from backtest import run_backtest

        result = run_backtest(
            strategy_name=strategy_name,
            params=params,
            bars_df=bars_df,
            iteration_id=iteration_id,
            rationale=rationale,
        )
        q.put(("result", result))
    except Exception as e:  # pragma: no cover - defensive
        q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def run_legacy_backtest_guarded(
    strategy_name: str,
    params: dict,
    bars_df,
    *,
    iteration_id: int = 0,
    rationale: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
):
    """Run the legacy (STRATEGY_REGISTRY) backtest in a killable child."""
    payload = (strategy_name, params, bars_df, iteration_id, rationale)
    result, err = _run_in_child(_legacy_backtest_child, payload, None, timeout_s)
    if err is not None:
        return _error_result(iteration_id, rationale, err)
    return result


def _hang_target(q, payload):  # pragma: no cover - runs only in child, for tests
    """Test-only child target that never returns — exercises the kill path."""
    while True:
        _time.sleep(0.05)


def run_backtest_guarded(
    spec,
    bars_df,
    recipe: dict,
    *,
    iteration_id: int = 0,
    rationale: str = "",
    progress_fn=None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    force_subprocess: bool = False,
):
    """Run a composed backtest, sandboxing it iff it contains custom code.

    ``recipe`` — the strings the caller already holds; the child rebuilds the
    Nautilus objects from them. Two shapes:
      Bybit:    {"symbol", "interval", "category"[, "base"]}
      External: {"source": "external", "instrument_id": "QQQ.NASDAQ",
                 "granularity": "1-DAY"}  (read-only external Nautilus catalog)
    Returns an ``IterationResult`` (errored on timeout or
    child crash). ``progress_fn`` receives live progress from either path.

    ``force_subprocess=True`` runs even builtin-only specs in the killable child.
    The autonomous agent uses this: a Nautilus backtest holds the GIL for its
    whole run (per-bar Python callbacks), which would freeze the async web
    server's event loop if it ran in-process on the request/worker thread. The
    child process isolates the GIL and the wall-clock timeout kills a hung run.
    """
    from backtest import run_composed_backtest

    # ── Fast path: builtin-only → in-process, no isolation overhead ──────────
    if not force_subprocess and not has_custom_block(spec):
        instrument, bar_type = _build_instrument_bar_type(recipe, bars_df)
        return run_composed_backtest(
            spec,
            bars_df,
            iteration_id=iteration_id,
            rationale=rationale,
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
            progress_fn=progress_fn,
            # Same two recipe keys the child path reads. Dropping them here made
            # the two "equivalent" paths silently disagree: the fast path ran on
            # the default capital and commission while the UI displayed the
            # values the caller asked for.
            initial_capital=recipe.get("initial_capital"),
            commission_bps_override=recipe.get("commission_bps_override"),
        )

    # ── Sandbox path: custom code → one killable child for the whole run ─────
    payload = (spec, bars_df, recipe, iteration_id, rationale)
    result, err = _run_in_child(_child_entry, payload, progress_fn, timeout_s)
    if err is not None:
        return _error_result(iteration_id, rationale, err)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Blok ÖNİZLEME / SMOKE — LLM+kullanıcı kodunun ikinci yürütme yüzeyi
#
# DeepR 2026-08-11 [ORTA]: bu modülün yukarıdaki her şeyi BACKTEST'i sarıyordu.
# Oysa aynı kod bir de backtest'e hiç girmeden çalışıyordu — ve orası web
# sunucusunun KENDİ süreciydi:
#
#   * `agent._test_execute_generated` (smoke): `t.join(timeout=2.0)` bir
#     daemon thread'i ÖLDÜRMÜYOR. Kullanıcı "timed out" hatasını alıyor, kod
#     arka planda bir çekirdeği yakmaya devam ediyor, tekrarlanan gönderimler
#     üst üste birikiyordu. Bir thread'i preempt etmenin yolu YOK; tek çare
#     süreç sınırı.
#   * `web/routes/strategy._preview_signals`: hiç duvar saati yoktu ve
#     `evaluate()` 150 kez ART ARDA çağrılıyordu — döngü bütçesi her çağrının
#     başında sıfırlandığı için tek bir HTTP isteği teorik olarak 750 milyon
#     adım koşabiliyordu, üstelik FastAPI'nin olay döngüsünde.
#
# NEDEN ALT SÜREÇ (ve neden "sert bir duvar saati" tek başına yetmezdi):
# in-process bir duvar saati, GIL'i bırakmayan tek bir bytecode'u (`9**9**9`)
# durduramaz — codegate'in kendi yorumu bunu zaten belgeliyor. Ölçülebilir tek
# sınır öldürülebilir bir süreçtir ve o desen bu dosyada ZATEN var, test
# edilmiş hâlde (`_run_in_child` + `TestKillMachinery`). İkinci bir mekanizma
# icat etmek yerine mevcut olanı yeniden kullanıyoruz.
#
# MALİYET: çocuklar bilerek HAFİF. Önizleme çocuğu yalnız `codegate` (~9 ms) +
# `indicators` (~1 ms) import eder — pandas'ı DEĞİL: parquet okuma ve grafik
# şekillendirme ebeveynde kalır, çocuğa yalnız fiyat listeleri geçer. Spawn
# dahil ~0.3 sn, ve önizleme zaten 15-20 sn'lik bir LLM turunun ardından gelir.
#
# KAPSAM DIŞI (bilerek): `composer._load_module_from_path`. O yol bir MODÜL
# NESNESİ üretmek zorunda ve nesne bu süreçte yaşamak zorunda — alt sürece
# taşınamaz, çünkü taşınan şey geri getirilemez. Oradaki kod ayrıca kayıt
# anında bu çocuklardan geçmiş, diske yazılmış ve yüklemede yeniden AST
# doğrulamasından geçen koddur; kalan risk kabul edilip belgelenmiştir
# (bkz. composer._load_module_from_path docstring'i).
# ═══════════════════════════════════════════════════════════════════════════

# Önizleme/smoke bir İNSANI bekletiyor: backtest'in 180 sn'si burada anlamsız.
# 10 sn, gerçek bir bloğun 150 barlık sürüşünden kat kat uzun; aşan her şey
# fiilen bir kaçak döngüdür.
USER_CODE_TIMEOUT_S = float(_os.environ.get("NAU_USER_CODE_TIMEOUT_S", "10"))
# Bellek tavanı: duvar saati bir `while True`'yu yakalar ama tek satırlık bir
# `x = [0] * 10**9` deadline'dan ÖNCE makineyi şişirir. Alt süreçte bu sınırı
# sert koymak güvenli — patlayan çocuk, sunucu değil.
#
# Tavan SÜREÇ TOPLAMINA uygulanır (POSIX'te RLIMIT_AS, Windows'ta Job Object
# ProcessMemoryLimit), kullanıcı koduna ayrılan EK bütçeye değil. Çocuk smoke
# yolunda `agent` zincirini (pandas + nautilus_trader) import ettiği için taban
# tüketim tek başına yüzlerce MB: ilk konan 512 MB'lık tavan altında import
# tamamlanamıyor, süreç `RuntimeError: can't start new thread` ile ölüyor ve
# hata kullanıcının bloğuna atfediliyordu ("bellek tavanı aşılmış olabilir")
# — oysa blok hiç çalışmamıştı. Ölçüldü (2026-08-11): 512 MB çöküyor, 1024 MB
# geçiyor; varsayılan emniyet payıyla 2048. Koruma değerini yitirmiyor —
# kaçak bir ayırma (`[0] * 10**9` ≈ 8 GB) bu tavanı yine anında aşar.
USER_CODE_MEMORY_MB = int(_os.environ.get("NAU_USER_CODE_MEMORY_MB", "2048"))


def _install_memory_ceiling(mem_mb: int) -> None:
    """Bu SÜRECE sert bir bellek tavanı koy. Sessizce başarısız olabilir.

    POSIX'te ``RLIMIT_AS`` doğrudan işi görür: ayırma anında ``MemoryError``
    olur, yani hata kullanıcıya normal bir istisna olarak döner.

    Windows'ta rlimit yok; karşılığı bir Job Object'in ``ProcessMemoryLimit``
    alanı. Win8+ bir süreç birden çok job'a ait olabildiği için pm2 altında
    zaten bir job'da olmak sorun değil. Kurulum başarısız olursa (eski
    Windows, kısıtlı token) sessizce vazgeçiyoruz — duvar saati hâlâ ayakta ve
    önizlemenin bir ctypes ayrıntısı yüzünden çalışmaması kabul edilemez.
    """
    if not mem_mb or mem_mb <= 0:
        return
    limit = int(mem_mb) * 1024 * 1024
    if _os.name != "nt":
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job_object_limit_process_memory = 0x00000100
        job_object_extended_limit_information = 9

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # restype'ları AÇIKÇA vermek ZORUNLU: ctypes'ın varsayılanı `c_int`,
        # yani 64-bit bir HANDLE 32 bite kırpılır. Kırpılmış handle ile
        # SetInformationJobObject sessizce başarısız olur ve sınır hiç
        # kurulmaz — ölçüldü: 256 MB tavanın ardından 800 MB rahatça ayrıldı.
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = job_object_limit_process_memory
        info.ProcessMemoryLimit = limit
        if not k32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            return
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
        # `job` handle'ı bilerek AÇIK bırakılıyor: kapatılırsa job yok olur ve
        # sınır düşer. Süreç bittiğinde işletim sistemi ikisini de toplar.
    except Exception:
        pass


def _block_smoke_child(q, payload):  # pragma: no cover - runs in the child
    """Çocuk hedefi: üretilen bloğun smoke testini KOŞ, sonucu bildir."""
    _child_stdio_guard()
    # SIRA ÖNEMLİ: ağır import ÖNCE, tavan SONRA. Tavanın amacı KULLANICI
    # KODUNU sınırlamak, kendi altyapımızı değil — `agent` zinciri pandas ve
    # nautilus_trader'ı çekiyor ve 512 MB'lık tavan altında import bile
    # tamamlanamıyordu: çocuk `exitcode=1` ile ölüyor, hata da kullanıcının
    # bloğuna atfediliyordu ("bellek tavanı aşılmış olabilir"), oysa blok hiç
    # çalışmamıştı. Tavanı import'tan sonra kurmak ikisini ayırır.
    try:
        from agent import _test_execute_generated_inproc
    except Exception as e:  # altyapı hatası — kullanıcı koduna atfetme
        q.put(("error", f"sandbox import failed: {type(e).__name__}: {e}"))
        return
    _install_memory_ceiling(payload.get("mem_mb", 0))
    try:
        _test_execute_generated_inproc(
            payload["src"],
            meta=payload.get("meta"),
            require_max_lookback=payload.get("require_max_lookback", False),
            role_hint=payload.get("role_hint", "entry"),
        )
    except Exception as e:
        q.put(("error", f"{type(e).__name__}: {e}"))
        return
    q.put(("result", True))


def _block_preview_child(q, payload):  # pragma: no cover - runs in the child
    """Çocuk hedefi: ``evaluate()``'i N bar boyunca sür, sinyalleri döndür.

    Ebeveyn parquet'i okuyup fiyat listelerini burada veriyor; çocuk pandas'a
    hiç dokunmuyor. Dönen sözlük çağıranın (``_preview_signals``) grafik
    sözleşmesindeki üç alanı taşır: ``signals``, ``eval_error``,
    ``eval_error_bars`` (+ ``evaluate`` hiç tanımlanmadıysa ``no_evaluate``).
    """
    _child_stdio_guard()
    _install_memory_ceiling(payload.get("mem_mb", 0))
    try:
        import math as _math
        import statistics as _stats

        import indicators as _ind_mod
        from codegate import (
            compile_with_loop_budget,
            safe_builtins,
            safe_module_proxy,
            validate_generated_code,
        )

        code = payload["code"]
        closes = payload["closes"]
        highs = payload["highs"]
        lows = payload["lows"]
        volumes = payload["volumes"]

        validate_generated_code(code)  # dunder/import/loop gates
        allowed: dict = {
            # Same builtins the smoke and the on-disk loader use. Rebuilding
            # this dict from _ALLOWED_BUILTINS alone once left out RuntimeError,
            # which the injected loop-budget guard raises: a `while True` block
            # died with `NameError: RuntimeError is not defined` and the
            # preview's blanket except turned it into an empty chart that reads
            # as "works" instead of "loop budget exceeded".
            "__builtins__": safe_builtins(),
            # Read-only proxies: even inside the child one block must not be
            # able to rewrite `ind.calc_rsi` for whatever runs after it.
            "math": safe_module_proxy(_math, "math"),
            "statistics": safe_module_proxy(_stats, "statistics"),
            "ind": safe_module_proxy(_ind_mod, "ind"),
        }
        ns: dict = {}
        exec(compile_with_loop_budget(code, "<preview>"), allowed, ns)
        for k, v in ns.items():
            if callable(v) and not k.startswith("_"):
                allowed[k] = v
        # M1084: budget preamble names land in ns; functions look them up in globals.
        for _bk in ("__budget", "__budget_tick"):
            if _bk in ns:
                allowed[_bk] = ns[_bk]
        ev = ns.get("evaluate")
        if ev is None:
            q.put(("result", {"no_evaluate": True}))
            return

        class _Block:
            def __init__(self, params, role):
                self.params = params
                self.role = role
                self.type = "custom"

        class _Port:
            def is_net_long(self, *a):
                return False

            def is_net_short(self, *a):
                return False

            def is_flat(self, *a):
                return True

        meta = payload.get("meta") or {}
        default_params = {
            k: v.get("default") for k, v in (meta.get("params") or {}).items()
        }
        block = _Block(default_params, payload.get("role_hint", "entry"))
        port, state = _Port(), {}
        start_i = payload["start_i"]
        signals = []
        eval_error = None
        eval_error_bars = 0
        for i in range(start_i, len(closes)):
            # M527: provide highs/lows/volumes to indicators like the runtime does.
            ind_dict = {
                "highs": highs[: i + 1],
                "lows": lows[: i + 1],
                "volumes": volumes[: i + 1],
            }
            try:
                sig = ev(state, block, closes[: i + 1], ind_dict, port)
            except Exception as exc:
                # Çökme "sinyal yok" DEĞİLDİR: ilk istisnayı sakla, kaç barda
                # patladığını say. Grafik yine çizilir (fiyat serisi geçerli),
                # ama arayüz bunun bir hata olduğunu söyler.
                sig = None
                eval_error_bars += 1
                if eval_error is None:
                    eval_error = f"{type(exc).__name__}: {exc}"
            signals.append({"i": i - start_i, "sig": sig})
        q.put(
            (
                "result",
                {
                    "signals": signals,
                    "eval_error": eval_error,
                    "eval_error_bars": eval_error_bars,
                },
            )
        )
    except Exception as e:
        q.put(("error", f"{type(e).__name__}: {e}"))


def _user_code_error(err: str, timeout_s: float, mem_mb: int) -> str:
    """Çocuktan gelen ham hatayı kullanıcının anlayacağı bir cümleye çevir.

    "aşımda net hata ver" gereği: `sandbox: timed out after 10s, terminated`
    bir operatöre neyi düzelteceğini söylemez; sınırı ve olası sebebi söylemek
    gerekir. Bellek tavanına takılan çocuk normal bir çıkışla değil öldürülerek
    bittiği için ebeveyne "crashed" görünür — o ihtimali de uydurmadan, adıyla
    anıyoruz.
    """
    if "timed out" in err:
        return (
            f"kod {timeout_s:g}s duvar saati sınırını aştı ve durduruldu "
            "(sonsuz döngü ya da aşırı ağır hesap?)"
        )
    if "crashed" in err:
        return (
            f"kod alt süreci çökertti — {mem_mb} MB bellek tavanı aşılmış "
            f"olabilir ({err})"
        )
    return err


def run_block_smoke_guarded(
    src: str,
    meta: dict | None = None,
    *,
    require_max_lookback: bool = False,
    role_hint: str = "entry",
    timeout_s: float = USER_CODE_TIMEOUT_S,
    mem_mb: int = USER_CODE_MEMORY_MB,
) -> str | None:
    """Smoke'u öldürülebilir bir çocukta koş. Hata metni ya da ``None``."""
    payload = {
        "src": src,
        "meta": meta,
        "require_max_lookback": require_max_lookback,
        "role_hint": role_hint,
        "mem_mb": mem_mb,
    }
    result, err = _run_in_child(_block_smoke_child, payload, None, timeout_s)
    if err is not None:
        return _user_code_error(err, timeout_s, mem_mb)
    return None if result else "smoke: child returned no result"


def run_block_preview_guarded(
    code: str,
    meta: dict | None,
    role_hint: str,
    *,
    closes: list,
    highs: list,
    lows: list,
    volumes: list,
    start_i: int,
    timeout_s: float = USER_CODE_TIMEOUT_S,
    mem_mb: int = USER_CODE_MEMORY_MB,
) -> tuple[dict | None, str | None]:
    """Önizleme sürüşünü öldürülebilir bir çocukta koş → ``(sonuç, hata)``."""
    payload = {
        "code": code,
        "meta": meta,
        "role_hint": role_hint,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volumes": volumes,
        "start_i": start_i,
        "mem_mb": mem_mb,
    }
    result, err = _run_in_child(_block_preview_child, payload, None, timeout_s)
    if err is not None:
        return None, _user_code_error(err, timeout_s, mem_mb)
    return result, None
