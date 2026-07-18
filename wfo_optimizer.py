"""Walk-Forward Optimization — train-only parameter search (lightweight GA).

Real WFO optimizes strategy parameters on the in-sample (train) window and
applies the *chosen* parameters to the out-of-sample (test) window. This
module provides the missing pieces:

  - ``build_param_space``  : which numeric knobs are tunable, and their ranges
    (``BLOCK_REGISTRY[...]["meta"]["params"]`` + strategy-level fields).
  - ``mutate_spec``        : produces a NEW spec applying an assignment
    (deep-copy; the caller's spec is never modified).
  - ``optimize_window``    : lightweight genetic search (GA) on train candles.
    Generation 0 = current spec values + random individuals; subsequent
    generations = elitism(1) + tournament(k=3) selection + uniform crossover +
    per-dimension gaussian mutation. Each candidate is run k-fold (embargoed);
    score = mean − 0.5·std (NAU pattern). Fully deterministic via seeded
    ``np.random.default_rng`` (same seed → same winner).

Only NUMERIC parameters are optimized. Enums (direction/cross/side,
entry/exit logic, order/sl/tp type, sizing mode, trend_filter) define *what the
strategy is* — sweeping them becomes model selection, not calibration; it also
explodes the search space.

Cost limit
----------
Total backtest count per window = POP × GEN × FOLD.

  - POP  = ``WFO_POP_SIZE``  (default 8,  env: ``NAUTILUS_WFO_POP_SIZE``)
  - GEN  = max(1, round(n_samples / POP))  — ``n_samples`` comes from the caller
    (``n_optimize`` in WFO); e.g. n_samples=20 → GEN=3 → 8×3 = 24 candidates.
  - FOLD = ``WF_FOLDS``      (default 3,  env: ``NAUTILUS_WF_FOLDS``)

Example: n_samples=20 with defaults → 24 candidates × 3 folds = 72 eval/window.
To reduce the budget, lower the env constants (e.g. POP=4, FOLD=2) or
shrink ``n_optimize``.

Metric warning (M35)
--------------------
``objective`` runs on the Engine path's BAR-FREQUENCY Sharpe; for the cross
runner (BacktestNode) or NAU comparison, use ``sharpe_nautilus`` /
``sharpe_per_trade`` — the scales differ and are not directly comparable.
"""

from __future__ import annotations

import math
import os

import numpy as np


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ── GA / fold budget (env-overridable module constants) ─────────────────────
# Cost per window = WFO_POP_SIZE × GEN × WF_FOLDS backtests (see module
# docstring). Tests may monkeypatch these constants; functions read the
# values from the module global at call time.
WFO_POP_SIZE = max(1, _env_int("NAUTILUS_WFO_POP_SIZE", 8))
WF_FOLDS = max(1, _env_int("NAUTILUS_WF_FOLDS", 3))
# NAU confidence-damping constant: score *= n / (n + K). K=20 → ×0.2 at 5 trades,
# ×0.5 at 20 trades, ×0.83 at 100 trades — scores inflated by few trades are suppressed.
WFO_TRADE_CONF_K = max(0, _env_int("NAUTILUS_WFO_TRADE_CONF_K", 20))

# M236: NAU calmar guards. DD_FLOOR (fraction; NAU 1% = 0.01) prevents micro-drawdown
# inflation; CALMAR_CAP clips calmar to ±10. STARTING_CASH is the fallback base in
# pnl_pct derivation.
_DD_FLOOR = 0.01
_CALMAR_CAP = 10.0
try:
    from app_constants import STARTING_CASH as _STARTING_CASH
except Exception:  # pragma: no cover
    _STARTING_CASH = 10_000.0

# M462: NAU valid-fold fraction — if 2 of 3 folds are valid (>=0.6) the candidate
# survives; a SINGLE -inf fold should not reject the candidate outright (preserves
# robust sparse-trade candidates).
WF_MIN_VALID_FOLDS_FRAC = _env_float("NAUTILUS_WF_MIN_VALID_FOLDS_FRAC", 0.6)

# GA internal settings (behavior constants; determinism comes from rng).
GA_TOURNAMENT_K = 3
GA_MUT_SIGMA = 0.15  # gaussian mutation std, as a fraction of the dimension range


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


# Strategy-level numeric knobs, each gated on the feature that activates it.
# (field, type, lo, hi, is_active(spec))
_SPEC_DIMS = [
    ("sl_value", "float", 0.5, 5.0, lambda s: bool(getattr(s, "use_bracket", False))),
    (
        "tp_value",
        "float",
        1.0,
        10.0,
        lambda s: (
            bool(getattr(s, "use_bracket", False))
            and getattr(s, "tp_type", "off") != "off"
        ),
    ),
    (
        "atr_period",
        "int",
        7,
        30,
        lambda s: (
            getattr(s, "sl_type", "") == "atr"
            or getattr(s, "tp_type", "") == "atr"
            or getattr(s, "trade_size_mode", "") == "atr_target"
        ),
    ),
    (
        "trend_ema_period",
        "int",
        20,
        200,
        lambda s: bool(getattr(s, "trend_filter", False)),
    ),
]


def build_param_space(spec) -> list[dict]:
    """Return the list of tunable numeric dimensions for ``spec``.

    Each dim: ``{target, type, lo, hi, default, label}`` where ``target`` is
    ``("block", i, key)`` or ``("spec", field)``.
    """
    from composer import BLOCK_REGISTRY

    space: list[dict] = []
    for i, block in enumerate(spec.blocks):
        reg = BLOCK_REGISTRY.get(block.type) or {}
        params_meta = (reg.get("meta") or {}).get("params", {})
        for key, meta in params_meta.items():
            ptype = meta.get("type")
            if ptype not in ("int", "float"):
                continue  # skip enums / non-numeric
            lo, hi = meta.get("min"), meta.get("max")
            if lo is None or hi is None or hi <= lo:
                continue
            space.append(
                {
                    "target": ("block", i, key),
                    "type": ptype,
                    "lo": float(lo),
                    "hi": float(hi),
                    "default": meta.get("default", lo),
                    "label": f"{block.type}[{i}].{key}",
                }
            )

    for field, ptype, lo, hi, is_active in _SPEC_DIMS:
        if is_active(spec):
            space.append(
                {
                    "target": ("spec", field),
                    "type": ptype,
                    "lo": float(lo),
                    "hi": float(hi),
                    "default": getattr(spec, field, lo),
                    "label": field,
                }
            )
    return space


def _current_values(spec, space) -> list:
    vals = []
    for dim in space:
        tgt = dim["target"]
        if tgt[0] == "block":
            _, i, key = tgt
            vals.append(spec.blocks[i].params.get(key, dim["default"]))
        else:
            vals.append(getattr(spec, tgt[1], dim["default"]))
    return vals


def _sample_dim(rng, dim):
    if dim["type"] == "int":
        return int(rng.integers(int(dim["lo"]), int(dim["hi"]) + 1))
    return float(rng.uniform(dim["lo"], dim["hi"]))


def mutate_spec(spec, space, values):
    """Return a NEW spec with ``values`` (aligned to ``space``) applied.

    Deep-copies via to_dict/from_dict so the caller's spec is never touched.
    """
    from composer import ComposedStrategySpec

    new = ComposedStrategySpec.from_dict(spec.to_dict())
    for dim, v in zip(space, values):
        if dim["type"] == "int":
            v = int(round(float(v)))
        else:
            v = float(v)
        tgt = dim["target"]
        if tgt[0] == "block":
            _, i, key = tgt
            new.blocks[i].params[key] = v
        else:
            setattr(new, tgt[1], v)
    return new


def values_to_dict(space, values) -> dict:
    """Human-readable {label: value} for reporting chosen params."""
    out = {}
    for dim, v in zip(space, values):
        out[dim["label"]] = (
            int(round(float(v))) if dim["type"] == "int" else round(float(v), 4)
        )
    return out


def objective_value(result, objective: str = "sharpe", min_trades: int = 5) -> float:
    """Scores a backtest result. Errored / low-trade runs return -inf —
    so the overfit trap that takes 0-1 trades and reports a huge/NaN Sharpe can
    never win (hard threshold: ``n < min_trades`` → -inf, default 5).

    Candidates that PASS the threshold get NAU-style trade-count confidence
    damping: ``val *= n / (n + WFO_TRADE_CONF_K)`` — scores inflated by few trades
    (multiple-testing inflation) are suppressed, and the multiplier approaches 1
    as n grows. The same damping is applied to values in the NaN-fallback chain.
    """
    if result is None or getattr(result, "error", None):
        return float("-inf")
    m = result.metrics or {}
    n = m.get("n_trades", 0) or 0
    if n < min_trades:
        return float("-inf")
    conf = n / (n + WFO_TRADE_CONF_K) if (n + WFO_TRADE_CONF_K) > 0 else 1.0

    def _calmar() -> float | None:
        # M236: NAU DD_FLOOR (1%) + CALMAR_CAP (±10) guards — a fold with a
        # micro-drawdown (dd=-0.0001) would get an unbounded score and dominate
        # the GA tournament. Use pnl_pct (not absolute USDT — M234), abs for
        # the NEGATIVE-fraction max_dd (L41), floor 0.01 (=1%), clip result to ±10.
        pnl_pct = m.get("pnl_pct")
        if pnl_pct is None:
            pnl = m.get("pnl", 0.0) or 0.0
            pnl_pct = pnl / _STARTING_CASH
        dd = m.get("max_dd")
        if dd is None or _isnan(dd):
            return None
        dd_abs = max(abs(float(dd)), _DD_FLOOR)
        return max(-_CALMAR_CAP, min(_CALMAR_CAP, float(pnl_pct) / dd_abs))

    if objective == "sortino":
        val = m.get("sortino")
    elif objective in ("calmar", "return_dd"):
        val = _calmar()
    else:  # sharpe (default)
        # NAU parity: per-trade sharpe ((mean/std)×√n), NOT annualized 252-day.
        # NAU's fold_quality composite also reads per-trade sharpe. Backward-compat:
        # if sharpe_per_trade is missing (old/stub metrics) fall back to annualized 'sharpe'.
        val = m.get("sharpe_per_trade")
        if val is None:
            val = m.get("sharpe")

    if val is None or _isnan(val):
        # M240: the fallback chain must NOT MIX SCALES — sharpe/sortino are ~O(1)
        # while pnl/|dd| was ~O(1e4); a single degenerate fold (sharpe NaN) would
        # inflate the candidate's penalized score by orders of magnitude and corrupt
        # the GA winner. Fall back to NaN sortino (same scale); if that's missing too,
        # CAPPED calmar (±10, same order of magnitude) — NOT an unbounded raw ratio.
        alt = m.get("sortino")
        if alt is not None and not _isnan(alt):
            return float(alt) * conf
        cal = _calmar()
        if cal is not None:
            return cal * conf
        return float("-inf")
    return float(val) * conf


# ---------------------------------------------------------------------------
# Lightweight GA — population generation (H10)
# ---------------------------------------------------------------------------


def ga_plan(space, n_samples: int) -> tuple[int, int]:
    """(pop_size, n_generations) budget mapping.

    GEN = max(1, round(n_samples / POP)) — rounds half UP
    (n_samples=20, POP=8 → GEN=3). Searching an empty space is meaningless → (1, 1):
    a single (current) candidate is run, no wasteful old n_samples×identical-run.
    """
    if not space:
        return 1, 1
    pop = max(1, WFO_POP_SIZE)
    return pop, max(1, int(n_samples / pop + 0.5))


def ga_initial_population(spec, space, rng, pop_size: int) -> list[list]:
    """Generation 0: individual 0 = spec's CURRENT values, the rest random.

    rng stays in the parent (the parallel path does not leak randomness to
    workers); same rng state → same population (determinism contract).
    """
    population: list[list] = [_current_values(spec, space)]
    for _ in range(max(0, pop_size - 1)):
        population.append([_sample_dim(rng, dim) for dim in space])
    return population


def _tournament_idx(rng, scores: list[float], k: int = GA_TOURNAMENT_K) -> int:
    """Tournament selection: k random individuals; the highest score wins
    (ties broken by lower index — deterministic comparison)."""
    n = len(scores)
    idxs = rng.integers(0, n, size=min(k, n))
    best = int(idxs[0])
    for raw in idxs[1:]:
        i = int(raw)
        if scores[i] > scores[best] or (scores[i] == scores[best] and i < best):
            best = i
    return best


def ga_next_population(
    rng, space, population: list[list], scores: list[float], pop_size: int
) -> list[list]:
    """Next generation: elitism(1) + tournament(k=3) + uniform crossover +
    per-dimension gaussian mutation (round to int, clamp to [lo, hi]).

    The elite is chosen with a strict-greater rule (ties broken by lower index) —
    the sequential and parallel paths produce the same winner. All randomness
    comes from ``rng``.
    """
    elite_idx = max(range(len(population)), key=lambda i: (scores[i], -i))
    new_pop: list[list] = [list(population[elite_idx])]
    while len(new_pop) < pop_size:
        p1 = population[_tournament_idx(rng, scores)]
        p2 = population[_tournament_idx(rng, scores)]
        child: list = []
        for d, dim in enumerate(space):
            v = float(p1[d]) if rng.random() < 0.5 else float(p2[d])
            v += float(rng.normal(0.0, GA_MUT_SIGMA * (dim["hi"] - dim["lo"])))
            v = min(max(v, dim["lo"]), dim["hi"])
            child.append(int(round(v)) if dim["type"] == "int" else v)
        new_pop.append(child)
    return new_pop


# ---------------------------------------------------------------------------
# K-fold train evaluation (L35)
# ---------------------------------------------------------------------------


def build_fold_bounds(train_bars, n_folds=None, embargo_days=None) -> list[tuple]:
    """Splits train candles into k contiguous folds; puts an embargo (purge)
    gap between folds — so a trade opened at the end of one fold and closed in
    another does not leak.

    Returns: [(start, end), ...] — ``pd.Timestamp`` pairs, ``end`` EXCLUSIVE
    (the last fold also covers the last candle). It is a pure function of
    train_bars: the sequential and parallel paths produce the SAME bounds from
    the same DataFrame (parity contract).

    Fallbacks: train_bars None/empty → ``[]`` (the caller falls back to a
    single full-window run); if embargo eats the total duration or any fold ends
    up empty → single fold (whole window).
    """
    import pandas as pd

    if n_folds is None:
        n_folds = WF_FOLDS
    if embargo_days is None:
        from backtest_robustness import WF_EMBARGO_DAYS as embargo_days

    try:
        if train_bars is None or len(train_bars) == 0:
            return []
        idx = train_bars.index
        t0 = pd.Timestamp(idx[0])
        # end exclusive: last candle's timestamp + 1ns → last candle included.
        t_end = pd.Timestamp(idx[-1]) + pd.Timedelta(1, "ns")
    except (TypeError, AttributeError):
        return []

    if n_folds <= 1:
        return [(t0, t_end)]

    embargo = pd.Timedelta(days=float(embargo_days))
    inner = (t_end - t0) - embargo * (n_folds - 1)
    if inner <= pd.Timedelta(0):
        return [(t0, t_end)]  # window too short for embargoed k-fold

    seg = inner / n_folds
    bounds: list[tuple] = []
    cursor = t0
    for i in range(n_folds):
        fs = cursor
        fe = fs + seg if i < n_folds - 1 else t_end
        bounds.append((fs, fe))
        cursor = fe + embargo

    # If any fold ends up empty (data gaps) fall back to a single fold —
    # a backtest on an empty slice would drag every candidate to -inf.
    for fs, fe in bounds:
        if not ((idx >= fs) & (idx < fe)).any():
            return [(t0, t_end)]
    return bounds


def penalized_score(fold_objs: list[float]) -> float:
    """Reduces fold scores to a single score: mean − 0.5·std (NAU pattern) —
    a candidate that shines in only one fold is penalized. Empty list → -inf."""
    if not fold_objs:
        return float("-inf")
    arr = np.asarray(fold_objs, dtype=float)
    return float(arr.mean() - 0.5 * arr.std())


# ---------------------------------------------------------------------------
# Window optimization (sequential path)
# ---------------------------------------------------------------------------


def optimize_window(
    spec,
    train_bars,
    space,
    instrument,
    bar_type,
    venue,
    *,
    n_samples: int = 20,
    seed: int = 0,
    objective: str = "sharpe",
    min_trades: int = 5,
    run_fn=None,
    iteration_id: int = 0,
) -> dict:
    """Lightweight GA search on the TRAIN window (sequential path).

    Generation 0 = current spec values + (POP−1) random individuals; subsequent
    generations evolve via ``ga_next_population``. Each valid candidate is run
    over ``build_fold_bounds`` folds; if any fold is -inf the candidate is
    rejected, otherwise score = mean − 0.5·std. Invalid candidates (e.g.
    slow<=fast, caught by ``spec.validate()``) are not run (score -inf).

    Determinism: all randomness comes from ``np.random.default_rng(seed)`` —
    same seed + same data → same winner. Returns:
    ``{values, params, objective, spec, metrics, n_evaluated}``.

    Cost: see module docstring (POP × GEN × FOLD backtests/window).
    """
    if run_fn is None:
        from backtest import run_composed_backtest as run_fn  # type: ignore

    rng = np.random.default_rng(seed)
    pop_size, n_gen = ga_plan(space, n_samples)
    population = ga_initial_population(spec, space, rng, pop_size)
    fold_bounds = build_fold_bounds(train_bars)

    best = None
    evaluated = 0
    for gen in range(n_gen):
        scores: list[float] = []
        for values in population:
            cand = mutate_spec(spec, space, values)
            if cand.validate() is not None:
                scores.append(float("-inf"))
                continue
            _folds = fold_bounds or [None]
            fold_objs: list[float] = []
            last_metrics: dict = {}
            # M462: previously the candidate was rejected outright on the FIRST
            # -inf fold; with the NAU valid-fold fraction (>=0.6), if 2 of 3
            # folds are valid the candidate survives (preserves robust
            # sparse-trade candidates). Run ALL folds and collect the valid ones.
            for fb in _folds:
                fold_bars = (
                    train_bars
                    if fb is None
                    else train_bars.loc[
                        (train_bars.index >= fb[0]) & (train_bars.index < fb[1])
                    ]
                )
                res = run_fn(
                    cand,
                    fold_bars,
                    iteration_id=iteration_id,
                    rationale="WFO train-optimize",
                    instrument=instrument,
                    bar_type=bar_type,
                    venue=venue,
                )
                evaluated += 1
                obj = objective_value(res, objective, min_trades)
                if obj != float("-inf"):
                    fold_objs.append(obj)
                    last_metrics = res.metrics or {}
            score = (
                penalized_score(fold_objs)
                if len(fold_objs) >= WF_MIN_VALID_FOLDS_FRAC * len(_folds) and fold_objs
                else float("-inf")
            )
            scores.append(score)
            if score != float("-inf") and (best is None or score > best["objective"]):
                best = {
                    "values": values,
                    "params": values_to_dict(space, values),
                    "objective": score,
                    "spec": cand,
                    "metrics": last_metrics,
                }
        if gen < n_gen - 1:
            population = ga_next_population(rng, space, population, scores, pop_size)

    if best is None:
        # No candidate produced a valid score — fall back to the naive spec.
        cur = _current_values(spec, space)
        best = {
            "values": cur,
            "params": values_to_dict(space, cur),
            "objective": float("-inf"),
            "spec": spec,
            "metrics": {},
        }
    best["n_evaluated"] = evaluated
    return best
