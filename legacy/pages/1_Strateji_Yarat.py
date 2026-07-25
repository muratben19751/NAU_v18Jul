"""Create Strategy — visual signal-block composer.

The user selects, combines, and saves signal blocks. Saved strategies
are used by both the Backtest page and the autonomous Agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from composer import (
    BLOCK_CATALOG,
    ComposedStrategySpec,
    SignalBlock,
    load_catalog,
    new_spec_id,
    save_catalog,
)
from wiki_helper import read_wiki_page, wiki_link_md


st.set_page_config(page_title="Create Strategy", layout="wide")
st.title("🧩 Create Strategy")
st.caption(
    "Combines signal blocks to produce a Nautilus `Strategy` subclass. Wiki-compatible."
)


if "draft_blocks" not in st.session_state:
    st.session_state.draft_blocks = []
if "wiki_active" not in st.session_state:
    st.session_state.wiki_active = "wiki/entities/strategy_and_actor.md"


main, wiki_col = st.columns([3, 2])


with main:
    st.subheader("1. Strategy header")
    c1, c2 = st.columns(2)
    name = c1.text_input(
        "Name", value="My New Strategy", help="Appears in the saved catalog under this name."
    )
    trade_size = c2.number_input(
        "Trade size (BTC)",
        min_value=0.001,
        max_value=10.0,
        value=0.1,
        step=0.01,
        help="Amount of BTC to buy on each entry signal.",
    )
    description = st.text_area(
        "Description",
        value="",
        height=60,
        help="A short note on what this strategy does.",
    )

    st.divider()

    st.subheader("2. Add signal block")
    st.caption(
        "**Entry** blocks are OR'ed → a position opens if any one triggers. "
        "**Exit** blocks are OR'ed → a position closes if any one triggers. "
        f"{wiki_link_md('wiki/concepts/order_flow_pipeline.md', 'Order Flow')}"
    )

    ca, cb = st.columns(2)
    block_type = ca.selectbox(
        "Block type",
        options=list(BLOCK_CATALOG.keys()),
        format_func=lambda k: BLOCK_CATALOG[k]["label"],
        key="new_block_type",
    )
    block_role = cb.selectbox("Role", options=["entry", "exit"], key="new_block_role")

    spec_meta = BLOCK_CATALOG[block_type]
    st.info(spec_meta["help"])

    param_inputs = {}
    param_cols = st.columns(max(1, len(spec_meta["params"])))
    for i, (pname, pspec) in enumerate(spec_meta["params"].items()):
        col = param_cols[i]
        if pspec["type"] == "int":
            param_inputs[pname] = col.number_input(
                pname,
                min_value=pspec["min"],
                max_value=pspec["max"],
                value=pspec["default"],
                step=1,
                key=f"newp_{pname}",
            )
        elif pspec["type"] == "float":
            param_inputs[pname] = col.number_input(
                pname,
                min_value=float(pspec["min"]),
                max_value=float(pspec["max"]),
                value=float(pspec["default"]),
                step=0.5,
                key=f"newp_{pname}",
            )
        elif pspec["type"] == "enum":
            param_inputs[pname] = col.selectbox(
                pname,
                options=pspec["options"],
                index=pspec["options"].index(pspec["default"]),
                key=f"newp_{pname}",
            )

    add_col, wiki_col2 = st.columns([1, 3])
    if add_col.button("➕ Add block", type="primary"):
        st.session_state.draft_blocks.append(
            SignalBlock(type=block_type, role=block_role, params=dict(param_inputs))
        )
        st.rerun()
    if spec_meta["wiki_refs"]:
        with wiki_col2:
            st.session_state.wiki_active = spec_meta["wiki_refs"][0]
            st.markdown(" ".join(wiki_link_md(r) for r in spec_meta["wiki_refs"]))

    st.divider()

    st.subheader("3. Current block list")
    if not st.session_state.draft_blocks:
        st.info("No blocks added yet. Add one above.")
    else:
        for i, b in enumerate(st.session_state.draft_blocks):
            row = st.container(border=True)
            with row:
                c1, c2, c3, c4 = st.columns([3, 1, 4, 1])
                c1.markdown(f"**{i + 1}. {BLOCK_CATALOG[b.type]['label']}**")
                c2.markdown(f"`{b.role}`")
                c3.code(
                    ", ".join(f"{k}={v}" for k, v in b.params.items()), language=None
                )
                if c4.button("🗑", key=f"del_{i}"):
                    st.session_state.draft_blocks.pop(i)
                    st.rerun()

    st.divider()

    st.subheader("4. Save")
    save_col, clear_col = st.columns([1, 1])
    if save_col.button(
        "💾 Save to Catalog",
        type="primary",
        disabled=not st.session_state.draft_blocks,
    ):
        spec = ComposedStrategySpec(
            id=new_spec_id(),
            name=name.strip() or "unnamed",
            description=description.strip(),
            blocks=list(st.session_state.draft_blocks),
            trade_size=float(trade_size),
        )
        err = spec.validate()
        if err:
            st.error(f"Invalid: {err}")
        else:
            catalog = load_catalog()
            catalog.append(spec)
            save_catalog(catalog)
            st.success(f"✅ Saved: **{spec.name}** (id={spec.id})")
            st.session_state.draft_blocks = []
            st.rerun()
    if clear_col.button("Reset"):
        st.session_state.draft_blocks = []
        st.rerun()

    st.divider()
    st.subheader("5. Saved strategies")
    catalog = load_catalog()
    if not catalog:
        st.info("No saved strategies yet.")
    else:
        for spec in reversed(catalog):
            with st.expander(
                f"📦 {spec.name}  ·  {len(spec.blocks)} block  ·  id={spec.id}"
            ):
                st.write(spec.description or "_(no description)_")
                st.write(
                    f"Created: `{spec.created_at}`  ·  Trade size: `{spec.trade_size} BTC`"
                )
                for b in spec.blocks:
                    st.markdown(
                        f"- **{BLOCK_CATALOG[b.type]['label']}** ({b.role}) — "
                        f"`{', '.join(f'{k}={v}' for k, v in b.params.items())}`"
                    )
                cdel, _ = st.columns([1, 5])
                if cdel.button("🗑 Delete", key=f"del_spec_{spec.id}"):
                    catalog2 = [s for s in load_catalog() if s.id != spec.id]
                    save_catalog(catalog2)
                    st.rerun()


with wiki_col:
    st.subheader("📖 Wiki")
    st.caption("Live wiki content based on the selected block.")
    active = st.session_state.wiki_active
    st.markdown(wiki_link_md(active))
    with st.container(border=True, height=700):
        st.markdown(read_wiki_page(active))
