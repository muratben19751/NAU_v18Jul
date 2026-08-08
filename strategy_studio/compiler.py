"""Strategy compiler (STUDIO_SPEC 1.3).

compile_strategy(defn) -> CompiledStrategy

CompiledStrategy is a neutral, fully-validated intermediate object.
INTEGRATION POINT for nautilus_web_app: write one adapter
    to_nautilus(compiled: CompiledStrategy) -> <your BacktestRunConfig>
in your repo and feed it to your existing NautilusTrader runner. Everything
upstream (schema, UI, AI diffs) stays unchanged.

Wiki References
---------------
Bkz: [[strategy_studio]], [[strategy_and_actor]]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .registry import INDICATOR_REGISTRY, ParamSpec
from .schema import Param, Rule, RuleGroup, StrategyDefinition


class CompileError(Exception):
    """Raised with rule_id/field context so the UI can point at the culprit."""

    def __init__(
        self, message: str, rule_id: str | None = None, field_name: str | None = None
    ):
        self.rule_id = rule_id
        self.field_name = field_name
        loc = (
            f" [rule={rule_id}" + (f", field={field_name}]" if field_name else "]")
            if rule_id
            else ""
        )
        super().__init__(message + loc)


@dataclass
class CompiledCondition:
    indicator: str
    params: dict[str, Any]
    operator: str
    target: Any | None
    timeframe: str | None
    rule_id: str


@dataclass
class CompiledGroup:
    match: str
    conditions: list[CompiledCondition] = field(default_factory=list)
    filters: list[CompiledCondition] = field(default_factory=list)


@dataclass
class CompiledStrategy:
    strategy_id: str
    version: int
    instruments: list[dict[str, str]]
    regime: dict[str, Any] | None
    allocation: dict[str, Any] | None
    entry: CompiledGroup
    exit: CompiledGroup
    risk: dict[str, Any]
    walkforward: dict[str, Any]


# ── helpers ──────────────────────────────────────────────────────


def _coerce(value: Any, spec: ParamSpec, rule_id: str, name: str) -> Any:
    try:
        if spec.type == "int":
            v: Any = int(value)
        elif spec.type == "float":
            v = float(value)
        else:
            v = str(value)
    except (TypeError, ValueError):
        raise CompileError(
            f"param '{name}' must be {spec.type}, got {value!r}", rule_id, name
        ) from None
    if spec.type in ("int", "float"):
        if spec.min is not None and v < spec.min:
            raise CompileError(
                f"param '{name}'={v} below min {spec.min}", rule_id, name
            )
        if spec.max is not None and v > spec.max:
            raise CompileError(
                f"param '{name}'={v} above max {spec.max}", rule_id, name
            )
    return v


def _compile_rule(rule: Rule) -> CompiledCondition:
    spec = INDICATOR_REGISTRY.get(rule.indicator)
    if spec is None:
        raise CompileError(f"unknown indicator '{rule.indicator}'", rule.id)
    if rule.operator not in spec.operators and rule.operator != "true":
        raise CompileError(
            f"operator '{rule.operator}' not supported by '{rule.indicator}' "
            f"(allowed: {', '.join(spec.operators)})",
            rule.id,
            "operator",
        )

    known = {p.name: p for p in spec.params}
    params: dict[str, Any] = {}
    for name, p in rule.params.items():
        if name not in known:
            raise CompileError(
                f"unknown param '{name}' for '{rule.indicator}'", rule.id, name
            )
        params[name] = _coerce(p.value, known[name], rule.id, name)
    for pspec in spec.params:  # fill defaults
        if pspec.name not in params:
            if pspec.default is None:
                raise CompileError(
                    f"missing required param '{pspec.name}'", rule.id, pspec.name
                )
            params[pspec.name] = pspec.default

    target = None
    if rule.target is not None:
        try:
            target = float(rule.target.value)
        except (TypeError, ValueError):
            target = str(rule.target.value)

    return CompiledCondition(
        indicator=rule.indicator,
        params=params,
        operator=rule.operator,
        target=target,
        timeframe=rule.timeframe,
        rule_id=rule.id,
    )


def _compile_group(group: RuleGroup, block: str) -> CompiledGroup:
    if not group.rules and block == "entry":
        raise CompileError("entry block must contain at least one rule")
    return CompiledGroup(
        match=group.match,
        conditions=[_compile_rule(r) for r in group.rules],
        filters=[_compile_rule(r) for r in group.filters],
    )


def _risk_value(p: Param, name: str) -> float:
    try:
        return float(p.value)
    except (TypeError, ValueError):
        raise CompileError(
            f"risk param '{name}' must be numeric", "risk", name
        ) from None


# ── public API ───────────────────────────────────────────────────


def compile_strategy(defn: StrategyDefinition) -> CompiledStrategy:
    active = [i for i in defn.instruments if i.active] or defn.instruments
    if not active:
        raise CompileError("strategy has no instruments configured")

    regime: dict[str, Any] | None = None
    if defn.regime:
        regime = {
            "conditions": _compile_group(defn.regime.conditions, "regime"),
            "evaluate": defn.regime.evaluate,
            "else": defn.regime.else_,
        }
        if defn.regime.else_ == "substrategy":
            sub = defn.regime.substrategy
            if sub is None:
                raise CompileError(
                    "regime else=substrategy but no substrategy is defined"
                )
            if not sub.entry.rules:
                raise CompileError(
                    "substrategy entry block must contain at least one rule"
                )
            regime["else_strategy"] = {
                "name": sub.name,
                "entry": _compile_group(sub.entry, "sub_entry"),
                "exit": _compile_group(sub.exit, "sub_exit"),
            }

    allocation: dict[str, Any] | None = None
    if defn.allocation and defn.allocation.mode == "ranked":
        a = defn.allocation
        if a.sort_indicator not in INDICATOR_REGISTRY:
            raise CompileError(
                f"allocation sort indicator '{a.sort_indicator}' is unknown",
                "allocation",
                "sort_indicator",
            )
        if a.top_n > len(active):
            raise CompileError(
                f"allocation.top_n={a.top_n} exceeds the {len(active)} "
                "active instrument(s)",
                "allocation",
                "top_n",
            )
        allocation = a.model_dump()

    risk = {
        "stop_loss_atr_mult": _risk_value(
            defn.risk.stop_loss_atr_mult, "stop_loss_atr_mult"
        ),
        "stop_loss_atr_len": int(
            _risk_value(defn.risk.stop_loss_atr_len, "stop_loss_atr_len")
        ),
        "take_profit_r": _risk_value(defn.risk.take_profit_r, "take_profit_r"),
        "risk_per_trade_pct": _risk_value(
            defn.risk.risk_per_trade_pct, "risk_per_trade_pct"
        ),
        "max_concurrent": int(_risk_value(defn.risk.max_concurrent, "max_concurrent")),
    }
    if risk["risk_per_trade_pct"] <= 0 or risk["risk_per_trade_pct"] > 5:
        raise CompileError(
            "risk_per_trade_pct must be in (0, 5]", "risk", "risk_per_trade_pct"
        )
    if defn.risk.time_stop_bars is not None:
        risk["time_stop_bars"] = int(
            _risk_value(defn.risk.time_stop_bars, "time_stop_bars")
        )

    return CompiledStrategy(
        strategy_id=defn.id,
        version=defn.version,
        instruments=[{"symbol": i.symbol, "timeframe": i.timeframe} for i in active],
        regime=regime,
        allocation=allocation,
        entry=_compile_group(defn.entry, "entry"),
        exit=_compile_group(defn.exit, "exit"),
        risk=risk,
        walkforward=defn.walkforward.model_dump(),
    )
