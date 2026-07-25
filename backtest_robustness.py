"""Robustness analysis module — Walk-Forward, Monte Carlo, In/Out-of-Sample, Multi-Symbol.

All functions are built on top of `run_composed_backtest`; they do not require a
separate Nautilus API.

Wiki References
---------------
See: [[backtesting_guide]], [[backtest_node]]
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, timedelta

import numpy as np
import pandas as pd


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Embargo (purge) gap, in days (M28), placed between train and test (and between
# folds in wfo_optimizer): prevents an open position at the end of train /
# lookback leakage from spilling into the test window. NAU pattern.
WF_EMBARGO_DAYS = max(0.0, _env_float("NAUTILUS_WF_EMBARGO_DAYS", 2))

# M268/M431: unit-timeout for the parallel batches in robustness — so a hung
# custom-block unit does not hold the whole suite hostage to the sandbox's 900s
# global timeout (wholesale loss of all the work). Batch budget < global timeout.
# On run_units timeout it rebuilds the pool and turns the missing unit into a
# 'unit timeout'.
WFO_BATCH_TIMEOUT_S = _env_float("NAUTILUS_WFO_BATCH_TIMEOUT_S", 600.0)


def _run_many_kw(run_many):
    """Pass timeout_s if run_many supports it (parallel_exec.run_units); silently
    skip for old-signature callables (tests)."""
    import inspect

    try:
        params = inspect.signature(run_many).parameters
        if "timeout_s" in params:
            return {"timeout_s": WFO_BATCH_TIMEOUT_S}
    except (TypeError, ValueError):
        pass
    return {}


def _isnan_num(x) -> bool:
    """None/unparseable/NaN → True (scalar metric guard)."""
    try:
        return np.isnan(float(x))
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Walk-Forward Optimization
# ---------------------------------------------------------------------------


def _wfo_window_bounds(
    total_start, total_end, train_months: int, test_months: int, step_months: int
) -> list[tuple]:
    """Precompute WFO window date bounds — the shared math for the sequential and parallel paths.

    - Months are REAL calendar months (``pd.DateOffset(months=n)``), not a 30-day
      approximation (L22): a 6-month train really lasts 6 months.
    - ``test_start = train_end + WF_EMBARGO_DAYS`` (M28): embargo gap.

    Returns [(window_n, train_start, train_end, test_start, test_end), ...]
    (tz-aware ``pd.Timestamp`` values).
    """
    # M62: step_months<=0 never advances the cursor → infinite loop + unbounded
    # bounds (memory), burns CPU until the sandbox child timeout. train/test<=0 is
    # also meaningless. The direct-POST path did not clamp (only split_pct was clamped).
    if step_months <= 0 or train_months <= 0 or test_months <= 0:
        return []
    bounds = []
    cursor = pd.Timestamp(total_start)
    end_ts = pd.Timestamp(total_end)
    window_n = 0
    while True:
        train_start = cursor
        train_end = train_start + pd.DateOffset(months=train_months)
        test_start = train_end + timedelta(days=WF_EMBARGO_DAYS)
        test_end = test_start + pd.DateOffset(months=test_months)
        if test_end > end_ts:
            break
        window_n += 1
        bounds.append((window_n, train_start, train_end, test_start, test_end))
        cursor = cursor + pd.DateOffset(months=step_months)
    return bounds


def run_walk_forward(
    spec,
    bars_df: pd.DataFrame,
    instrument,
    bar_type,
    venue,
    train_months: int = 6,
    test_months: int = 1,
    step_months: int = 1,
    progress_fn=None,
    n_optimize: int = 20,
    objective: str = "sharpe",
    min_trades: int = 5,
    run_many=None,
) -> list[dict]:
    """Sliding-window Walk-Forward *Optimization*.

    Each window: optimize parameters over the TRAIN period (lightweight GA — elitism +
    tournament + crossover + mutation, k-fold embargoed score; numeric knobs only;
    see ``wfo_optimizer``), apply the selected parameters over the TEST period.
    The unchanged (naive) spec is also run over the same test window — so
    "did optimization help OOS?" can be compared honestly. Between train and test
    there is a ``WF_EMBARGO_DAYS``-day embargo gap (M28).

    ``run_many`` (optional): a callable with the ``parallel_exec.BacktestPool.run_units``
    signature. When provided, the train backtests are batched per GA generation and
    distributed to a process pool (candidate generation stays in the parent since
    generation N+1's population depends on N's scores); then the OOS test backtests
    are batched. Seeds and selection rules are identical to the sequential path →
    the winners are deterministically identical.

    Returns: list of {
        window, train_start, train_end, test_start, test_end,
        chosen_params, train_objective, objective_metric,
        train_metrics, test_metrics (optimized OOS), test_metrics_naive (naive OOS),
        train_equity, test_equity, train_n_trades, test_n_trades
    }
    """
    from backtest import run_composed_backtest
    from wfo_optimizer import build_param_space

    def _p(msg: str) -> None:
        if progress_fn:
            try:
                progress_fn(msg)
            except Exception:
                pass

    if bars_df.empty:
        return []

    space = build_param_space(spec)
    if not space:
        _p(
            "WFO: no optimizable numeric parameters — "
            "a naive rolling OOS evaluation will run."
        )

    idx = bars_df.index
    total_start = idx[0].to_pydatetime().replace(tzinfo=UTC)
    total_end = idx[-1].to_pydatetime().replace(tzinfo=UTC)

    bounds = _wfo_window_bounds(
        total_start, total_end, train_months, test_months, step_months
    )

    if run_many is not None:
        return _run_walk_forward_parallel(
            spec,
            bars_df,
            bounds,
            space,
            run_many,
            _p,
            n_optimize=n_optimize,
            objective=objective,
            min_trades=min_trades,
        )

    windows = []

    for window_n, train_start, train_end, test_start, test_end in bounds:
        _p(
            f"WFO window {window_n}: train {train_start.date()} → {train_end.date()}, "
            f"test {test_start.date()} → {test_end.date()} · "
            f"{len(space)} param · GA budget {n_optimize}"
        )

        train_bars = bars_df.loc[
            (bars_df.index >= pd.Timestamp(train_start))
            & (bars_df.index < pd.Timestamp(train_end))
        ]
        test_bars = bars_df.loc[
            (bars_df.index >= pd.Timestamp(test_start))
            & (bars_df.index < pd.Timestamp(test_end))
        ]

        best = None
        opt_spec = spec
        if not train_bars.empty:
            from wfo_optimizer import optimize_window

            best = optimize_window(
                spec,
                train_bars,
                space,
                instrument,
                bar_type,
                venue,
                n_samples=n_optimize,
                seed=window_n,  # deterministic per window; varies across windows
                objective=objective,
                min_trades=min_trades,
                run_fn=run_composed_backtest,
                iteration_id=window_n * 100,
            )
            opt_spec = best["spec"]
            _p(f"  window {window_n} selected: {best['params']}")

        # Optimized spec on OOS test window.
        test_result = None
        if not test_bars.empty:
            test_result = run_composed_backtest(
                opt_spec,
                test_bars,
                iteration_id=window_n * 100 + 1,
                rationale=f"WFO test w{window_n} (optimized)",
                instrument=instrument,
                bar_type=bar_type,
                venue=venue,
            )

        # Naive (unchanged) spec on the SAME OOS window — honest baseline.
        naive_result = None
        if not test_bars.empty and space:
            naive_result = run_composed_backtest(
                spec,
                test_bars,
                iteration_id=window_n * 100 + 2,
                rationale=f"WFO test w{window_n} (naive)",
                instrument=instrument,
                bar_type=bar_type,
                venue=venue,
            )

        windows.append(
            _wfo_window_entry(
                window_n,
                train_start,
                train_end,
                test_start,
                test_end,
                best,
                objective,
                test_metrics=(
                    test_result.metrics if test_result and not test_result.error else {}
                ),
                test_metrics_naive=(
                    naive_result.metrics
                    if naive_result and not naive_result.error
                    else {}
                ),
                test_equity=test_result.equity_curve if test_result else [],
                test_n_trades=(
                    test_result.metrics.get("n_trades", 0)
                    if test_result and not test_result.error
                    else 0
                ),
            )
        )

    _p(f"WFO completed · {len(windows)} window")
    return windows


def _derive_test_objective(metrics: dict, objective: str):
    """From the test metrics, derive the SAME metric used in train's ``objective``
    (M7): sharpe→sharpe, sortino→sortino, calmar/return_dd→pnl/abs(max_dd).

    max_dd is a NEGATIVE fraction in this repo — abs() is mandatory. If the
    denominator is 0/None/NaN (or the metric is missing), returns None; efficiency
    skips that window.
    """
    if not metrics:
        return None
    if objective == "sortino":
        v = metrics.get("sortino")
    elif objective in ("calmar", "return_dd"):
        pnl = metrics.get("pnl", 0.0) or 0.0
        dd = metrics.get("max_dd")
        if dd in (None, 0) or _isnan_num(dd):
            return None
        v = pnl / abs(dd)
    else:  # sharpe (default)
        v = metrics.get("sharpe")
    if v is None or _isnan_num(v):
        return None
    return float(v)


def _wfo_window_entry(
    window_n,
    train_start,
    train_end,
    test_start,
    test_end,
    best,
    objective,
    *,
    test_metrics,
    test_metrics_naive,
    test_equity,
    test_n_trades,
) -> dict:
    """Assemble one WFO window dict — single shape shared by both paths."""
    test_obj = _derive_test_objective(test_metrics or {}, objective)
    return {
        "window": window_n,
        "train_start": str(train_start.date()),
        "train_end": str(train_end.date()),
        "test_start": str(test_start.date()),
        "test_end": str(test_end.date()),
        "chosen_params": best["params"] if best else {},
        "train_objective": (
            None
            if best is None or best["objective"] == float("-inf")
            else round(best["objective"], 4)
        ),
        # M7: OOS objective is also written to the window record so efficiency's
        # numerator/denominator use the same metric; the metric name used is in
        # objective_metric.
        "test_objective": (round(test_obj, 4) if test_obj is not None else None),
        "objective_metric": objective,
        "train_metrics": best["metrics"] if best else {},
        "test_metrics": test_metrics,
        "test_metrics_naive": test_metrics_naive,
        "train_equity": [],
        "test_equity": test_equity,
        "train_n_trades": (best["metrics"].get("n_trades", 0) if best else 0),
        "test_n_trades": test_n_trades,
    }


def _run_walk_forward_parallel(
    spec,
    bars_df: pd.DataFrame,
    bounds: list[tuple],
    space: list[dict],
    run_many,
    _p,
    *,
    n_optimize: int,
    objective: str,
    min_trades: int,
) -> list[dict]:
    """Parallel WFO: runs GA generations in lockstep across windows — all
    (window × candidate × fold) units of generation g are ONE pool batch;
    scores are reduced in the parent and the next generation evolves in the
    parent (H10 + L35).

    Parity contract with the sequential ``optimize_window``:
      - per-window ``rng = default_rng(window_n)`` and the SAME
        ``ga_initial_population``/``ga_next_population`` call order,
      - fold bounds via ``build_fold_bounds`` in the parent (no randomness
        leaks to the worker),
      - invalid candidates are not run (score -inf), and if any fold is -inf the
        candidate is rejected; score = ``penalized_score`` (mean − 0.5·std),
      - strictly-greater rule + generation/candidate order → the exact same winner,
      - window dicts are assembled via the shared ``_wfo_window_entry``.
    """
    from types import SimpleNamespace

    from wfo_optimizer import (
        WF_MIN_VALID_FOLDS_FRAC,
        _current_values,
        build_fold_bounds,
        ga_initial_population,
        ga_next_population,
        ga_plan,
        mutate_spec,
        objective_value,
        penalized_score,
        values_to_dict,
    )

    def _heartbeat(label):
        def cb(done, total, _key):
            if done % 10 == 0 or done == total:
                _p(f"  {label}: {done}/{total} completed")

        return cb

    # ── Phase A: GA generations — windows in lockstep ────────────────────────
    pop_size, n_gen = ga_plan(space, n_optimize)
    meta: dict[int, dict] = {}
    for window_n, train_start, train_end, test_start, test_end in bounds:
        _p(
            f"WFO window {window_n}: train {train_start.date()} → {train_end.date()}, "
            f"test {test_start.date()} → {test_end.date()} · "
            f"{len(space)} param · GA {pop_size}×{n_gen}"
        )
        train_bars = bars_df.loc[
            (bars_df.index >= pd.Timestamp(train_start))
            & (bars_df.index < pd.Timestamp(train_end))
        ]
        test_bars = bars_df.loc[
            (bars_df.index >= pd.Timestamp(test_start))
            & (bars_df.index < pd.Timestamp(test_end))
        ]
        m: dict = {
            "bounds": (train_start, train_end, test_start, test_end),
            "train_empty": train_bars.empty,
            "test_empty": test_bars.empty,
            "best": None,
            "evaluated": 0,
        }
        if not train_bars.empty:
            rng = np.random.default_rng(window_n)
            m["rng"] = rng
            m["population"] = ga_initial_population(spec, space, rng, pop_size)
            fb = build_fold_bounds(train_bars)
            if not fb:
                fb = [
                    (
                        pd.Timestamp(train_bars.index[0]),
                        pd.Timestamp(train_bars.index[-1]) + pd.Timedelta(1, "ns"),
                    )
                ]
            m["fold_bounds"] = fb
        meta[window_n] = m

    for gen in range(n_gen):
        units_a: list[dict] = []
        for window_n, *_rest in bounds:
            m = meta[window_n]
            if m["train_empty"]:
                continue
            cands: list[tuple] = []
            for ci, values in enumerate(m["population"]):
                cand = mutate_spec(spec, space, values)
                if cand.validate() is not None:
                    # Invalid candidate is not run — score -inf (sequential-path parity).
                    cands.append((ci, values, None))
                    continue
                cands.append((ci, values, cand))
                for fi, (fs, fe) in enumerate(m["fold_bounds"]):
                    units_a.append(
                        {
                            "key": f"w{window_n}g{gen}c{ci}f{fi}",
                            "kind": "slice",
                            "spec": cand.to_dict(),
                            "start": fs.isoformat(),
                            "end": fe.isoformat(),
                            "iteration_id": window_n * 100,
                            "rationale": "WFO train-optimize",
                        }
                    )
            m["cands"] = cands
        train_payloads = (
            run_many(
                units_a,
                progress_cb=_heartbeat(f"WFO train g{gen + 1}/{n_gen}"),
                **_run_many_kw(run_many),
            )
            if units_a
            else {}
        )

        for window_n, *_rest in bounds:
            m = meta[window_n]
            if m["train_empty"]:
                continue
            scores: list[float] = []
            for ci, values, cand in m["cands"]:
                if cand is None:
                    scores.append(float("-inf"))
                    continue
                fold_objs: list[float] = []
                last_metrics: dict = {}
                _n_folds = len(m["fold_bounds"])
                # M462 parity: ALL folds are evaluated; if the valid-fold ratio
                # >=0.6 the candidate survives (same rule as sequential optimize_window).
                for fi in range(_n_folds):
                    payload = train_payloads.get(f"w{window_n}g{gen}c{ci}f{fi}")
                    m["evaluated"] += 1
                    if payload is None:
                        continue  # -inf fold (invalid candidate that was not run)
                    res = SimpleNamespace(
                        error=payload.get("error"),
                        metrics=payload.get("metrics") or {},
                    )
                    obj = objective_value(res, objective, min_trades)
                    if obj != float("-inf"):
                        fold_objs.append(obj)
                        last_metrics = res.metrics
                score = (
                    penalized_score(fold_objs)
                    if fold_objs
                    and len(fold_objs) >= WF_MIN_VALID_FOLDS_FRAC * _n_folds
                    else float("-inf")
                )
                scores.append(score)
                if score != float("-inf") and (
                    m["best"] is None or score > m["best"]["objective"]
                ):
                    m["best"] = {
                        "values": values,
                        "params": values_to_dict(space, values),
                        "objective": score,
                        "spec": cand,
                        "metrics": last_metrics,
                    }
            if gen < n_gen - 1:
                m["population"] = ga_next_population(
                    m["rng"], space, m["population"], scores, pop_size
                )

    # ── Reduce: fallback + n_evaluated + test units ─────────────────────────
    units_b: list[dict] = []
    for window_n, _ts, _te, _s, _e in bounds:
        m = meta[window_n]
        best = m["best"]
        if not m["train_empty"]:
            if best is None:
                # No candidate produced a valid score — optimize_window fallback.
                cur = _current_values(spec, space)
                best = {
                    "values": cur,
                    "params": values_to_dict(space, cur),
                    "objective": float("-inf"),
                    "spec": spec,
                    "metrics": {},
                }
            best["n_evaluated"] = m["evaluated"]
            _p(f"  window {window_n} selected: {best['params']}")
        m["best"] = best

        opt_spec = best["spec"] if best else spec
        train_start, train_end, test_start, test_end = m["bounds"]
        if not m["test_empty"]:
            units_b.append(
                {
                    "key": f"w{window_n}t",
                    "kind": "slice",
                    "spec": opt_spec.to_dict(),
                    "start": test_start.isoformat(),
                    "end": test_end.isoformat(),
                    "iteration_id": window_n * 100 + 1,
                    "rationale": f"WFO test w{window_n} (optimized)",
                    "want_equity": True,
                }
            )
            if space:
                units_b.append(
                    {
                        "key": f"w{window_n}n",
                        "kind": "slice",
                        "spec": spec.to_dict(),
                        "start": test_start.isoformat(),
                        "end": test_end.isoformat(),
                        "iteration_id": window_n * 100 + 2,
                        "rationale": f"WFO test w{window_n} (naive)",
                    }
                )

    test_payloads = (
        run_many(units_b, progress_cb=_heartbeat("WFO test"), **_run_many_kw(run_many))
        if units_b
        else {}
    )

    # ── Assemble windows (identical shape to the sequential path) ────────────
    windows = []
    for window_n, train_start, train_end, test_start, test_end in bounds:
        m = meta[window_n]
        pt = test_payloads.get(f"w{window_n}t")
        pn = test_payloads.get(f"w{window_n}n")
        windows.append(
            _wfo_window_entry(
                window_n,
                train_start,
                train_end,
                test_start,
                test_end,
                m.get("best"),
                objective,
                test_metrics=(
                    (pt.get("metrics") or {}) if pt and not pt.get("error") else {}
                ),
                test_metrics_naive=(
                    (pn.get("metrics") or {}) if pn and not pn.get("error") else {}
                ),
                test_equity=(pt.get("equity_curve") or []) if pt else [],
                test_n_trades=(
                    (pt.get("metrics") or {}).get("n_trades", 0)
                    if pt and not pt.get("error")
                    else 0
                ),
            )
        )

    _p(f"WFO completed · {len(windows)} window")
    return windows


def wfo_aggregate(windows: list[dict]) -> dict:
    """Aggregate WFO windows: mean OOS optimized-vs-naive, efficiency, and
    parameter stability (a coefficient-of-variation overfit signal).

    Returns {} when there are no windows.
    """
    if not windows:
        return {}

    def _mean(key_path):
        vals = []
        for w in windows:
            m = w.get(key_path[0]) or {}
            v = m.get(key_path[1])
            if v is not None and not _isnan_local(v):
                vals.append(float(v))
        return float(np.mean(vals)) if vals else None

    def _isnan_local(x):
        try:
            return np.isnan(float(x))
        except (TypeError, ValueError):
            return True

    oos_sharpe_opt = _mean(("test_metrics", "sharpe"))
    oos_sharpe_naive = _mean(("test_metrics_naive", "sharpe"))
    oos_pnl_opt = _mean(("test_metrics", "pnl"))
    oos_pnl_naive = _mean(("test_metrics_naive", "pnl"))

    # M28: NAU dispersion penalty — mean − 0.5·std (variance across windows is
    # penalized; a candidate that looks 'robust' from a single lucky window falls).
    # Consumer: agent _robustness_passed reads it via .get (backward compatible).
    def _penalized(key_path):
        vals = []
        for w in windows:
            v = w
            for k in key_path:
                v = (v or {}).get(k) if isinstance(v, dict) else None
            if v is not None and not _isnan_local(v):
                vals.append(float(v))
        if not vals:
            return None
        arr = np.asarray(vals, dtype=float)
        return float(arr.mean() - 0.5 * arr.std())

    oos_sharpe_penalized = _penalized(("test_metrics", "sharpe"))
    oos_pnl_penalized = _penalized(("test_metrics", "pnl"))
    is_obj = [
        w["train_objective"] for w in windows if w.get("train_objective") is not None
    ]
    is_obj_mean = float(np.mean(is_obj)) if is_obj else None

    # WFO efficiency: aggregate OOS objective / aggregate IS objective.
    efficiency = None
    if is_obj_mean not in (None, 0) and oos_sharpe_opt is not None:
        efficiency = round(oos_sharpe_opt / is_obj_mean, 3)

    # Parameter stability: per-param coefficient of variation across windows.
    param_cv: dict[str, float] = {}
    keys = set()
    for w in windows:
        keys.update((w.get("chosen_params") or {}).keys())
    for k in keys:
        series = [
            float(w["chosen_params"][k])
            for w in windows
            if k in (w.get("chosen_params") or {})
        ]
        if len(series) >= 2:
            mean = np.mean(series)
            std = np.std(series)
            param_cv[k] = round(float(std / abs(mean)), 3) if mean else None

    unstable = [k for k, cv in param_cv.items() if cv is not None and cv > 0.5]
    stability_label = "unstable (overfit risk)" if unstable else "stable"

    return {
        "n_windows": len(windows),
        "oos_sharpe_optimized": (
            round(oos_sharpe_opt, 3) if oos_sharpe_opt is not None else None
        ),
        "oos_sharpe_naive": (
            round(oos_sharpe_naive, 3) if oos_sharpe_naive is not None else None
        ),
        "oos_pnl_optimized": (
            round(oos_pnl_opt, 2) if oos_pnl_opt is not None else None
        ),
        "oos_pnl_naive": (
            round(oos_pnl_naive, 2) if oos_pnl_naive is not None else None
        ),
        "is_objective_mean": (
            round(is_obj_mean, 3) if is_obj_mean is not None else None
        ),
        "wfo_efficiency": efficiency,
        # M28: mean − 0.5·std (NAU dispersion penalty) — the gates prefer this.
        "oos_sharpe_penalized": (
            round(oos_sharpe_penalized, 3) if oos_sharpe_penalized is not None else None
        ),
        "oos_pnl_penalized": (
            round(oos_pnl_penalized, 2) if oos_pnl_penalized is not None else None
        ),
        "param_cv": param_cv,
        "unstable_params": unstable,
        "stability_label": stability_label,
    }


# ---------------------------------------------------------------------------
# Monte Carlo — Trade Shuffle
# ---------------------------------------------------------------------------


def run_monte_carlo(
    trades: list[dict],
    n_sims: int = 500,
    starting_cash: float = 1_000_000.0,
    progress_fn=None,
    method: str = "iid_bootstrap",
) -> dict:
    """Bootstrap Monte Carlo of the trade PnL distribution.

    Resamples the realized per-trade PnLs to build ``n_sims`` alternative equity
    paths, then reports distribution bands. Two resampling methods:

      - ``iid_bootstrap`` (default): sample trades WITH REPLACEMENT. Each sim is a
        different multiset, so final PnL and win-rate genuinely vary.
      - ``block_bootstrap``: resample contiguous BLOCKS of trades (length
        ≈ √n_trades) with replacement, preserving streak/autocorrelation in the
        trade sequence.

    NOTE: the previous implementation *permuted* a fixed set of PnLs. Because a
    permutation preserves the sum, every sim ended at the identical final equity
    and win-rate — so the final-PnL / win-rate bands were mathematically constant
    and conveyed no information. Sampling with replacement fixes that.

    Returns: {
        n_sims, n_trades, starting_cash, method,
        p5_final, p25_final, median_final, p75_final, p95_final,
        original_final,
        max_dd_p50, max_dd_p95,
        win_rate_mean, win_rate_std,
        curves_sample: list[list[float]]  — 50 sample curves (for the chart)
        percentile_curves: {p5, p25, p50, p75, p95}  — percentiles at each point
    }
    """

    def _p(msg: str) -> None:
        if progress_fn:
            try:
                progress_fn(msg)
            except Exception:
                pass

    if not trades:
        return {"error": "No trade data — run a backtest first."}

    pnls = [t.get("pnl", 0.0) for t in trades]
    n_trades = len(pnls)
    _p(f"Monte Carlo starting · {n_sims} simulations · {n_trades} trades · {method}")

    # Vectorized with numpy — n_sims × n_trades resample matrix.
    # NOT a permutation, but sampling WITH REPLACEMENT: since each sim is a
    # different multiset, final PnL and win-rate genuinely vary.
    rng = np.random.default_rng(seed=42)
    pnls_arr = np.asarray(pnls, dtype=float)
    if method == "block_bootstrap" and n_trades > 1:
        block_len = max(1, int(round(np.sqrt(n_trades))))
        n_blocks = int(np.ceil(n_trades / block_len))
        starts = rng.integers(0, n_trades, size=(n_sims, n_blocks))
        offsets = np.arange(block_len)
        # (n_sims, n_blocks, block_len) circular block indices → flatten → trim
        block_idx = (starts[:, :, None] + offsets[None, None, :]) % n_trades
        idx = block_idx.reshape(n_sims, n_blocks * block_len)[:, :n_trades]
    else:
        idx = rng.integers(0, n_trades, size=(n_sims, n_trades))
    shuffled = pnls_arr[idx]

    # Cumulative equity
    cumulative = starting_cash + np.cumsum(shuffled, axis=1)
    # Add the starting point
    start_col = np.full((n_sims, 1), starting_cash)
    all_curves = np.hstack([start_col, cumulative])  # n_sims × (n_trades+1)

    # Percentile bands (for each point)
    p5 = np.percentile(all_curves, 5, axis=0).tolist()
    p25 = np.percentile(all_curves, 25, axis=0).tolist()
    p50 = np.percentile(all_curves, 50, axis=0).tolist()
    p75 = np.percentile(all_curves, 75, axis=0).tolist()
    p95 = np.percentile(all_curves, 95, axis=0).tolist()

    # Final value distribution
    finals = all_curves[:, -1]
    original_final = starting_cash + sum(pnls)

    # Equity path by the real (un-shuffled) trade order — for the overlay (#32)
    real_curve = [starting_cash] + (starting_cash + np.cumsum(pnls)).tolist()

    # Max drawdown for each simulation
    max_dds = []
    for curve in all_curves:
        peak = np.maximum.accumulate(curve)
        dd = (curve - peak) / peak
        max_dds.append(float(dd.min()))

    max_dds_arr = np.array(max_dds)

    # Win rate distribution
    win_rates = [(sim > 0).sum() / n_trades for sim in shuffled]

    # 50 sample curves (do not send too much data to the chart)
    sample_idx = rng.choice(n_sims, size=min(50, n_sims), replace=False)
    curves_sample = all_curves[sample_idx].tolist()

    _p(f"Monte Carlo completed · median final: ${float(np.median(finals)):,.0f}")

    return {
        "n_sims": n_sims,
        "n_trades": n_trades,
        "starting_cash": starting_cash,
        "method": method,
        "original_final": round(original_final, 2),
        "p5_final": round(float(np.percentile(finals, 5)), 2),
        "p25_final": round(float(np.percentile(finals, 25)), 2),
        "median_final": round(float(np.median(finals)), 2),
        "p75_final": round(float(np.percentile(finals, 75)), 2),
        "p95_final": round(float(np.percentile(finals, 95)), 2),
        "max_dd_p50": round(float(np.median(max_dds_arr)) * 100, 2),
        "max_dd_p95": round(
            float(np.percentile(max_dds_arr, 5)) * 100, 2
        ),  # p5 of negative values = worst-case
        "win_rate_mean": round(float(np.mean(win_rates)) * 100, 2),
        "win_rate_std": round(float(np.std(win_rates)) * 100, 2),
        "curves_sample": curves_sample,
        "real_curve": real_curve,
        "percentile_curves": {"p5": p5, "p25": p25, "p50": p50, "p75": p75, "p95": p95},
    }


# ---------------------------------------------------------------------------
# In-Sample / Out-of-Sample Split
# ---------------------------------------------------------------------------


def run_insample_oos_split(
    spec,
    bars_df: pd.DataFrame,
    instrument,
    bar_type,
    venue,
    split_pct: float = 0.7,
    progress_fn=None,
    run_many=None,
) -> dict:
    """In-sample on the first 70% of the data, out-of-sample test on the remaining 30%.

    If ``run_many`` is given, the IS/OOS pair runs concurrently in the process pool
    (the positional iloc split is carried to the worker as ``irange`` — an identical slice).

    Returns: {
        split_pct, split_date,
        in_sample_metrics, oos_metrics,
        in_sample_equity, oos_equity,
        overfitting_score, overfitting_label,
    }
    """
    from backtest import run_composed_backtest

    def _p(msg: str) -> None:
        if progress_fn:
            try:
                progress_fn(msg)
            except Exception:
                pass

    # L6/edge: at len==1, split_idx=1 → bars_df.index[1] raised IndexError and
    # turned the WHOLE suite into a single error; at least 2 bars (>=1 per side)
    # required.
    if len(bars_df) < 2:
        return {"error": "Insufficient data (at least 2 bars required)."}

    # L6: at split_pct=1.0, index[split_idx] overflowed and turned the WHOLE suite
    # (including a completed WFO) into a single error via IndexError — clamp so both
    # sides get at least 1 bar.
    split_idx = max(1, min(int(len(bars_df) * split_pct), len(bars_df) - 1))
    in_bars = bars_df.iloc[:split_idx]
    oos_bars = bars_df.iloc[split_idx:]
    split_date = str(bars_df.index[split_idx].date())

    _p(f"In-sample: {len(in_bars):,} bar → {split_date} | OOS: {len(oos_bars):,} bar")

    if run_many is not None:
        payloads = run_many(
            [
                {
                    "key": "is",
                    "kind": "slice",
                    "spec": spec.to_dict(),
                    "irange": [0, split_idx],
                    "iteration_id": 900,
                    "rationale": "In-sample",
                    "want_equity": True,
                },
                {
                    "key": "oos",
                    "kind": "slice",
                    "spec": spec.to_dict(),
                    "irange": [split_idx, len(bars_df)],
                    "iteration_id": 901,
                    "rationale": "Out-of-sample",
                    "want_equity": True,
                },
            ],
            **_run_many_kw(run_many),
        )
        p_in = payloads.get("is") or {"metrics": {}, "error": "no result"}
        p_oos = payloads.get("oos") or {"metrics": {}, "error": "no result"}
        in_m = (p_in.get("metrics") or {}) if not p_in.get("error") else {}
        oos_m = (p_oos.get("metrics") or {}) if not p_oos.get("error") else {}
        in_equity = (p_in.get("equity_curve") or []) if not p_in.get("error") else []
        oos_equity = (p_oos.get("equity_curve") or []) if not p_oos.get("error") else []
        in_error = p_in.get("error")
        oos_error = p_oos.get("error")
    else:
        in_result = run_composed_backtest(
            spec,
            in_bars,
            iteration_id=900,
            rationale="In-sample",
            instrument=instrument,
            bar_type=bar_type,
            venue=venue,
        )
        oos_result = run_composed_backtest(
            spec,
            oos_bars,
            iteration_id=901,
            rationale="Out-of-sample",
            instrument=instrument,
            bar_type=bar_type,
            venue=venue,
        )
        in_m = in_result.metrics if not in_result.error else {}
        oos_m = oos_result.metrics if not oos_result.error else {}
        in_equity = in_result.equity_curve if not in_result.error else []
        oos_equity = oos_result.equity_curve if not oos_result.error else []
        in_error = in_result.error
        oos_error = oos_result.error

    import math as _math

    def _num(x):
        """Normalize None/NaN to None (clears the NaN-truthy trap) — #12."""
        if x is None:
            return None
        try:
            f = float(x)
            return None if _math.isnan(f) else f
        except (TypeError, ValueError):
            return None

    # Overfitting score: OOS / In-sample. If Sharpe is NaN (v2 multi-currency
    # quirk) fall back to Sortino — Sortino is downside-only and not affected by
    # this bug.
    in_sharpe = _num(in_m.get("sharpe"))
    oos_sharpe = _num(oos_m.get("sharpe"))
    metric_used = "Sharpe"
    if in_sharpe is None or oos_sharpe is None:
        in_sharpe = _num(in_m.get("sortino"))
        oos_sharpe = _num(oos_m.get("sortino"))
        metric_used = "Sortino"

    if not in_sharpe or in_sharpe <= 0 or oos_sharpe is None:
        score = None
        label = "— (yetersiz veri)"
    else:
        score = round(oos_sharpe / in_sharpe, 2)
        if score >= 0.5:  # 0.7→0.5: more realistic threshold for different market regimes
            label = "✓ Robust"
        elif score >= 0.25:  # 0.4→0.25
            label = "⚠ Caution"
        else:
            label = "✗ Overfitting suspected"

    _p(f"Split completed · overfitting score ({metric_used}): {score} ({label})")

    return {
        "split_pct": split_pct,
        "split_date": split_date,
        "in_sample_n_bars": len(in_bars),
        "oos_n_bars": len(oos_bars),
        "in_sample_metrics": in_m,
        "oos_metrics": oos_m,
        "in_sample_equity": in_equity,
        "oos_equity": oos_equity,
        "overfitting_score": score,
        "overfitting_label": label,
        "in_sample_error": in_error,
        "oos_error": oos_error,
    }


# ---------------------------------------------------------------------------
# Multi-Symbol Generalization Test
# ---------------------------------------------------------------------------


def run_multi_symbol(
    spec,
    primary_symbol: str,
    symbols: list[str],
    interval: str,
    category: str = "linear",
    days: int = 180,
    progress_fn=None,
    run_many=None,
    source: str = "bybit",
) -> dict:
    """Measures generalizability by testing the strategy on multiple symbols.

    Runs the same spec over the same time range in a backtest for each symbol.
    Results: how many symbols had positive PnL, average Sharpe, per-symbol detail.

    ``source="external"``: symbols are external-catalog instrument ids
    (e.g. "SPY.ARCA"), ``interval`` is the catalog DSL (e.g. "1-DAY"), and the
    window is cut relative to the data's own end (the catalog does not run up to "now").

    Returns: {
        symbols_tested: int,
        symbols_positive: int,          # those with PnL > 0
        pass_rate: float,               # positive / total
        generalization_label: str,      # "✓ Generalizable" / "⚠ Limited" / "✗ Symbol specific"
        primary_symbol: str,
        results: [
            {symbol, pnl, sharpe, n_trades, error}
        ]
    }
    """
    from datetime import datetime

    from backtest import (
        STARTING_CASH,
        _make_bybit_bar_type,
        _make_bybit_instrument,
        run_composed_backtest,
    )
    from data import load_bybit_bars

    def _p(msg: str) -> None:
        if progress_fn:
            try:
                progress_fn(msg)
            except Exception:
                pass

    end_dt = datetime.now(UTC)
    start_dt = end_dt - timedelta(days=days)
    cur = "USD" if source == "external" else "USDT"

    results = []
    _p(
        f"Multi-symbol test starting · {len(symbols)} symbols · "
        f"last {days} days · interval={interval}"
    )

    if run_many is not None:
        # Parallel branch: each symbol = one pool unit (data fetch + backtest in
        # the worker; distinct symbols → distinct cache files, no write race).
        # Completion lines are emitted after collection, in symbol order.
        units = [
            {
                "key": f"ms:{sym}",
                "kind": "symbol",
                "spec": spec.to_dict(),
                "symbol": sym,
                "interval": interval,
                "category": category,
                "days": days,
                # The window is fixed ONCE in the PARENT; if each worker anchored
                # to its own now(), the symbols in one batch (and the sequential↔parallel
                # paths) would be tested over SLIGHTLY different time windows. With
                # start/end_ms all symbols run over the SAME window.
                "start_ms": int(start_dt.timestamp() * 1000),
                "end_ms": int(end_dt.timestamp() * 1000),
                "source": source,
                # Stable iteration_id: hash(sym) is per-process random with PYTHONHASHSEED
                # (record ids change on restart). sha1 is deterministic.
                "iteration_id": int(hashlib.sha1(sym.encode()).hexdigest(), 16) % 10000,
                "rationale": f"multi-symbol · {sym}",
            }
            for sym in symbols
        ]
        payloads = run_many(units, **_run_many_kw(run_many))
        for sym in symbols:
            p = payloads.get(f"ms:{sym}")
            err = p.get("error") if p else "no result"
            if err == "no data":
                _p(f"  [{sym}] ⚠ No data — skipping")
                results.append(
                    {
                        "symbol": sym,
                        "pnl": None,
                        "sharpe": None,
                        "n_trades": 0,
                        "error": "no data",
                    }
                )
                continue
            if err:
                _p(f"  [{sym}] ✗ Error: {err}")
                results.append(
                    {
                        "symbol": sym,
                        "pnl": None,
                        "sharpe": None,
                        "n_trades": 0,
                        "error": str(err),
                    }
                )
                continue
            m = p.get("metrics") or {}
            pnl = m.get("pnl", 0.0) or 0.0
            sharpe = m.get("sharpe")
            n_trades = m.get("n_trades", 0) or 0
            pnl_pct = pnl / STARTING_CASH * 100
            icon = "✓" if pnl > 0 else "✗"
            sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "—"
            _p(
                f"  [{sym}] {icon} PnL={pnl:+.2f} {cur} ({pnl_pct:+.1f}%) · "
                f"Sharpe={sharpe_str} · {n_trades} trade"
            )
            results.append(
                {
                    "symbol": sym,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "sharpe": round(sharpe, 2) if sharpe is not None else None,
                    "n_trades": n_trades,
                    "error": None,
                }
            )
    else:
        for sym in symbols:
            _p(f"  [{sym}] Loading data…")
            try:
                if source == "external":
                    from backtest import _make_external_bar_type
                    from data import external_instrument_object, load_external_bars

                    df = load_external_bars(sym, interval)
                    if not df.empty:
                        # Cut the window relative to the data's own end — the catalog
                        # does not run up to "now", so a now()-based slice would be empty.
                        df = df[df.index >= df.index[-1] - timedelta(days=days)]
                else:
                    df = load_bybit_bars(
                        symbol=sym,
                        interval=interval,
                        category=category,
                        start=start_dt,
                        end=end_dt,
                    )
                if df.empty:
                    _p(f"  [{sym}] ⚠ No data — skipping")
                    results.append(
                        {
                            "symbol": sym,
                            "pnl": None,
                            "sharpe": None,
                            "n_trades": 0,
                            "error": "no data",
                        }
                    )
                    continue

                _p(f"  [{sym}] {len(df):,} bar · backtest running…")
                if source == "external":
                    instr = external_instrument_object(sym)
                    if instr is None:
                        raise ValueError(f"instrument {sym} not in external catalog")
                    bt = _make_external_bar_type(instr.id, interval)
                else:
                    instr = _make_bybit_instrument(
                        symbol=sym,
                        base=sym[:-4] if sym.upper().endswith("USDT") else sym[:3],
                    )
                    bt = _make_bybit_bar_type(instr.id, interval)
                r = run_composed_backtest(
                    spec,
                    df,
                    iteration_id=hash(sym) % 10000,
                    rationale=f"multi-symbol · {sym}",
                    instrument=instr,
                    bar_type=bt,
                    venue=instr.id.venue,
                )
                if r.error:
                    # Parity/honesty: the parallel branch produces a '✗ Error' line
                    # + pnl=None on an in-band error; the sequential branch should
                    # follow the same format too (instead of the misleading
                    # '✗ PnL=+0.00 · 0 trade' success line).
                    _p(f"  [{sym}] ✗ Error: {r.error}")
                    results.append(
                        {
                            "symbol": sym,
                            "pnl": None,
                            "sharpe": None,
                            "n_trades": 0,
                            "error": str(r.error),
                        }
                    )
                    continue
                m = r.metrics
                pnl = m.get("pnl", 0.0) or 0.0
                sharpe = m.get("sharpe")
                n_trades = m.get("n_trades", 0) or 0
                pnl_pct = pnl / STARTING_CASH * 100

                icon = "✓" if pnl > 0 else "✗"
                sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "—"
                _p(
                    f"  [{sym}] {icon} PnL={pnl:+.2f} {cur} ({pnl_pct:+.1f}%) · "
                    f"Sharpe={sharpe_str} · {n_trades} trade"
                )
                results.append(
                    {
                        "symbol": sym,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "sharpe": round(sharpe, 2) if sharpe is not None else None,
                        "n_trades": n_trades,
                        "error": r.error,
                    }
                )
            except Exception as e:
                _p(f"  [{sym}] ✗ Error: {e}")
                results.append(
                    {
                        "symbol": sym,
                        "pnl": None,
                        "sharpe": None,
                        "n_trades": 0,
                        "error": str(e),
                    }
                )

    # Results with enough trades
    valid = [
        r for r in results if not r.get("error") and (r.get("n_trades") or 0) >= 5
    ]  # 3→5
    positive = [r for r in valid if (r.get("pnl") or 0) > 0]
    n_valid = len(valid)
    n_positive = len(positive)
    pass_rate = n_positive / n_valid if n_valid > 0 else 0.0

    if n_valid == 0:
        label = "— (yetersiz veri)"
    elif pass_rate >= 0.7:
        label = "✓ Generalizable"
    elif pass_rate >= 0.4:
        label = "⚠ Limited"
    else:
        label = "✗ Symbol specific"

    sharpes = [r["sharpe"] for r in valid if r.get("sharpe") is not None]
    avg_sharpe = round(sum(sharpes) / len(sharpes), 2) if sharpes else None

    _p(
        f"Multi-symbol completed · {n_positive}/{n_valid} symbols positive · "
        f"pass_rate={pass_rate:.0%} · {label}"
    )

    return {
        "symbols_tested": len(symbols),
        "symbols_valid": n_valid,
        "symbols_positive": n_positive,
        "pass_rate": round(pass_rate, 2),
        "generalization_label": label,
        "avg_sharpe": avg_sharpe,
        "primary_symbol": primary_symbol,
        "results": results,
    }
