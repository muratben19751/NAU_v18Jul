"""Walk-forward optimizer (STUDIO_SPEC Phase 4).

INTEGRATION POINT for nautilus_web_app: replace StubWalkForwardOptimizer with
an adapter over your existing walk-forward optimizer. Contract:

    optimizer.run(defn: StrategyDefinition) -> list[OptResult]

Param addressing uses "<rule_id>.<param_name>" (or "risk.<field>") keys —
apply_result() below shows how results map back onto a StrategyDefinition and
must keep working whatever engine sits behind.

Wiki References
---------------
Bkz: [[strategy_studio]], [[backtesting_guide]]
"""
from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field

from .backtest import BacktestAdapter
from .compiler import compile_strategy
from .mutations import update_risk, update_rule_param
from .schema import OptimizeRange, StrategyDefinition

# INTEGRATION POINT: set to whatever your real optimizer can handle.
OPTIMIZER_MAX_RUNS = 20_000
# Stub-only cap on evaluated combinations (evenly sampled from the grid).
STUB_MAX_EVALS = 400


@dataclass
class OptResult:
    rank: int
    params: dict[str, float]      # "<rule_id>.<name>" -> value
    dsr: float
    sharpe: float
    net_pnl_pct: float
    max_dd_pct: float
    trades: int

    def to_dict(self) -> dict:
        return asdict(self)


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


@dataclass
class StubWalkForwardOptimizer:
    adapter: BacktestAdapter
    top_n: int = 10
    _evals: int = field(default=0, init=False)

    def run(self, defn: StrategyDefinition) -> list[OptResult]:
        axes = axes_for(defn)
        if not axes:
            return []
        keys = [k for k, _ in axes]
        grid = list(itertools.product(*[vals for _, vals in axes]))
        if len(grid) > STUB_MAX_EVALS:  # even sampling, keeps corners
            step = len(grid) / STUB_MAX_EVALS
            grid = [grid[int(i * step)] for i in range(STUB_MAX_EVALS)]

        objective = defn.walkforward.objective
        scored = []
        self._evals = 0
        for combo in grid:
            trial = defn.model_copy(deep=True)
            apply_params(trial, dict(zip(keys, combo)))
            metrics = self.adapter.run(compile_strategy(trial))
            self._evals += 1
            scored.append((dict(zip(keys, combo)), metrics))

        def score(item):
            _p, m = item
            if objective == "sharpe":
                return m.sharpe
            if objective == "max_dd":
                return m.max_dd_pct  # less negative = better
            return m.dsr

        scored.sort(key=score, reverse=True)
        return [
            OptResult(rank=i + 1, params=p, dsr=m.dsr, sharpe=m.sharpe,
                      net_pnl_pct=m.net_pnl_pct, max_dd_pct=m.max_dd_pct,
                      trades=m.trades)
            for i, (p, m) in enumerate(scored[: self.top_n])
        ]
