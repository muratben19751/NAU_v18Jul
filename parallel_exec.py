"""Process-pool executor for the robustness suite's independent backtests.

The autonomous agent's robustness suite (`_run_full_robustness`) fires hundreds
of INDEPENDENT Nautilus backtests (WFO window×candidate units, IS/OOS pair,
multi-symbol runs) which historically ran sequentially inside one sandbox child.
This module fans them out over a spawn-context ``ProcessPoolExecutor`` so a
16-core machine actually gets used.

Design contract (mirrors sandbox.py):
  * Only primitives cross the process boundary — spec dicts, ISO date strings,
    a string ``recipe``. pyo3 Nautilus objects are rebuilt inside each worker
    via ``sandbox._build_instrument_bar_type``.
  * ``bars_df`` is written ONCE to a temp parquet snapshot; each worker loads it
    once in its initializer and slices per task — no per-task DataFrame pickling.
  * Every worker runs a parent-liveness watchdog: if the pool owner (the
    robustness sandbox child) is hard-killed by the server's timeout, workers
    ``os._exit`` within ~1s instead of orphaning.
  * Determinism: candidate GENERATION stays in the parent (seeded RNG); workers
    only evaluate. Reduction happens in ascending candidate order with the same
    strictly-greater rule as the sequential path — identical winners.

Kill switch: ``NAUTILUS_PARALLEL=0`` → callers take their untouched sequential
branch. Worker count: ``NAUTILUS_PARALLEL_WORKERS`` (default ``min(cpu - 2,
available_RAM / 1.5 GB)``, clamped to [1, 28]); ``NAUTILUS_WORKER_MEM_GB``
tunes the per-worker RAM budget.

This module keeps its top level import-light: spawn re-imports it in every
worker, so pandas/nautilus/composer are imported lazily inside functions.

Wiki References
---------------
See: [[nau_holdout_dogrulama_turu_2026_08_18]] (multi-symbol biriminin peer
penceresi mühre çapalanır — bu modül `end_ms`'i okumayıp kendi kesimini
yaptığında mühürlü kuyruk paralel yoldan geri geliyordu),
[[multi_symbol_generalization]], [[webapp_module_map]]
"""

from __future__ import annotations

import os
import tempfile

from app_constants import env_float as _env_float

DEFAULT_WORKER_CLAMP = (1, 28)

# Worker-process globals, populated once by _worker_init.
_G: dict = {}


def parallel_enabled() -> bool:
    """Kill switch — NAUTILUS_PARALLEL=0 disables all pool usage."""
    return os.environ.get("NAUTILUS_PARALLEL", "1") != "0"


def get_worker_count() -> int:
    """Pool size: env override, else min(cpu - 2, RAM-based cap).

    Each worker loads a full bar snapshot + a live Nautilus engine, so peak
    RAM scales ~linearly with worker count. On memory-constrained hosts the
    CPU-based count (cpu - 2) can OOM, so we also cap by available RAM,
    budgeting ~1.5 GB/worker. NAUTILUS_PARALLEL_WORKERS overrides both;
    NAUTILUS_WORKER_MEM_GB tunes the per-worker budget.
    """
    lo, hi = DEFAULT_WORKER_CLAMP
    raw = os.environ.get("NAUTILUS_PARALLEL_WORKERS")
    if raw:
        try:
            return max(lo, min(hi, int(raw)))
        except ValueError:
            pass
    cpu = os.cpu_count() or 4
    count = cpu - 2

    # RAM-aware cap (best effort). Prefer psutil's available memory; fall back
    # to total physical RAM via sysconf (POSIX) so the guard still applies
    # without the psutil dependency.
    try:
        avail_gb = None
        try:
            import psutil

            avail_gb = psutil.virtual_memory().available / (1024**3)
        except Exception:
            if hasattr(os, "sysconf"):
                try:
                    total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                    # Assume ~70% is usable headroom for workers.
                    avail_gb = 0.7 * total / (1024**3)
                except (ValueError, OSError):
                    avail_gb = None
        if avail_gb is not None:
            per_worker_gb = _env_float("NAUTILUS_WORKER_MEM_GB", 1.5)
            ram_cap = max(1, int(avail_gb / per_worker_gb))
            count = min(count, ram_cap)
    except Exception:
        pass

    return max(lo, min(hi, count))


def make_snapshot(bars_df) -> str:
    """Write ``bars_df`` once to a temp parquet; returns the file path.

    Workers load this in their initializer. A private snapshot (not the live
    bybit cache file) is used because it is byte-exact with the DataFrame the
    agent holds and immune to concurrent cache writes. Caller removes the
    containing directory when done.
    """
    tmp_dir = tempfile.mkdtemp(prefix="nautilus_rob_")
    path = os.path.join(tmp_dir, "bars.parquet")
    bars_df.to_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Worker side (runs in spawned pool processes)
# ---------------------------------------------------------------------------


def _worker_init(snapshot_path: str, recipe: dict) -> None:
    """Per-worker one-time setup: UTF-8 stdout, parent watchdog, data load,
    instrument/bar_type rebuild, composer import (registers custom blocks)."""
    import multiprocessing as mp
    import threading

    from sandbox import _child_stdio_guard

    # Progress/log strings contain Turkish/glyph chars; fresh Windows children
    # need UTF-8 stdout or print() crashes on cp125x. pythonw children (PM2)
    # additionally boot with stdout/stderr = None — reuse sandbox.py's guard
    # instead of only reconfigure(), which raises AttributeError on None and
    # left the worker's first print() to crash silently under the bare except
    # this used to have.
    _child_stdio_guard()

    # Parent-liveness watchdog: the pool owner is the robustness sandbox child;
    # when the server hard-kills it (timeout), TerminateProcess gives workers no
    # signal — poll and self-exit so no orphan keeps burning CPU.
    parent = mp.parent_process()

    def _watchdog() -> None:
        import time

        while True:
            if parent is None or not parent.is_alive():
                os._exit(1)
            time.sleep(1.0)

    threading.Thread(target=_watchdog, daemon=True).start()

    import pandas as pd

    from sandbox import _build_instrument_bar_type

    _G["df"] = pd.read_parquet(snapshot_path)
    # The snapshot frame doubles as the price-scale hint: an unlisted sub-cent
    # symbol must not get the 2-decimal Bybit default here either, or every
    # worker would backtest an all-zero series (DeepR 2026-08-11 [YÜKSEK]).
    _G["instrument"], _G["bar_type"] = _build_instrument_bar_type(recipe, _G["df"])
    _G["recipe"] = recipe

    # Register persisted custom blocks (composer loads them at import) so
    # agent-generated custom-block specs evaluate identically here.
    import composer  # noqa: F401


def _run_unit(unit: dict) -> dict:
    """Evaluate one backtest unit. Never raises — errors return in-band.

    Unit schema (primitives only):
      {"key": str, "kind": "slice"|"symbol", "spec": spec_dict,
       "iteration_id": int, "rationale": str, "want_equity": bool,
       # kind="slice": one of
       "start": iso, "end": iso            # date mask on the snapshot df
       "irange": [i0, i1]                  # positional iloc split (IS/OOS)
       # kind="symbol":
       "symbol": str, "interval": str, "category": str, "days": int,
       "source": "bybit"|"external"   # external: symbol = catalog instrument id,
                                      # interval = katalog DSL ("1-DAY" ...)}

    Returns {"key", "metrics", "error", "n_trades", "equity_curve"?, "n_bars"?}.
    """
    key = unit.get("key", "?")
    try:
        import pandas as pd

        from backtest import run_composed_backtest
        from composer import ComposedStrategySpec

        spec = ComposedStrategySpec.from_dict(unit["spec"])

        if unit.get("kind") == "symbol":
            from datetime import UTC, datetime, timedelta

            from sandbox import _build_instrument_bar_type

            sym = unit["symbol"]
            days = int(unit.get("days", 180))
            if unit.get("source") == "external":
                from data import load_external_bars

                df = load_external_bars(sym, unit["interval"])
                if not df.empty:
                    # Kesim SIRALI YOLLA AYNI fonksiyondan geçiyor. Bu dal
                    # kendi kopyasını taşıyordu ve `end_ms`'i okumuyordu: sıralı
                    # yol mühre göre kesilse bile paralel yol serinin kendi
                    # sonuna göre kesip mühürlü kuyruğu peer testine geri
                    # sokuyordu. İki kopya = iki farklı pencere.
                    from backtest_robustness import _clip_peer_window

                    end_ms = unit.get("end_ms")
                    df = _clip_peer_window(
                        df,
                        days,
                        datetime.fromtimestamp(end_ms / 1000, UTC)
                        if end_ms is not None
                        else None,
                    )
                recipe = {
                    "source": "external",
                    "instrument_id": sym,
                    "granularity": unit["interval"],
                }
            else:
                from data import load_bybit_bars

                # If the window was fixed in the parent (start/end_ms) use it —
                # all symbols run over the same range; otherwise backward-compat now()-days.
                if unit.get("end_ms") is not None and unit.get("start_ms") is not None:
                    end_dt = datetime.fromtimestamp(unit["end_ms"] / 1000, UTC)
                    start_dt = datetime.fromtimestamp(unit["start_ms"] / 1000, UTC)
                else:
                    end_dt = datetime.now(UTC)
                    start_dt = end_dt - timedelta(days=days)
                df = load_bybit_bars(
                    symbol=sym,
                    interval=unit["interval"],
                    category=unit.get("category", "linear"),
                    start=start_dt,
                    end=end_dt,
                )
                recipe = {
                    "symbol": sym,
                    "interval": unit["interval"],
                    "category": unit.get("category", "linear"),
                }
            if df.empty:
                return {
                    "key": key,
                    "metrics": {},
                    "error": "no data",
                    "n_trades": 0,
                    "n_bars": 0,
                }
            instrument, bar_type = _build_instrument_bar_type(recipe, df)
            bars = df
        else:
            instrument, bar_type = _G["instrument"], _G["bar_type"]
            df = _G["df"]
            if "irange" in unit:
                i0, i1 = unit["irange"]
                bars = df.iloc[i0:i1]
            else:
                bars = df.loc[
                    (df.index >= pd.Timestamp(unit["start"]))
                    & (df.index < pd.Timestamp(unit["end"]))
                ]

        result = run_composed_backtest(
            spec,
            bars,
            iteration_id=int(unit.get("iteration_id", 0)),
            rationale=unit.get("rationale", ""),
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        payload: dict = {
            "key": key,
            "metrics": (result.metrics or {}) if not result.error else {},
            "error": result.error,
            "n_trades": ((result.metrics or {}).get("n_trades", 0) or 0)
            if not result.error
            else 0,
        }
        if unit.get("kind") == "symbol" and not result.error:
            # Multi-symbol acceptance is based on alpha, so compute buy-and-hold
            # over this worker's exact data slice (not the parent's primary bar
            # series).  Keep the value in the metrics envelope consumed by the
            # robustness reducer.
            from app_constants import stamp_buy_hold_benchmark

            stamp_buy_hold_benchmark(payload["metrics"], bars)
        if unit.get("want_equity"):
            payload["equity_curve"] = result.equity_curve or []
        if unit.get("kind") == "symbol":
            payload["n_bars"] = len(bars)
        return payload
    except Exception as e:  # noqa: BLE001 — must stay in-band, never sink the suite
        return {
            "key": key,
            "metrics": {},
            "error": f"{type(e).__name__}: {e}",
            "n_trades": 0,
        }


# ---------------------------------------------------------------------------
# Parent side (runs in the robustness sandbox child)
# ---------------------------------------------------------------------------


class BacktestPool:
    """Spawn-context ProcessPoolExecutor wrapper for backtest units.

    Context manager; ``run_units`` submits a batch and collects results as they
    complete. A ``BrokenProcessPool`` propagates to the caller, which falls back
    to the sequential path (the pool is then dead by definition).
    """

    def __init__(
        self,
        snapshot_path: str,
        recipe: dict,
        max_workers: int | None = None,
    ) -> None:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        self.max_workers = max_workers or get_worker_count()
        # M23: when the pool is rebuilt after a timeout the same initargs are needed.
        self._init_args = (snapshot_path, recipe)
        self._pool = ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=mp.get_context("spawn"),
            initializer=_worker_init,
            initargs=self._init_args,
        )

    def run_units(
        self, units: list[dict], *, progress_cb=None, timeout_s: float | None = None
    ) -> dict[str, dict]:
        """Run all units; returns {key -> payload}. Order-independent.

        M23: if ``timeout_s`` is given the batch cannot exceed this budget — units
        whose time runs out (incomplete) are converted to a ``{'error': 'unit timeout'}``
        payload and the pool is REBUILT. Note: ``cancel_futures`` does NOT KILL a
        running worker; the only way to escape a hung worker is to abandon the pool
        and create a fresh ProcessPoolExecutor (the spawn cost is preferable to
        holding the entire 900 s suite hostage).
        """
        from concurrent.futures import TimeoutError as _FutTimeout
        from concurrent.futures import as_completed

        if not units:
            return {}
        futures = {self._pool.submit(_run_unit, u): u["key"] for u in units}
        out: dict[str, dict] = {}
        done = 0
        total = len(futures)
        try:
            for fut in as_completed(futures, timeout=timeout_s):
                key = futures[fut]
                payload = fut.result()  # BrokenProcessPool propagates to caller
                out[payload.get("key", key)] = payload
                done += 1
                if progress_cb:
                    try:
                        progress_cb(done, total, key)
                    except Exception:
                        pass
        except _FutTimeout:
            # M300: at the timeout moment also collect futures that COMPLETED but
            # as_completed has not yet yielded — otherwise done results are silently
            # dropped (the key never appears in out, the caller sees None).
            for fut, key in futures.items():
                if key in out:
                    continue
                if fut.done() and not fut.cancelled():
                    try:
                        payload = fut.result()
                        out[payload.get("key", key)] = payload
                        continue
                    except Exception:
                        pass
                out.setdefault(
                    key,
                    {
                        "key": key,
                        "error": f"unit timeout ({timeout_s:.0f}s batch budget)",
                        "metrics": None,
                    },
                )
            # A pool with a hung worker cannot be recovered — abandon it, build a fresh pool.
            import multiprocessing as mp

            old_pool = self._pool
            old_pool.shutdown(wait=False, cancel_futures=True)
            # cancel_futures only cancels QUEUED futures; the RUNNING worker (the
            # hung unit that caused the timeout) continues — explicitly terminate the
            # processes so they don't burn 2x CPU alongside / pile up with the new pool.
            for _proc in (getattr(old_pool, "_processes", None) or {}).values():
                try:
                    _proc.terminate()
                    # terminate() only signals; without a join the exited child
                    # stays a zombie (and its handle leaks on Windows). A short
                    # bounded wait, then SIGKILL and reap — a rebuilt pool per
                    # timeout would otherwise accumulate one leak per worker.
                    _proc.join(timeout=5)
                    if _proc.is_alive():
                        _proc.kill()
                        _proc.join(timeout=5)
                except Exception:
                    pass
            self._pool = type(self._pool)(
                max_workers=self.max_workers,
                mp_context=mp.get_context("spawn"),
                initializer=_worker_init,
                initargs=self._init_args,
            )
        return out

    def shutdown(self) -> None:
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def __enter__(self) -> BacktestPool:
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
