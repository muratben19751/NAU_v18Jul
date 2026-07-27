"""to_graph() — the StrategyDefinition → canvas graph derivation.

The canvas stores nothing, so these are the only guarantees the view rests on:
one node per rule, a deterministic edge set, and nodes that simply do not exist
when the corresponding part of the schema does not.
"""

from __future__ import annotations

import pytest

from scripts.seed_studio import build_engine_fixture, build_fixture
from strategy_studio.graph import to_graph
from strategy_studio.schema import Param, Rule, RuleGroup


@pytest.fixture()
def full():
    """wt-funding-v3 — regime + filters + optimized params."""
    return build_fixture()


@pytest.fixture()
def plain():
    """rsi-adx-btc — no regime, no allocation."""
    return build_engine_fixture()


def _ids(nodes):
    return [n["id"] for n in nodes]


def test_one_node_per_rule(full):
    g = to_graph(full)
    rule_ids = {f"rule:{r.id}" for _b, r in full.iter_rules()}
    got = [n["id"] for n in g["nodes"] if n["kind"] in ("rule", "filter")]
    assert sorted(got) == sorted(rule_ids)
    assert len(got) == len(set(got))  # exactly one, never a duplicate


def test_rule_edges_point_at_their_container(full):
    g = to_graph(full)
    by_source = {e["source"]: e for e in g["edges"] if e["kind"] in ("rule", "filter")}
    for block, rule in full.iter_rules():
        edge = by_source[f"rule:{rule.id}"]
        expected = (
            "regime" if block.startswith("regime") else f"group:{block.split('.')[0]}"
        )
        assert edge["target"] == expected


def test_filters_are_dashed_edges(full):
    g = to_graph(full)
    filter_rule_ids = {
        f"rule:{r.id}" for b, r in full.iter_rules() if b.endswith(".filter")
    }
    assert filter_rule_ids, "fixture must exercise filters"
    dashed = {e["source"] for e in g["edges"] if e["kind"] == "filter"}
    assert dashed == filter_rule_ids
    for n in g["nodes"]:
        if n["id"] in filter_rule_ids:
            assert n["kind"] == "filter"


def test_no_regime_no_regime_node(plain):
    assert plain.regime is None
    g = to_graph(plain)
    assert "regime" not in _ids(g["nodes"])
    assert not [e for e in g["edges"] if e["kind"] == "else"]
    # instruments feed the groups directly when nothing gates them
    heads = {e["target"] for e in g["edges"] if e["source"].startswith("inst:")}
    assert heads == {"group:entry", "group:exit"}


def test_regime_gates_the_groups(full):
    g = to_graph(full)
    assert {e["target"] for e in g["edges"] if e["source"].startswith("inst:")} == {
        "regime"
    }
    assert {"group:entry", "group:exit"} <= {
        e["target"] for e in g["edges"] if e["source"] == "regime"
    }


def test_no_allocation_no_allocation_node(plain, full):
    assert to_graph(plain)["nodes"] == [
        n for n in to_graph(plain)["nodes"] if n["id"] != "allocation"
    ]
    assert "allocation" not in _ids(to_graph(plain)["nodes"])
    if full.allocation is None:
        assert "allocation" not in _ids(to_graph(full)["nodes"])


def test_inactive_instruments_are_not_drawn(full):
    g = to_graph(full)
    drawn = {n["ref"]["symbol"] for n in g["nodes"] if n["kind"] == "instrument"}
    assert drawn == {i.symbol for i in full.instruments if i.active}
    assert drawn != {i.symbol for i in full.instruments}, (
        "fixture must have an inactive one"
    )


def test_edge_count_is_deterministic(plain):
    g = to_graph(plain)
    n_inst = sum(1 for i in plain.instruments if i.active)
    n_rules = sum(1 for _b, _r in plain.iter_rules())
    # instruments→(entry,exit) + rule→group + group→risk (entry, exit)
    assert len(g["edges"]) == n_inst * 2 + n_rules + 2
    assert len({e["id"] for e in g["edges"]}) == len(g["edges"])


def test_optimized_params_get_the_opt_badge(full):
    g = to_graph(full)
    badged = {n["id"] for n in g["nodes"] if "opt" in n["badges"]}
    expected = {
        ("risk" if owner == "risk" else f"rule:{owner}")
        for _block, owner, _name, _p in full.optimized_params()
    }
    assert badged == expected


def test_layers_are_compacted_and_ordered(full, plain):
    for defn in (full, plain):
        g = to_graph(defn)
        layers = sorted({n["layer"] for n in g["nodes"]})
        assert layers == list(range(len(layers)))
        assert g["meta"]["columns"] == len(layers)
        # every edge but the regime-condition ones runs left to right
        pos = {n["id"]: n["layer"] for n in g["nodes"]}
        for e in g["edges"]:
            assert pos[e["source"]] < pos[e["target"]], e


def test_pure_and_stable(full):
    before = full.model_dump_json()
    first, second = to_graph(full), to_graph(full)
    assert first == second
    assert full.model_dump_json() == before  # definition untouched


def test_substrategy_groups_hang_off_the_else_branch(full):
    from strategy_studio.schema import SubStrategy

    defn = full.model_copy(deep=True)
    defn.regime.else_ = "substrategy"
    defn.regime.substrategy = SubStrategy(
        entry=RuleGroup(rules=[Rule(indicator="rsi", params={"len": Param(value=14)})])
    )
    g = to_graph(defn)
    ids = _ids(g["nodes"])
    assert "group:sub_entry" in ids and "group:sub_exit" in ids
    else_edges = {e["target"] for e in g["edges"] if e["kind"] == "else"}
    assert else_edges == {"group:sub_entry", "group:sub_exit"}
    # the substrategy shares the main risk block
    assert _edge_exists(g, "group:sub_entry", "risk")


def _edge_exists(g, source, target):
    return any(e["source"] == source and e["target"] == target for e in g["edges"])
