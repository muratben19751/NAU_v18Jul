"""Backtest adapter (STUDIO_SPEC Phase 3).

INTEGRATION POINT for nautilus_web_app — this is the ONLY file to touch when
wiring the real engine. Implement:

    class NautilusBacktestAdapter:
        def run(self, compiled: CompiledStrategy) -> BacktestMetrics: ...

using your existing NautilusTrader runner (compile via to_nautilus(compiled)),
then swap the adapter in app/main.py:  ADAPTER = NautilusBacktestAdapter().

StubBacktestAdapter below is deterministic: metrics derive from a hash of the
compiled config, so changing any parameter changes the results — good enough
to exercise the full UI loop (run -> poll -> metrics -> equity curve) and for
tests, useless for real decisions.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .compiler import CompiledStrategy


@dataclass
class FoldMetrics:
    fold: int
    dsr: float
    sharpe: float
    max_dd_pct: float


@dataclass
class BacktestMetrics:
    net_pnl_pct: float
    sharpe: float
    dsr: float
    max_dd_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    equity_curve: list[float] = field(default_factory=list)  # normalized, 1.0 start
    folds: list[FoldMetrics] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> BacktestMetrics:
        d = json.loads(raw)
        d["folds"] = [FoldMetrics(**f) for f in d.get("folds", [])]
        return cls(**d)


class BacktestAdapter(Protocol):
    def run(self, compiled: CompiledStrategy) -> BacktestMetrics: ...


class StubBacktestAdapter:
    """Deterministic fake engine. See module docstring."""

    N_BARS = 220  # equity curve points (fits the 260px sparkline budget)

    def _seed(self, compiled: CompiledStrategy) -> int:
        payload = json.dumps({
            "instruments": compiled.instruments,
            "entry": [(c.indicator, c.params, c.operator, c.target)
                      for c in compiled.entry.conditions],
            "filters": [(c.indicator, c.params, c.operator, c.target)
                        for c in compiled.entry.filters],
            "exit": [(c.indicator, c.params, c.operator, c.target)
                     for c in compiled.exit.conditions],
            "risk": compiled.risk,
            "regime": bool(compiled.regime),
            "regime_else": (compiled.regime or {}).get("else"),
            "sub_entry": [
                (c.indicator, c.params, c.operator, c.target)
                for c in compiled.regime["else_strategy"]["entry"].conditions
            ] if compiled.regime and compiled.regime.get("else_strategy")
            else [],
            "allocation": compiled.allocation,
        }, sort_keys=True, default=str)
        return int(hashlib.sha256(payload.encode()).hexdigest()[:12], 16)

    def run(self, compiled: CompiledStrategy) -> BacktestMetrics:
        rng = random.Random(self._seed(compiled))

        # plausible-looking drifted random walk
        drift = rng.uniform(-0.0004, 0.0016)
        vol = rng.uniform(0.004, 0.011)
        equity, peak, max_dd = [1.0], 1.0, 0.0
        for _ in range(self.N_BARS - 1):
            equity.append(equity[-1] * (1 + rng.gauss(drift, vol)))
            peak = max(peak, equity[-1])
            max_dd = max(max_dd, (peak - equity[-1]) / peak)

        net = (equity[-1] - 1) * 100
        rets = [equity[i + 1] / equity[i] - 1 for i in range(len(equity) - 1)]
        mean = sum(rets) / len(rets)
        std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) or 1e-9
        sharpe = mean / std * math.sqrt(252)
        dsr = max(0.0, min(0.99, sharpe / 2 + rng.uniform(-0.12, 0.08)))

        folds = compiled.walkforward.get("folds", 6)
        fold_metrics = [
            FoldMetrics(fold=i + 1,
                        dsr=round(max(0.0, dsr + rng.uniform(-0.15, 0.1)), 2),
                        sharpe=round(sharpe + rng.uniform(-0.4, 0.3), 2),
                        max_dd_pct=round(-(max_dd * 100) + rng.uniform(-2, 2), 1))
            for i in range(folds)
        ]

        return BacktestMetrics(
            net_pnl_pct=round(net, 1),
            sharpe=round(sharpe, 2),
            dsr=round(dsr, 2),
            max_dd_pct=round(-max_dd * 100, 1),
            trades=rng.randint(120, 600),
            win_rate_pct=round(rng.uniform(38, 58), 1),
            profit_factor=round(rng.uniform(0.9, 1.9), 2),
            equity_curve=[round(x, 5) for x in equity],
            folds=fold_metrics,
        )
