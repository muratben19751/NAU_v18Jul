"""Composed-strategy spec model + spec-upsert service: the block-role type
vocabulary, SignalBlock/ComposedStrategySpec dataclasses, and build_spec
(the single home for both constructor call-sites -- strategy.py's
POST /strategy/save form path and backtest.py's /describe NL path).

composer.py decomposition (Faz 2, Adım 4 -- riskier-layer session).
Extracted verbatim from composer.py.

ComposedStrategySpec.validate() reads BLOCK_REGISTRY, which stays in
composer.py (the registry core is out of this session's scope) -- reached
via a function-local `from composer import BLOCK_REGISTRY` inside
validate() itself to avoid a module-level circular import (composer.py
imports this module at its own top level), the same pattern this file's
own register_custom_from_disk/_load_module_from_path already use for
their own cross-module dependencies.

ComposedStrategyConfig (a different class -- the Nautilus StrategyConfig
subclass) and ComposedStrategy itself never move out of composer.py; see
the decomposition plan's critical-decision section.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

BlockRole = Literal["entry", "exit"]
BlockType = str  # dynamic — built-ins + custom

# Strategy-level option literals
EntryExitLogic = Literal["OR", "AND"]
OrderTypeOpt = Literal["market", "limit"]
SLType = Literal["percent", "atr"]
TPType = Literal["percent", "atr", "off"]
TradeSizeMode = Literal[
    "fixed", "fixed_usdt", "percent_equity", "atr_target", "vol_target"
]


def _signal_matches_role(role: str, result) -> bool:
    """Interpret a block output using the role's exact signal vocabulary."""

    if role == "entry":
        return result in ("long", "short")
    if role == "exit":
        return result == "exit"
    return False


@dataclass
class SignalBlock:
    type: BlockType
    role: BlockRole
    params: dict


@dataclass
class ComposedStrategySpec:
    id: str
    name: str
    description: str
    blocks: list[SignalBlock]
    trade_size: float = 0.1
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Optional strategy-level Nautilus features (backward-compatible defaults)
    entry_logic: EntryExitLogic = "OR"
    exit_logic: EntryExitLogic = "OR"
    order_type: OrderTypeOpt = "market"
    limit_offset_bps: float = 0.0
    use_bracket: bool = False
    sl_type: SLType = "percent"
    sl_value: float = 2.0
    tp_type: TPType = "off"
    tp_value: float = 4.0
    atr_period: int = 14
    allow_short: bool = False
    trade_size_mode: TradeSizeMode = "fixed"
    trade_size_percent: float = 5.0
    trade_size_atr_risk: float = 1.0
    trade_size_usdt: float = 1000.0
    # vol_target sizing: size = (vol_target / ewma_vol) * capital / price.
    # capital is a FIXED notional (not live equity), fed from the form's
    # Initial Capital. See _compute_qty and [[vol_targeted_trend]].
    trade_size_vol_target: float = 0.02
    trade_size_vol_span: int = 10
    trade_size_capital: float = 10000.0
    emulate: bool = False
    # Multi-timeframe trend filter (optional)
    trend_filter: bool = False
    trend_interval: str = "60"  # Bybit interval code for the trend bar feed
    trend_ema_period: int = 50
    # Delay fill: execute entry on next bar's open instead of signal bar's close
    # Eliminates same-bar look-ahead bias; default True for more realistic execution
    delay_fill: bool = True
    # Deterministic adverse fill model. AUTO enables this so selection,
    # robustness and holdout all pay one tick on aggressive fills; manual/legacy
    # specs remain backward compatible unless explicitly enabled.
    model_slippage: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocks"] = [asdict(b) for b in self.blocks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ComposedStrategySpec:
        blocks = [SignalBlock(**b) for b in d.get("blocks", [])]
        return cls(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            blocks=blocks,
            trade_size=d.get("trade_size", 0.1),
            created_at=d.get("created_at", datetime.now(UTC).isoformat()),
            entry_logic=d.get("entry_logic", "OR"),
            exit_logic=d.get("exit_logic", "OR"),
            order_type=d.get("order_type", "market"),
            limit_offset_bps=float(d.get("limit_offset_bps", 0.0)),
            use_bracket=bool(d.get("use_bracket", False)),
            sl_type=d.get("sl_type", "percent"),
            sl_value=float(d.get("sl_value", 2.0)),
            tp_type=d.get("tp_type", "off"),
            tp_value=float(d.get("tp_value", 4.0)),
            atr_period=int(d.get("atr_period", 14)),
            allow_short=bool(d.get("allow_short", False)),
            trade_size_mode=d.get("trade_size_mode", "fixed"),
            trade_size_percent=float(d.get("trade_size_percent", 5.0)),
            trade_size_atr_risk=float(d.get("trade_size_atr_risk", 1.0)),
            trade_size_usdt=float(d.get("trade_size_usdt", 1000.0)),
            trade_size_vol_target=float(d.get("trade_size_vol_target", 0.02)),
            trade_size_vol_span=int(d.get("trade_size_vol_span", 10)),
            trade_size_capital=float(d.get("trade_size_capital", 10000.0)),
            emulate=bool(d.get("emulate", False)),
            trend_filter=bool(d.get("trend_filter", False)),
            trend_interval=str(d.get("trend_interval", "60")),
            trend_ema_period=int(d.get("trend_ema_period", 50)),
            delay_fill=bool(d.get("delay_fill", True)),
            model_slippage=bool(d.get("model_slippage", False)),
        )

    def validate(self) -> str | None:
        from composer import BLOCK_REGISTRY

        if not self.name or not self.name.strip():
            return "Strategy name is required."
        if not self.blocks:
            return "At least one signal block is required."
        if not any(b.role == "entry" for b in self.blocks):
            return "At least one 'entry' block is required."
        for b in self.blocks:
            reg_entry = BLOCK_REGISTRY.get(b.type)
            if reg_entry is None:
                return f"Unknown block type: {b.type}"
            # Custom blocks generated for AUTO carry their semantic role in
            # metadata. A saved exit block must never be re-used as an entry
            # (or vice versa): the function may still execute, but its return
            # vocabulary has a different meaning and can manufacture trades.
            declared_role = (reg_entry.get("meta") or {}).get("role")
            if declared_role in ("entry", "exit") and b.role != declared_role:
                return (
                    f"Custom block {b.type!r} is declared for role "
                    f"{declared_role!r}, not {b.role!r}."
                )
            v = reg_entry.get("validate")
            if v is not None:
                # Carry the L25 isolation into validate too: give the custom validate
                # hook a view carrying a COPY of params instead of the live SignalBlock —
                # so `b.params.update(...)` cannot corrupt the spec's live dict. Built-in
                # validators are read-only, pass with the real block (behavior unchanged).
                vb = (
                    b
                    if reg_entry.get("builtin")
                    else SimpleNamespace(
                        params=dict(b.params), role=b.role, type=b.type
                    )
                )
                err = v(vb)
                if err:
                    return err
        if self.use_bracket:
            if self.sl_value <= 0:
                return "sl_value must be > 0 when bracket is enabled."
            if self.tp_type != "off" and self.tp_value <= 0:
                return "tp_value must be > 0 when TP is not off."
        # DeepR 2026-08-09 [ORTA]: the HTML input's min is browser-side only;
        # a direct POST could pass 0/negative and reach Nautilus's own
        # AverageTrueRange(period) with an invalid value, which raises AFTER
        # the sandbox has already spent a full backtest run. Only required
        # when ATR is actually consulted — mirrors ComposedStrategy.on_start's
        # own needs_atr condition (sl_type/tp_type == "atr" or ATR-target sizing).
        needs_atr = (
            self.sl_type == "atr"
            or self.tp_type == "atr"
            or self.trade_size_mode == "atr_target"
        )
        if needs_atr and self.atr_period <= 0:
            return "atr_period must be > 0 when ATR-based SL/TP/sizing is used."
        if self.trade_size_mode == "percent_equity":
            try:
                pct = float(self.trade_size_percent)
            except (TypeError, ValueError):
                return "trade_size_percent must be a finite number."
            if not math.isfinite(pct):
                return "trade_size_percent must be a finite number."
            if pct <= 0:
                return "trade_size_percent must be > 0."
            if pct > 100:
                return "trade_size_percent must be <= 100 for an unlevered strategy."
        if self.trade_size_mode == "atr_target" and self.trade_size_atr_risk <= 0:
            return "trade_size_atr_risk must be > 0."
        if self.trade_size_mode == "fixed_usdt" and self.trade_size_usdt <= 0:
            return "trade_size_usdt must be > 0."
        if self.trade_size_mode == "vol_target":
            if self.trade_size_vol_target <= 0:
                return "trade_size_vol_target must be > 0."
            if self.trade_size_vol_span < 2:
                return "trade_size_vol_span must be >= 2."
            if self.trade_size_capital <= 0:
                return "trade_size_capital must be > 0."
        return None


# --------------------------------------------------------------------------
# Spec-upsert service — single home for BOTH constructor call-sites
# (strategy.py POST /strategy/save form path + backtest.py /describe NL path).
# Before this, each site hand-built ComposedStrategySpec with its own inline
# clamps; the two disagreed (save() dropped vol_target; describe skipped
# bracket/order/trend). build_spec accepts the UNION of fields and centralizes
# every clamp/whitelist so a new spec field is added in ONE place.
# --------------------------------------------------------------------------
def _as_bool(v) -> bool:
    """Coerce an HTML form value to bool. Empty string / "0" / "false" / "off"
    → False (checkbox-style forms send "" when unchecked). Matches the intent of
    both the old ``bool(form_str)`` and ``_parse_bool_form`` call-sites."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() not in ("", "0", "false", "off", "no")


def build_spec(
    *,
    name: str,
    description: str,
    blocks: list[SignalBlock],
    trade_size: float = 0.1,
    entry_logic: str = "OR",
    exit_logic: str = "OR",
    order_type: str = "market",
    limit_offset_bps: float = 0.0,
    use_bracket=False,
    sl_type: str = "percent",
    sl_value: float = 2.0,
    tp_type: str = "off",
    tp_value: float = 4.0,
    atr_period: int = 14,
    allow_short=False,
    trade_size_mode: str = "fixed",
    trade_size_percent: float = 5.0,
    trade_size_atr_risk: float = 1.0,
    trade_size_usdt: float = 1000.0,
    trade_size_vol_target: float = 0.02,
    trade_size_vol_span: int = 10,
    trade_size_capital: float = 10000.0,
    emulate=False,
    trend_filter=False,
    trend_interval: str = "60",
    trend_ema_period: int = 50,
) -> ComposedStrategySpec:
    """Build a validated ``ComposedStrategySpec`` from the union of both call
    sites' fields, applying every clamp/whitelist centrally. Assigns a fresh
    ``id`` via :func:`new_spec_id`. Booleans accept HTML-form strings (see
    :func:`_as_bool`). The caller still handles persistence + validation errors.

    Note: ``trade_size_mode`` whitelist now INCLUDES ``vol_target`` — the old
    strategy.py save() clamp excluded it and silently downgraded vol_target
    strategies to "fixed", so a vol_target spec could not round-trip. Fixed here.
    """
    return ComposedStrategySpec(
        id=new_spec_id(),
        # No silent "unnamed" fallback: an empty/falsy name and a
        # whitespace-only one used to be handled inconsistently (only the
        # latter survived stripping as ""), and either way a blank name
        # silently saved created indistinguishable catalog/CSV rows.
        # validate() now rejects both the same way — the caller re-prompts
        # instead of a strategy quietly landing in the catalog unnamed.
        name=(name or "").strip(),
        description=(description or "").strip(),
        blocks=list(blocks),
        trade_size=float(trade_size),
        entry_logic=entry_logic if entry_logic in ("OR", "AND") else "OR",
        exit_logic=exit_logic if exit_logic in ("OR", "AND") else "OR",
        order_type=order_type if order_type in ("market", "limit") else "market",
        limit_offset_bps=float(limit_offset_bps),
        use_bracket=_as_bool(use_bracket),
        sl_type=sl_type if sl_type in ("percent", "atr") else "percent",
        sl_value=float(sl_value),
        tp_type=tp_type if tp_type in ("percent", "atr", "off") else "off",
        tp_value=float(tp_value),
        atr_period=int(atr_period),
        allow_short=_as_bool(allow_short),
        trade_size_mode=(
            trade_size_mode
            if trade_size_mode
            in ("fixed", "fixed_usdt", "percent_equity", "atr_target", "vol_target")
            else "fixed"
        ),
        trade_size_percent=float(trade_size_percent),
        trade_size_atr_risk=float(trade_size_atr_risk),
        trade_size_usdt=float(trade_size_usdt),
        trade_size_vol_target=float(trade_size_vol_target),
        trade_size_vol_span=int(trade_size_vol_span),
        trade_size_capital=float(trade_size_capital),
        emulate=_as_bool(emulate),
        trend_filter=_as_bool(trend_filter),
        trend_interval=(trend_interval or "60").strip(),
        trend_ema_period=int(trend_ema_period),
    )


def new_spec_id() -> str:
    return uuid.uuid4().hex[:12]
