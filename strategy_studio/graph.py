"""StrategyDefinition → canvas graph (CANVAS_DESIGN.md, Phase 1).

``to_graph`` is a pure function: it reads a definition and returns plain dicts.
No store access, no request context, no randomness — so the canvas is testable
without a server and identical for identical input.

The graph is DERIVED, never stored. ``StrategyDefinition`` is a constrained tree
(``regime? → entry/exit RuleGroup → Rule → risk → allocation``), so edges follow
from the schema and the user never draws them; node positions are recomputed by
the client on every render. That is why no canvas-specific field (position,
zoom, edge list) exists in the schema.

Layers are assigned here rather than in the browser so the column order is part
of the tested contract; the client only turns a layer index into pixels.

Wiki References
---------------
Bkz: [[strategy_studio]] "Canvas görünümü", [[webapp_module_map]]

Kısıtlı-ağaç-serbest-graf-değil kararının genelleştirilmiş hâli genel ikinci
beyinde: `kisitli_agac_serbest_graf_degil` (bu wiki'de sayfası yok).
"""

from __future__ import annotations

from typing import Any

from strategy_studio.registry import INDICATOR_REGISTRY
from strategy_studio.schema import Param, Rule, RuleGroup, StrategyDefinition

# Column order, left to right. Regime CONDITIONS sit left of the regime node
# because they feed it; every other rule sits right of it because the regime
# gates them. Empty layers are dropped and the rest renumbered (``_compact``),
# so a strategy without a regime has no gap where the regime column would be.
_L_INSTRUMENT = 0
_L_REGIME_RULE = 1
_L_REGIME = 2
_L_RULE = 3
_L_GROUP = 4
_L_RISK = 5
_L_ALLOCATION = 6

# Group nodes, in render order. ``block`` is the mutation vocabulary used by the
# existing endpoints (``POST /studio/{id}/blocks/{block}/rules``) — keeping the
# same names is what lets the canvas reuse them without a translation table.
_GROUP_TITLES = {
    "entry": "Long conditions",
    "exit": "Exit conditions",
    "sub_entry": "Substrategy entry",
    "sub_exit": "Substrategy exit",
}
_GROUP_BADGES = {
    "entry": "ENTRY · LONG",
    "exit": "EXIT",
    "sub_entry": "SUB · ENTRY",
    "sub_exit": "SUB · EXIT",
}


# Which node a pending suggestion hangs off, by the block it was made for.
_GHOST_ANCHOR = {
    "entry": "group:entry",
    "exit": "group:exit",
    "sub_entry": "group:sub_entry",
    "sub_exit": "group:sub_exit",
    "regime": "regime",
    "risk": "risk",
    "allocation": "allocation",
}


def _fmt(v: Any) -> str:
    """Numbers as the UI writes them: 200.0 → '200', 1.5 stays 1.5."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _has_opt(rule: Rule) -> bool:
    if any(p.optimize for p in rule.params.values()):
        return True
    return bool(rule.target and rule.target.optimize)


def _rule_detail(rule: Rule) -> str:
    bits = [f"{k}={_fmt(p.value)}" for k, p in rule.params.items()]
    if rule.timeframe:
        bits.append(f"tf {rule.timeframe}")
    head = " · ".join(bits)
    tail = rule.operator.replace("_", " ")
    if rule.target is not None:
        tail = f"{tail} {_fmt(rule.target.value)}"
    return f"{head} — {tail}" if head else tail


def _rule_node(rule: Rule, block: str, is_filter: bool, layer: int) -> dict:
    spec = INDICATOR_REGISTRY.get(rule.indicator)
    return {
        "id": f"rule:{rule.id}",
        "kind": "filter" if is_filter else "rule",
        "label": spec.label if spec else rule.indicator,
        "detail": _rule_detail(rule),
        "badge": "SKIP IF" if is_filter else None,
        "block": f"{block}.filter" if is_filter else block,
        "layer": layer,
        "badges": ["opt"] if _has_opt(rule) else [],
        "ref": {"rule_id": rule.id, "indicator": rule.indicator},
    }


def _group_summary(group: RuleGroup) -> str:
    join = "AND" if group.match == "all" else "OR"
    n = len(group.rules)
    out = f"match {join} · {n} rule{'' if n == 1 else 's'}"
    if group.filters:
        out += f" · {len(group.filters)} filter{'' if len(group.filters) == 1 else 's'}"
    return out


def _risk_badges(defn: StrategyDefinition) -> list[str]:
    for name in type(defn.risk).model_fields:
        p = getattr(defn.risk, name)
        if isinstance(p, Param) and p.optimize:
            return ["opt"]
    return []


def _edge(source: str, target: str, kind: str = "flow") -> dict:
    return {
        "id": f"{source}->{target}",
        "source": source,
        "target": target,
        "kind": kind,
    }


def _compact(nodes: list[dict]) -> int:
    """Renumber layers so used columns are consecutive. Returns column count."""
    used = sorted({n["layer"] for n in nodes})
    remap = {old: i for i, old in enumerate(used)}
    for n in nodes:
        n["layer"] = remap[n["layer"]]
    return len(used)


def to_graph(defn: StrategyDefinition, ghosts: list[dict] | None = None) -> dict:
    """Derive ``{nodes, edges, meta}`` from a strategy definition.

    Node kinds: ``instrument | regime | rule | filter | group | risk |
    allocation | ghost``. Edge kinds: ``flow`` (schema spine), ``rule`` (a rule
    feeding its group), ``filter`` (a "skip if" rule — drawn dashed), ``else``
    (the regime's ELSE branch), ``ghost`` (a pending AI suggestion).

    ``ghosts`` are pending AI suggestions — ``[{id, block, label, detail,
    source}]``. They are NOT part of the definition (they live in the store), so
    they arrive as an argument rather than being read here: this function stays
    a function of what it is given.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    # ── instruments (active only — the inactive ones do not trade) ──
    instruments = [i for i in defn.instruments if i.active]
    for inst in instruments:
        nodes.append(
            {
                "id": f"inst:{inst.symbol}",
                "kind": "instrument",
                "label": inst.symbol,
                "detail": inst.timeframe,
                "badge": "DATA",
                "block": "instruments",
                "layer": _L_INSTRUMENT,
                "badges": [],
                "ref": {"symbol": inst.symbol},
            }
        )

    # ── groups (the RuleGroups that actually exist on this definition) ──
    groups: list[tuple[str, RuleGroup]] = [("entry", defn.entry), ("exit", defn.exit)]
    sub = defn.regime.substrategy if defn.regime else None
    if sub:
        groups += [("sub_entry", sub.entry), ("sub_exit", sub.exit)]
    for block, group in groups:
        nodes.append(
            {
                "id": f"group:{block}",
                "kind": "group",
                "label": _GROUP_TITLES[block],
                "detail": _group_summary(group),
                "badge": _GROUP_BADGES[block],
                "block": block,
                "layer": _L_GROUP,
                "badges": [],
                "ref": {"match": group.match},
            }
        )

    # ── regime (own group: its conditions hang off the node itself) ──
    if defn.regime:
        r = defn.regime
        nodes.append(
            {
                "id": "regime",
                "kind": "regime",
                "label": "Market regime check",
                "detail": (
                    f"eval {r.evaluate.replace('_', ' ')} · "
                    f"match {'AND' if r.conditions.match == 'all' else 'OR'} · "
                    f"else {'substrategy' if r.else_ == 'substrategy' else 'stay flat'}"
                ),
                "badge": "IF · REGIME",
                "block": "regime",
                "layer": _L_REGIME,
                "badges": [],
                "ref": {},
            }
        )
        for rule in r.conditions.rules:
            nodes.append(_rule_node(rule, "regime", False, _L_REGIME_RULE))
            edges.append(_edge(f"rule:{rule.id}", "regime", "rule"))
        for rule in r.conditions.filters:
            nodes.append(_rule_node(rule, "regime", True, _L_REGIME_RULE))
            edges.append(_edge(f"rule:{rule.id}", "regime", "filter"))

    # ── rules of every group ──
    for block, group in groups:
        for rule in group.rules:
            nodes.append(_rule_node(rule, block, False, _L_RULE))
            edges.append(_edge(f"rule:{rule.id}", f"group:{block}", "rule"))
        for rule in group.filters:
            nodes.append(_rule_node(rule, block, True, _L_RULE))
            edges.append(_edge(f"rule:{rule.id}", f"group:{block}", "filter"))

    # ── risk / allocation ──
    nodes.append(
        {
            "id": "risk",
            "kind": "risk",
            "label": "Position & risk",
            "detail": (
                f"SL {_fmt(defn.risk.stop_loss_atr_mult.value)}×ATR"
                f"({_fmt(defn.risk.stop_loss_atr_len.value)}) · "
                f"TP {_fmt(defn.risk.take_profit_r.value)}R · "
                f"risk {_fmt(defn.risk.risk_per_trade_pct.value)}%"
            ),
            "badge": "RISK",
            "block": "risk",
            "layer": _L_RISK,
            "badges": _risk_badges(defn),
            "ref": {},
        }
    )
    if defn.allocation:
        a = defn.allocation
        detail = (
            f"top {a.top_n} by {a.sort_indicator} {a.sort_direction} · "
            f"{a.weighting} · rebalance {a.rebalance}"
            if a.mode == "ranked"
            else "each active instrument trades independently"
        )
        nodes.append(
            {
                "id": "allocation",
                "kind": "allocation",
                "label": f"Universe · {a.mode}",
                "detail": detail,
                "badge": "ALLOCATION",
                "block": "allocation",
                "layer": _L_ALLOCATION,
                "badges": [],
                "ref": {"mode": a.mode},
            }
        )

    # ── spine edges ──
    # Instruments feed the first gate: the regime when there is one, otherwise
    # the main groups directly. The substrategy groups hang off the ELSE branch,
    # so they are never fed straight from the instruments.
    heads = ["regime"] if defn.regime else ["group:entry", "group:exit"]
    for inst in instruments:
        for head in heads:
            edges.append(_edge(f"inst:{inst.symbol}", head))
    if defn.regime:
        edges.append(_edge("regime", "group:entry"))
        edges.append(_edge("regime", "group:exit"))
        if sub:
            edges.append(_edge("regime", "group:sub_entry", "else"))
            edges.append(_edge("regime", "group:sub_exit", "else"))
    for block, _group in groups:
        edges.append(_edge(f"group:{block}", "risk"))
    if defn.allocation:
        edges.append(_edge("risk", "allocation"))

    # ── pending AI suggestions, drawn one column left of what they touch ──
    by_id = {n["id"]: n for n in nodes}
    for gh in ghosts or []:
        anchor = by_id.get(_GHOST_ANCHOR.get(gh["block"], ""))
        if anchor is None:
            continue  # a suggestion for a block this strategy does not have
        nodes.append(
            {
                "id": f"ghost:{gh['id']}",
                "kind": "ghost",
                "label": gh.get("label", "AI suggestion"),
                "detail": gh.get("detail", ""),
                "badge": "AI · LOOP" if gh.get("source") == "loop" else "AI",
                "block": gh["block"],
                "layer": anchor["layer"] - 1,
                "badges": [],
                "ref": {"suggestion_id": gh["id"]},
            }
        )
        edges.append(_edge(f"ghost:{gh['id']}", anchor["id"], "ghost"))

    columns = _compact(nodes)
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "strategy_id": defn.id,
            "name": defn.name,
            "version": defn.version,
            "columns": columns,
            "counts": {
                "nodes": len(nodes),
                "edges": len(edges),
                "rules": sum(1 for n in nodes if n["kind"] in ("rule", "filter")),
            },
        },
    }
