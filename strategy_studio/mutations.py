"""Strategy mutations (STUDIO_SPEC Phase 2).

Every edit operates on a StrategyDefinition (the draft) and returns the
mutated copy. Validation errors raise MutationError with UI-friendly text.
"""
from __future__ import annotations

from .registry import INDICATOR_REGISTRY, ParamSpec
from .schema import OptimizeRange, Param, Rule, RuleGroup, StrategyDefinition

_TARGET_OPS = {"gt", "lt", "gte", "lte", "crosses_above", "crosses_below"}


class MutationError(Exception):
    pass


# ── lookup helpers ───────────────────────────────────────────────

def get_group(defn: StrategyDefinition, block: str) -> RuleGroup:
    if block == "entry":
        return defn.entry
    if block == "exit":
        return defn.exit
    if block == "regime":
        if defn.regime is None:
            raise MutationError("strategy has no regime block")
        return defn.regime.conditions
    if block in ("sub_entry", "sub_exit"):
        if defn.regime is None or defn.regime.substrategy is None:
            raise MutationError("strategy has no regime substrategy")
        sub = defn.regime.substrategy
        return sub.entry if block == "sub_entry" else sub.exit
    raise MutationError(f"unknown block '{block}'")


def find_rule(defn: StrategyDefinition, rule_id: str):
    """-> (block_name, container_list, index, rule)"""
    for block in ("entry", "exit", "regime", "sub_entry", "sub_exit"):
        try:
            group = get_group(defn, block)
        except MutationError:
            continue
        for container in (group.rules, group.filters):
            for i, rule in enumerate(container):
                if rule.id == rule_id:
                    return block, container, i, rule
    raise MutationError(f"rule '{rule_id}' not found")


def _coerce_bounded(value: str, spec: ParamSpec, name: str):
    try:
        if spec.type == "int":
            f = float(value)
            if f != int(f):
                raise ValueError
            v = int(f)
        else:
            v = float(value) if spec.type == "float" else str(value)
    except (TypeError, ValueError):
        raise MutationError(f"'{name}' must be {spec.type}, got {value!r}")
    if spec.type in ("int", "float"):
        if spec.min is not None and v < spec.min:
            raise MutationError(f"'{name}'={v} below min {spec.min}")
        if spec.max is not None and v > spec.max:
            raise MutationError(f"'{name}'={v} above max {spec.max}")
    return v


# ── mutations ────────────────────────────────────────────────────

def add_rule(defn: StrategyDefinition, block: str, indicator: str,
             as_filter: bool = False) -> Rule:
    spec = INDICATOR_REGISTRY.get(indicator)
    if spec is None:
        raise MutationError(f"unknown indicator '{indicator}'")
    group = get_group(defn, block)
    params = {p.name: Param(value=p.default)
              for p in spec.params if p.default is not None}
    operator = spec.operators[0]
    target = Param(value=0) if operator in _TARGET_OPS else None
    rule = Rule(indicator=indicator, params=params,
                operator=operator, target=target)
    (group.filters if as_filter else group.rules).append(rule)
    return rule


def update_rule_param(defn: StrategyDefinition, rule_id: str,
                      param: str, value: str) -> Rule:
    _block, _c, _i, rule = find_rule(defn, rule_id)
    spec = INDICATOR_REGISTRY[rule.indicator]
    if param == "target":
        if rule.target is None:
            raise MutationError(f"rule '{rule_id}' has no target")
        try:
            new = float(value)
        except (TypeError, ValueError):
            raise MutationError(f"target must be numeric, got {value!r}")
        rule.target = Param(value=new, optimize=rule.target.optimize)
        return rule
    known = {p.name: p for p in spec.params}
    if param not in known:
        raise MutationError(
            f"unknown param '{param}' for '{rule.indicator}'")
    coerced = _coerce_bounded(value, known[param], param)
    old = rule.params.get(param)
    rule.params[param] = Param(value=coerced,
                               optimize=old.optimize if old else None)
    return rule


def delete_rule(defn: StrategyDefinition, rule_id: str) -> str:
    block, container, i, _rule = find_rule(defn, rule_id)
    if block == "entry" and container is defn.entry.rules \
            and len(defn.entry.rules) == 1:
        raise MutationError("entry block must keep at least one rule")
    container.pop(i)
    return block


def set_block_attr(defn: StrategyDefinition, block: str,
                   match: str | None = None,
                   evaluate: str | None = None) -> None:
    if match is not None:
        if match not in ("all", "any"):
            raise MutationError("match must be 'all' or 'any'")
        get_group(defn, block).match = match
    if evaluate is not None:
        if block != "regime" or defn.regime is None:
            raise MutationError("evaluate only applies to the regime block")
        if evaluate not in ("every_bar", "4h", "daily_close"):
            raise MutationError(f"invalid evaluate '{evaluate}'")
        defn.regime.evaluate = evaluate


_RISK_BOUNDS = {
    "stop_loss_atr_mult": (0.1, 20.0), "stop_loss_atr_len": (2, 100),
    "take_profit_r": (0.1, 20.0), "risk_per_trade_pct": (0.01, 5.0),
    "max_concurrent": (1, 50), "time_stop_bars": (1, 5000),
}


def update_risk(defn: StrategyDefinition, name: str, value: str) -> None:
    if name not in _RISK_BOUNDS:
        raise MutationError(f"unknown risk field '{name}'")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise MutationError(f"'{name}' must be numeric, got {value!r}")
    lo, hi = _RISK_BOUNDS[name]
    if not (lo <= v <= hi):
        raise MutationError(f"'{name}'={v} outside [{lo}, {hi}]")
    if name in ("stop_loss_atr_len", "max_concurrent", "time_stop_bars"):
        v = int(v)
    old: Param | None = getattr(defn.risk, name)
    setattr(defn.risk, name,
            Param(value=v, optimize=old.optimize if old else None))


def _find_param(defn: StrategyDefinition, owner: str, name: str) -> Param:
    """owner = rule_id or 'risk'."""
    if owner == "risk":
        if name not in _RISK_BOUNDS:
            raise MutationError(f"unknown risk field '{name}'")
        p: Param | None = getattr(defn.risk, name)
        if p is None:
            raise MutationError(f"risk field '{name}' not set")
        return p
    _b, _c, _i, rule = find_rule(defn, owner)
    if name == "target":
        if rule.target is None:
            raise MutationError(f"rule '{owner}' has no target")
        return rule.target
    if name not in rule.params:
        raise MutationError(f"rule '{owner}' has no param '{name}'")
    return rule.params[name]


def _default_range(p: Param) -> OptimizeRange:
    """Sensible sweep default around the current value: ±50%, 7 steps."""
    try:
        v = float(p.value)
    except (TypeError, ValueError):
        raise MutationError("only numeric params can be optimized")
    lo, hi = sorted((v * 0.5, v * 1.5)) if v != 0 else (-1.0, 1.0)
    step = round((hi - lo) / 6, 6) or 1.0
    if isinstance(p.value, int) and not isinstance(p.value, bool):
        lo, hi = int(lo) or 1, int(hi) or 2
        step = max(1, int(step))
    return OptimizeRange(min=lo, step=step, max=hi)


def toggle_optimize(defn: StrategyDefinition, owner: str, name: str) -> bool:
    """Flip a param's sweep inclusion. -> True if now included."""
    p = _find_param(defn, owner, name)
    p.optimize = None if p.optimize else _default_range(p)
    return p.optimize is not None


def set_optimize_range(defn: StrategyDefinition, owner: str, name: str,
                       min_v: str, step_v: str, max_v: str) -> None:
    p = _find_param(defn, owner, name)
    try:
        p.optimize = OptimizeRange(min=float(min_v), step=float(step_v),
                                   max=float(max_v))
    except (TypeError, ValueError) as e:
        raise MutationError(f"invalid range: {e}")


def set_regime_else(defn: StrategyDefinition, mode: str) -> None:
    """Switch the ELSE branch between flat and an inline substrategy.
    Switching to 'substrategy' seeds a minimal mean-revert pair so the
    strategy stays compilable; an existing substrategy is preserved when
    toggling back and forth."""
    if defn.regime is None:
        raise MutationError("strategy has no regime block")
    if mode not in ("flat", "substrategy"):
        raise MutationError("else mode must be 'flat' or 'substrategy'")
    defn.regime.else_ = mode
    if mode == "substrategy" and defn.regime.substrategy is None:
        from .schema import SubStrategy
        sub = SubStrategy(name="Mean-revert in chop")
        rsi_in = Rule(indicator="rsi",
                      params={"len": Param(value=14)},
                      operator="lt", target=Param(value=30))
        rsi_out = Rule(indicator="rsi",
                       params={"len": Param(value=14)},
                       operator="gt", target=Param(value=50))
        sub.entry.rules.append(rsi_in)
        sub.exit.rules.append(rsi_out)
        defn.regime.substrategy = sub


_ALLOC_FIELDS = {
    "mode": ("single", "ranked"),
    "sort_indicator": None,          # validated against the registry
    "sort_direction": ("desc", "asc"),
    "weighting": ("equal", "inverse_volatility"),
    "rebalance": ("daily", "weekly", "on_signal"),
    "top_n": None,                   # validated as int >= 1
}


def update_allocation(defn: StrategyDefinition, name: str, value: str) -> None:
    from .schema import AllocationBlock
    if name not in _ALLOC_FIELDS:
        raise MutationError(f"unknown allocation field '{name}'")
    if defn.allocation is None:
        defn.allocation = AllocationBlock()
    if name == "top_n":
        try:
            v = int(float(value))
        except (TypeError, ValueError):
            raise MutationError("allocation.top_n must be an integer")
        if v < 1:
            raise MutationError("allocation.top_n must be >= 1")
        defn.allocation.top_n = v
        return
    if name == "sort_indicator":
        if value not in INDICATOR_REGISTRY:
            raise MutationError(f"unknown indicator '{value}'")
        defn.allocation.sort_indicator = value
        return
    allowed = _ALLOC_FIELDS[name]
    if value not in allowed:
        raise MutationError(
            f"allocation.{name} must be one of {', '.join(allowed)}")
    setattr(defn.allocation, name, value)


def toggle_instrument(defn: StrategyDefinition, symbol: str) -> None:
    hit = False
    for inst in defn.instruments:
        if inst.symbol == symbol:
            inst.active = not inst.active
            hit = True
    if not hit:
        raise MutationError(f"instrument '{symbol}' not configured")
    if not any(i.active for i in defn.instruments):
        raise MutationError("at least one instrument must stay active")
