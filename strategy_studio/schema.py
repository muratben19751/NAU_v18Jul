"""Strategy Studio — StrategyDefinition schema (STUDIO_SPEC 1.1).

Single source of truth: the UI renders this document, the LLM edits it,
the compiler translates it. Nothing else holds strategy state.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Operator = Literal[
    "crosses_above", "crosses_below", "gt", "lt", "gte", "lte",
    "price_above", "price_below", "within_session", "true",
]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class OptimizeRange(BaseModel):
    min: float
    step: float
    max: float

    @model_validator(mode="after")
    def _check(self) -> OptimizeRange:
        if self.step <= 0:
            raise ValueError("optimize.step must be > 0")
        if self.min > self.max:
            raise ValueError("optimize.min must be <= optimize.max")
        return self

    @property
    def n_values(self) -> int:
        return round((self.max - self.min) / self.step) + 1


class Param(BaseModel):
    value: float | int | str
    optimize: OptimizeRange | None = None


class Rule(BaseModel):
    id: str = Field(default_factory=new_id)
    indicator: str
    params: dict[str, Param] = Field(default_factory=dict)
    operator: Operator = "true"
    target: Param | None = None
    timeframe: str | None = None  # None => strategy timeframe


class RuleGroup(BaseModel):
    match: Literal["all", "any"] = "all"
    rules: list[Rule] = Field(default_factory=list)
    filters: list[Rule] = Field(default_factory=list)  # "skip if" filters


class SubStrategy(BaseModel):
    """Inline strategy for the regime ELSE branch (e.g. mean-revert in chop).
    Shares the main strategy's risk block and instruments."""
    name: str = "Else branch"
    entry: RuleGroup = Field(default_factory=RuleGroup)
    exit: RuleGroup = Field(default_factory=RuleGroup)


class RegimeBranch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conditions: RuleGroup
    evaluate: Literal["every_bar", "4h", "daily_close"] = "every_bar"
    then: Literal["main"] = "main"
    else_: Literal["flat", "substrategy"] = Field("flat", alias="else")
    substrategy: SubStrategy | None = None
    substrategy_id: str | None = None  # reserved: reference a saved strategy


class RiskBlock(BaseModel):
    stop_loss_atr_mult: Param
    stop_loss_atr_len: Param
    take_profit_r: Param
    risk_per_trade_pct: Param
    max_concurrent: Param
    time_stop_bars: Param | None = None


class WalkForwardConfig(BaseModel):
    scheme: Literal["anchored_purged_kfold"] = "anchored_purged_kfold"
    in_sample_months: int = 9
    oos_months: int = 3
    folds: int = 6
    objective: Literal["dsr", "sharpe", "max_dd", "custom"] = "dsr"
    embargo_bars: int = 24

    @field_validator("folds")
    @classmethod
    def _folds(cls, v: int) -> int:
        if v < 2:
            raise ValueError("walkforward.folds must be >= 2")
        return v


class AllocationBlock(BaseModel):
    """Composer-style universe handling. mode='single' keeps today's
    behavior (trade every active instrument independently); 'ranked' sorts
    the active universe by an indicator and trades the top N with the
    chosen weighting."""
    mode: Literal["single", "ranked"] = "single"
    sort_indicator: str = "relative_volume"
    sort_direction: Literal["desc", "asc"] = "desc"
    top_n: int = 2
    weighting: Literal["equal", "inverse_volatility"] = "equal"
    rebalance: Literal["daily", "weekly", "on_signal"] = "daily"

    @field_validator("top_n")
    @classmethod
    def _top_n(cls, v: int) -> int:
        if v < 1:
            raise ValueError("allocation.top_n must be >= 1")
        return v


class InstrumentConfig(BaseModel):
    symbol: str
    timeframe: str
    active: bool = False


class StrategyDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    version: int = 1
    regime: RegimeBranch | None = None
    allocation: AllocationBlock | None = None
    entry: RuleGroup
    exit: RuleGroup
    risk: RiskBlock
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    walkforward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    parent_version: int | None = None
    origin: Literal["user", "ai"] = "user"

    # ── convenience ──────────────────────────────────────────────
    def iter_rules(self):
        """Yield (block_name, rule) for every rule in the tree."""
        if self.regime:
            for r in self.regime.conditions.rules:
                yield "regime", r
            if self.regime.substrategy:
                for r in self.regime.substrategy.entry.rules:
                    yield "sub_entry", r
                for r in self.regime.substrategy.entry.filters:
                    yield "sub_entry.filter", r
                for r in self.regime.substrategy.exit.rules:
                    yield "sub_exit", r
                for r in self.regime.substrategy.exit.filters:
                    yield "sub_exit.filter", r
        for r in self.entry.rules:
            yield "entry", r
        for r in self.entry.filters:
            yield "entry.filter", r
        for r in self.exit.rules:
            yield "exit", r
        for r in self.exit.filters:
            yield "exit.filter", r

    def optimized_params(self) -> list[tuple[str, str, str, Param]]:
        """(block, rule_id or 'risk', param_name, param) for every param
        with an optimize range — feeds the Optimization panel."""
        out: list[tuple[str, str, str, Param]] = []
        for block, rule in self.iter_rules():
            for name, p in rule.params.items():
                if p.optimize:
                    out.append((block, rule.id, name, p))
            if rule.target and rule.target.optimize:
                out.append((block, rule.id, "target", rule.target))
        for name in type(self.risk).model_fields:
            p = getattr(self.risk, name)
            if isinstance(p, Param) and p.optimize:
                out.append(("risk", "risk", name, p))
        return out

    def sweep_size(self) -> int:
        n = 1
        for *_x, p in self.optimized_params():
            n *= p.optimize.n_values  # type: ignore[union-attr]
        return n if self.optimized_params() else 0
