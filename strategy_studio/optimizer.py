"""Walk-forward optimizer (STUDIO_SPEC Phase 4).

Contract (unchanged, whatever engine sits behind):

    optimizer.run(defn: StrategyDefinition) -> list[OptResult]

Param addressing is ``"<rule_id>.<param_name>"`` (or ``"risk.<field>"``);
``apply_params`` below writes a result back onto a StrategyDefinition and the
Apply button depends on that spelling.

What "walk-forward" means here
------------------------------
Each candidate is scored on data it was not selected on:

1. **Anchored in-sample screen.** One run over the leading
   ``in_sample_months`` share of the sample. A candidate that cannot trade
   (``min_trades``) or scores ``-inf`` there never reaches the folds — this is
   the cheap filter that keeps the expensive stage small.
2. **Purged walk-forward folds.** ``walkforward.folds`` consecutive
   out-of-sample windows of ``oos_months`` each, laid end to end after the
   in-sample anchor, every one purged by ``embargo_bars`` at its front.
   Ranking uses ``mean − 0.5·std`` of the per-fold objective — the host app's
   ``wfo_optimizer.penalized_score`` convention — so a candidate that shines in
   one fold and collapses in the rest loses to a steady one.

The months are read as a *ratio*, not as calendar lengths: they split whatever
sample the adapter loads (``NautilusBacktestAdapter.lookback_days``, 180 by
default) into 1 in-sample plus ``folds`` out-of-sample windows. Raise the
lookback to make them literal. The single-run fold table is a different thing —
purged k-fold over the run's own sample — and is left alone.

Why not the host's ``wfo_optimizer``
------------------------------------
That one is a GA over a param space derived from ``BLOCK_REGISTRY`` bounds. The
studio's search space is the min/step/max the user typed into the rule tree, so
a GA over registry bounds would silently optimize outside the ranges on screen.
The conventions worth sharing are shared instead: ``penalized_score``, the
valid-fold fraction, and the trade-count confidence damping.

Wiki References
---------------
Bkz: [[strategy_studio]], [[backtesting_guide]]
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from statistics import NormalDist

from .backtest import BacktestAdapter, BacktestMetrics, Window, probabilistic_sharpe
from .compiler import CompileError, compile_strategy
from .mutations import MutationError, update_risk, update_rule_param
from .schema import OptimizeRange, StrategyDefinition

# Ceiling on the *declared* sweep (combinations × folds); the route refuses
# above this before starting a background run.
OPTIMIZER_MAX_RUNS = 20_000
# Combinations actually evaluated (evenly sampled from the grid when larger).
MAX_EVALS = 400
# Legacy spelling kept for one release: the cap was stub-only when it landed.
STUB_MAX_EVALS = MAX_EVALS

# A fold with fewer trades than this says nothing — the classic overfit trap is
# 1 trade with a huge Sharpe. Same threshold the host optimizer uses.
MIN_TRADES = 5
# Shared with wfo_optimizer: score *= n / (n + K). K=20 → ×0.5 at 20 trades.
TRADE_CONF_K = 20
# Shared with wfo_optimizer: a candidate survives on 60% valid folds, so one
# dead fold does not reject an otherwise robust sparse-trade candidate.
MIN_VALID_FOLDS_FRAC = 0.6

_EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist()


class NoViableCandidates(Exception):
    """Every candidate was rejected — carries the tally of why.

    Raised rather than returning an empty list: "0 results" on the panel is
    indistinguishable from "the optimizer never ran", and the reason (all
    screened out in-sample? every fold under min_trades?) is the actionable
    part.
    """


@dataclass
class OptResult:
    rank: int
    params: dict[str, float]  # "<rule_id>.<name>" -> value
    dsr: float
    sharpe: float
    net_pnl_pct: float
    max_dd_pct: float
    trades: int
    # Added after the stub: defaulted so opt runs stored by an older build
    # still rehydrate through OptResult(**row).
    score: float = 0.0  # penalized walk-forward objective (ranking key)
    folds_valid: int = 0  # out-of-sample folds that produced a score
    trials: int = 0  # candidates evaluated — the DSR deflation N

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def walk_forward(self) -> bool:
        """False for a run stored before the walk-forward optimizer landed.

        Those rows rehydrate with `score`/`folds_valid`/`trials` at zero, and
        rendering them in the walk-forward layout reads as a broken panel
        ("DSR 0.000 · 0 folds · 0 trials") rather than as an old result. The
        panel branches on this and labels them instead.
        """
        return self.trials > 0


def _axis_values(r: OptimizeRange) -> list[float]:
    out, v = [], r.min
    for _ in range(r.n_values):
        out.append(round(v, 10))
        v += r.step
    return out


def axes_for(defn: StrategyDefinition) -> list[tuple[str, list[float]]]:
    axes = []
    for block, rule_id, name, p in defn.optimized_params():
        key = f"risk.{name}" if block == "risk" else f"{rule_id}.{name}"
        axes.append((key, _axis_values(p.optimize)))  # type: ignore[arg-type]
    return axes


def apply_params(defn: StrategyDefinition, params: dict[str, float]) -> None:
    """Write a result's params back onto a StrategyDefinition (in place)."""
    for key, value in params.items():
        owner, name = key.split(".", 1)
        if owner == "risk":
            update_risk(defn, name, str(value))
        else:
            update_rule_param(defn, owner, name, str(value))


# ── walk-forward geometry ───────────────────────────────────────────────────


def windows_for(wf) -> tuple[Window | None, list[Window]]:
    """(in-sample screen, out-of-sample folds) as fractions of the sample.

    Anchored: the in-sample window starts at the first bar and the folds walk
    forward from where it ends. ``in_sample_months=0`` drops the screen (every
    candidate goes straight to the folds).

    ``scheme`` has one value today (``anchored_purged_kfold``); a second scheme
    would branch here, on the geometry, and nowhere else.
    """
    folds = max(1, int(wf.folds))
    is_months = max(0, int(wf.in_sample_months))
    oos_months = max(1, int(wf.oos_months))
    unit = is_months + folds * oos_months
    is_frac = is_months / unit
    oos_frac = oos_months / unit
    embargo = max(0, int(wf.embargo_bars or 0))

    screen = Window(0.0, is_frac, label="in-sample") if is_frac > 0 else None
    oos = [
        Window(
            start=is_frac + i * oos_frac,
            # The last fold takes the remainder so rounding never leaves a
            # sliver of the sample unevaluated.
            end=1.0 if i == folds - 1 else is_frac + (i + 1) * oos_frac,
            embargo_bars=embargo,
            label=f"oos{i + 1}",
        )
        for i in range(folds)
    ]
    return screen, oos


# ── scoring ─────────────────────────────────────────────────────────────────


def fold_objective(m: BacktestMetrics, objective: str) -> float:
    """One fold's contribution to the ranking score.

    Trade-count damping (``n / (n + K)``) is applied to ``dsr``/``sharpe``,
    where 0 is the neutral floor and a small trade count means an inflated
    number. It is deliberately *not* applied to ``max_dd``: that objective is a
    negative number where higher is better, so damping would pull a thin,
    lightly-traded candidate towards 0 and reward it for having no history.
    """
    if objective == "sharpe":
        val, damp = m.sharpe, True
    elif objective == "max_dd":
        val, damp = m.max_dd_pct, False
    else:  # dsr, and 'custom' until a custom objective exists
        val, damp = m.dsr, True
    if not math.isfinite(val):
        return float("-inf")
    if damp and m.trades + TRADE_CONF_K > 0:
        val *= m.trades / (m.trades + TRADE_CONF_K)
    return float(val)


def penalized_score(fold_objs: list[float]) -> float:
    """mean − 0.5·std (host convention): one brilliant fold is not enough."""
    if not fold_objs:
        return float("-inf")
    n = len(fold_objs)
    mean = sum(fold_objs) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in fold_objs) / n)
    return mean - 0.5 * std


def expected_max_sharpe(sigma_sr: float, n_trials: int) -> float:
    """Highest Sharpe a search of ``n_trials`` produces on skill-free data.

    Bailey & López de Prado's benchmark for the Deflated Sharpe Ratio:
    ``σ·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]``. Search 400 combinations and the
    best one is expected to clear ~3σ of the trial spread from nothing but
    luck; DSR asks whether the winner beat *that*, not whether it beat zero.
    """
    if n_trials < 2 or sigma_sr <= 0 or not math.isfinite(sigma_sr):
        return 0.0
    a = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
    b = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sigma_sr * ((1.0 - _EULER_GAMMA) * a + _EULER_GAMMA * b)


def _stitch(fold_metrics: list[BacktestMetrics]) -> tuple[list[float], list[float]]:
    """Out-of-sample folds → (one continuous curve, its step returns).

    Returns rather than levels are concatenated: each fold's curve restarts at
    1.0, so splicing the levels would book a fake jump at every boundary.
    """
    rets: list[float] = []
    for m in fold_metrics:
        c = m.equity_curve
        rets += [c[i + 1] / c[i] - 1 for i in range(len(c) - 1) if c[i]]
    curve = [1.0]
    for r in rets:
        curve.append(curve[-1] * (1 + r))
    return curve, rets


def _max_dd_pct(curve: list[float]) -> float:
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return -dd * 100


def _step_sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    return mean / sd if sd > 0 else 0.0


# ── the optimizer ───────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    params: dict[str, float]
    folds: list[BacktestMetrics]
    score: float
    step_sharpe: float
    returns: list[float]
    curve: list[float]


@dataclass
class WalkForwardOptimizer:
    """Grid search scored out-of-sample, ranked penalized, reported deflated."""

    adapter: BacktestAdapter
    top_n: int = 10
    max_evals: int = MAX_EVALS
    min_trades: int = MIN_TRADES
    _evals: int = field(default=0, init=False)
    last_run: dict = field(default_factory=dict, init=False)

    # ── grid ────────────────────────────────────────────────────────────
    def _grid(self, defn: StrategyDefinition) -> list[dict[str, float]]:
        axes = axes_for(defn)
        if not axes:
            return []
        keys = [k for k, _ in axes]
        combos = list(itertools.product(*[vals for _, vals in axes]))
        if len(combos) > self.max_evals:  # even sampling, keeps the corners
            step = len(combos) / self.max_evals
            combos = [combos[int(i * step)] for i in range(self.max_evals)]
        return [dict(zip(keys, c, strict=True)) for c in combos]

    # ── stages ──────────────────────────────────────────────────────────
    def _compile(self, defn: StrategyDefinition, params: dict[str, float]):
        trial = defn.model_copy(deep=True)
        apply_params(trial, params)
        return compile_strategy(trial)

    def run(self, defn: StrategyDefinition) -> list[OptResult]:
        grid = self._grid(defn)
        if not grid:
            return []
        screen, oos_windows = windows_for(defn.walkforward)
        objective = defn.walkforward.objective
        self._evals = 0
        tally = {"invalid": 0, "screened_out": 0, "thin_folds": 0, "engine_error": 0}

        # Stage 1 — anchored in-sample screen.
        viable: list[tuple[dict[str, float], object]] = []
        for params in grid:
            try:
                compiled = self._compile(defn, params)
            except (CompileError, MutationError):
                # A grid corner the compiler rejects (e.g. fast >= slow). Not a
                # failure of the sweep, but it is counted, never silent.
                tally["invalid"] += 1
                continue
            if screen is None:
                viable.append((params, compiled))
                continue
            try:
                m = self.adapter.run(compiled, window=screen)
                self._evals += 1
            except Exception:  # noqa: BLE001 — one dead candidate, not a dead sweep
                tally["engine_error"] += 1
                continue
            if m.trades < self.min_trades or fold_objective(m, objective) == float(
                "-inf"
            ):
                tally["screened_out"] += 1
                continue
            viable.append((params, compiled))

        # Stage 2 — purged walk-forward folds.
        min_valid = MIN_VALID_FOLDS_FRAC * len(oos_windows)
        cands: list[_Candidate] = []
        for params, compiled in viable:
            fold_metrics, objs = [], []
            for w in oos_windows:
                try:
                    m = self.adapter.run(compiled, window=w)
                    self._evals += 1
                except Exception:  # noqa: BLE001 — a dead fold tells us nothing
                    continue
                if m.trades < self.min_trades:
                    continue
                obj = fold_objective(m, objective)
                if obj == float("-inf"):
                    continue
                fold_metrics.append(m)
                objs.append(obj)
            if not objs or len(objs) < min_valid:
                tally["thin_folds"] += 1
                continue
            curve, rets = _stitch(fold_metrics)
            cands.append(
                _Candidate(
                    params=params,
                    folds=fold_metrics,
                    score=penalized_score(objs),
                    step_sharpe=_step_sharpe(rets),
                    returns=rets,
                    curve=curve,
                )
            )

        self.last_run = {
            "grid": len(grid),
            "viable": len(viable),
            "scored": len(cands),
            "engine_runs": self._evals,
            **tally,
        }
        if not cands:
            raise NoViableCandidates(
                f"no candidate survived the walk-forward screen: {len(grid)} "
                f"combinations → {tally['invalid']} uncompilable, "
                f"{tally['engine_error']} engine errors, "
                f"{tally['screened_out']} below {self.min_trades} in-sample "
                f"trades, {tally['thin_folds']} without "
                f"{min_valid:.0f}/{len(oos_windows)} usable folds"
            )

        # Deflation: the benchmark is what this many trials would have thrown
        # up by luck, so it is a property of the sweep, not of one candidate.
        # N counts every combination tried (that is the multiple testing), while
        # the spread can only be measured over the candidates that produced a
        # Sharpe. A single scored candidate has no spread and therefore no
        # deflation — its `dsr` falls back to the undeflated PSR.
        trials = len(grid)
        sharpes = [c.step_sharpe for c in cands]
        mean_sr = sum(sharpes) / len(sharpes)
        sigma_sr = math.sqrt(sum((s - mean_sr) ** 2 for s in sharpes) / len(sharpes))
        sr_star = expected_max_sharpe(sigma_sr, trials)

        cands.sort(key=lambda c: c.score, reverse=True)
        results = []
        for i, c in enumerate(cands[: self.top_n]):
            fold_sharpes = [m.sharpe for m in c.folds if math.isfinite(m.sharpe)]
            results.append(
                OptResult(
                    rank=i + 1,
                    params=c.params,
                    dsr=probabilistic_sharpe(c.returns, c.step_sharpe, sr_star),
                    sharpe=round(
                        sum(fold_sharpes) / len(fold_sharpes) if fold_sharpes else 0.0,
                        2,
                    ),
                    net_pnl_pct=round((c.curve[-1] - 1) * 100, 2),
                    max_dd_pct=round(_max_dd_pct(c.curve), 2),
                    trades=sum(m.trades for m in c.folds),
                    score=round(c.score, 4),
                    folds_valid=len(c.folds),
                    trials=trials,
                )
            )
        return results
