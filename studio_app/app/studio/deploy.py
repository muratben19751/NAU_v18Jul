"""Deployment (STUDIO_SPEC Phase 6).

INTEGRATION POINT for nautilus_web_app: implement `launch(artifact)` against
your live/sim NautilusTrader runner (TradingNode config). Until then this
module compiles the SAVED version, enforces the deployment gate server-side,
and persists a deployment record + compiled artifact for the runner to pick
up. The kill switch is passed through in the artifact; if your runner lacks
native support, add a monitor task that pauses the deployment when realized
daily PnL breaches `kill_switch_daily_pct`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .backtest import BacktestMetrics
from .compiler import CompiledStrategy, compile_strategy
from .schema import StrategyDefinition

DEFAULT_GATE_DSR = 0.8


class DeployBlocked(Exception):
    pass


@dataclass
class DeployConfig:
    environment: str                  # paper | live
    instruments: str                  # active | all
    capital: float
    kill_switch_daily_pct: float | None
    gate_enabled: bool
    gate_min_objective: float


def check_gate(defn: StrategyDefinition,
               latest_metrics: BacktestMetrics | None,
               cfg: DeployConfig) -> None:
    """Server-side deployment gate — not just UI."""
    if not cfg.gate_enabled:
        return
    if latest_metrics is None:
        raise DeployBlocked(
            "deployment gate: no completed walk-forward run to evaluate")
    objective = defn.walkforward.objective
    value = {"sharpe": latest_metrics.sharpe,
             "max_dd": latest_metrics.max_dd_pct}.get(
        objective, latest_metrics.dsr)
    if value < cfg.gate_min_objective:
        raise DeployBlocked(
            f"deployment gate: OOS {objective.upper()} {value:.2f} "
            f"below required {cfg.gate_min_objective:.2f}")


def build_artifact(defn: StrategyDefinition, compiled: CompiledStrategy,
                   cfg: DeployConfig) -> str:
    """The JSON document a runner consumes. INTEGRATION POINT: feed this to
    to_nautilus()/your TradingNode bootstrap."""
    return json.dumps({
        "strategy_id": defn.id,
        "version": defn.version,
        "environment": cfg.environment,
        "capital": cfg.capital,
        "kill_switch_daily_pct": cfg.kill_switch_daily_pct,
        "instruments": compiled.instruments,
        "allocation": compiled.allocation,
        "risk": compiled.risk,
        "compiled": {
            "entry_conditions": len(compiled.entry.conditions),
            "exit_conditions": len(compiled.exit.conditions),
            "has_regime": compiled.regime is not None,
            "regime_else": (compiled.regime or {}).get("else"),
        },
        "config": asdict(cfg),
    }, indent=2)


def prepare_deployment(defn: StrategyDefinition,
                       latest_metrics: BacktestMetrics | None,
                       cfg: DeployConfig) -> str:
    if cfg.environment not in ("paper", "live"):
        raise DeployBlocked(f"unknown environment '{cfg.environment}'")
    check_gate(defn, latest_metrics, cfg)
    compiled = compile_strategy(defn)   # SAVED version, never the draft
    return build_artifact(defn, compiled, cfg)
