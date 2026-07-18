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
"""

from __future__ import annotations

import atexit
import multiprocessing as mp
import queue as _queue
import threading as _threading
import time as _time
import traceback
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_TIMEOUT_S = 180.0
# Robustness runs many backtests (multi-symbol + IS/OOS + walk-forward + Monte
# Carlo), so it gets a much longer wall-clock budget than a single backtest.
ROBUSTNESS_TIMEOUT_S = 900.0

# Graceful-exit koruması (sandbox.py:335): daemon=False child'lar (robustness/
# manual-suite — kendi ProcessPool'unu sahiplenir) sunucu ZARIF çıkışında
# multiprocessing'in atexit join'ini ASAR; parent canlı (join'de takılı)
# olduğundan parent-liveness watchdog'u da tetiklenmez → shutdown kilitlenir.
# Çalışan non-daemon child'ları izle ve mp'nin atexit'inden ÖNCE (atexit LIFO —
# bu register mp import'undan SONRA yapıldığından önce koşar) terminate et.
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


def _derive_base(symbol: str) -> str:
    return symbol[:-4] if symbol.upper().endswith("USDT") else symbol[:3]


def _build_instrument_bar_type(recipe: dict):
    """Rebuild the Nautilus instrument + bar_type from a string recipe.

    Recipe shapes (see ``run_backtest_guarded`` docstring):
      Bybit (default): {"symbol", "interval", "category"[, "base"]}
      External:        {"source": "external", "instrument_id", "granularity"}
      Index:           {"source": "index", "ticker", "granularity"}
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

    from backtest import _make_bybit_bar_type, _make_bybit_instrument

    symbol = recipe["symbol"]
    instrument = _make_bybit_instrument(
        symbol=symbol,
        base=recipe.get("base") or _derive_base(symbol),
        category=recipe.get("category", "linear"),
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
    import sys

    # M8: parent hard-kill edilirse (pm2 delete+start / TerminateProcess)
    # daemon bayrağı çocuğu ÖLDÜRMEZ (atexit çalışmaz) — watchdog olmadan
    # kaçak custom kod öksüz süreç olarak çekirdek yakmaya devam ediyordu.
    _start_parent_watchdog()

    # Progress strings contain Turkish/glyph chars; a fresh Windows child that
    # never imported server needs UTF-8 stdout or print() crashes on cp125x.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    try:
        spec, bars_df, recipe, iteration_id, rationale = payload
        from backtest import run_composed_backtest

        instrument, bar_type = _build_instrument_bar_type(recipe)

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
    # non-daemon child'ı izle — graceful-exit'te atexit ile terminate edilir
    # (aksi halde mp'nin atexit join'i shutdown'ı kilitlerdi). finally'de bırakılır.
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
                # Yarış: çocuk sonucu pipe'a flush edip get() timeout'u ile bu
                # is_alive() kontrolü arasında çıkmış olabilir → teslim edilmiş
                # sonucu 'crashed' diye çöpe atmadan son bir kez kısa drenaj yap.
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

    Sets ``agent_backtest._IPC_Q`` so the module's ``_add_step`` calls (used
    throughout ``_run_full_robustness``) relay to the parent instead of touching
    the child's own empty progress dict.

    Spawned NON-daemonic (so it may own a parallel_exec.BacktestPool);
    the parent watchdog guarantees it never outlives the server.
    """
    import sys

    _start_parent_watchdog()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    try:
        spec, bars_df, recipe, trades, symbol, interval = payload
        instrument, bar_type = _build_instrument_bar_type(recipe)
        import web.routes.agent_backtest as ab

        ab._IPC_Q = q  # route _add_step → parent (as ('progress', msg))
        rob = ab._run_full_robustness(
            "child",
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
        )
        q.put(("result", rob))
    except Exception as e:  # pragma: no cover - defensive
        q.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def cleanup_stale_snapshots(max_age_h: float = 2.0) -> int:
    """L23: timeout'la öldürülen robustness çocuklarının öksüz bıraktığı
    ``nautilus_rob_*`` temp snapshot dizinlerini süpürür.

    Temizlik normalde çocuğun finally'sinde — ama TerminateProcess (900s
    timeout kill'i) finally'ye şans tanımaz ve Windows %TEMP%'i otomatik
    süpürmez. Yaş filtresi (mtime), eşzamanlı koşan başka bir suite'in AKTİF
    snapshot'ını silme riskini kaldırır. Silinen dizin sayısını döndürür.
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
    cleanup_stale_snapshots()  # L23: önceki timeout-kill'lerin artıkları
    result, err = _run_in_child(
        _robustness_child, payload, progress_fn, timeout_s, daemon=False
    )
    if err is not None:
        return {"error": err}
    return result


def _manual_suite_child(q, payload):
    """Child target: manuel /robustness sayfasının suite'i (WFO + IS/OOS +
    tam backtest + Monte Carlo) — tamamı tek killable child'da.

    Bu suite daha önce sunucu sürecindeki bir daemon thread'de HAM koşuyordu;
    Nautilus backtest'leri GIL'i tuttuğu için event loop donuyordu (agent'ta
    düzeltilen bug'ın birebir kopyası). Dönen dict route'un beklediği parçaları
    taşır: {wfo_windows, wfo_summary, split, mc, full_error}.
    """
    import sys

    _start_parent_watchdog()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    try:
        spec, bars_df, recipe, params = payload
        instrument, bar_type = _build_instrument_bar_type(recipe)

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
            f"Walk-Forward başlıyor · train={params['train_months']}ay "
            f"test={params['test_months']}ay"
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

        prog("Monte Carlo için tam backtest çalıştırılıyor…")
        full_result = run_composed_backtest(
            spec,
            bars_df,
            iteration_id=999,
            rationale="Robustness full run",
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        mc_result = {"error": "Trade verisi yok."}
        if not full_result.error and full_result.trades:
            prog(
                f"Monte Carlo · {params['n_sims']} simülasyon · "
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
    """Manuel robustness suite'ini killable child'da koştur (sunucu donmaz).

    ``params`` = {train_months, test_months, n_optimize, objective, split_pct,
    n_sims}. Timeout/çökmede ``{"error": ...}`` döner.
    """
    payload = (spec, bars_df, recipe, params)
    result, err = _run_in_child(
        _manual_suite_child, payload, progress_fn, timeout_s, daemon=False
    )
    if err is not None:
        return {"error": err}
    return result


def _legacy_backtest_child(q, payload):
    """Child target: legacy STRATEGY_REGISTRY backtest'i (loop 'agent' modu)."""
    import sys

    _start_parent_watchdog()  # M8: hard-kill'de öksüz kalmasın
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
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
    """Legacy (STRATEGY_REGISTRY) backtest'i killable child'da koştur."""
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
        instrument, bar_type = _build_instrument_bar_type(recipe)
        return run_composed_backtest(
            spec,
            bars_df,
            iteration_id=iteration_id,
            rationale=rationale,
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
            progress_fn=progress_fn,
        )

    # ── Sandbox path: custom code → one killable child for the whole run ─────
    payload = (spec, bars_df, recipe, iteration_id, rationale)
    result, err = _run_in_child(_child_entry, payload, progress_fn, timeout_s)
    if err is not None:
        return _error_result(iteration_id, rationale, err)
    return result
