"""Backtest — select a saved strategy, run it, view the results.

The order flow diagram matches the wiki's Order Flow Pipeline page exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from backtest import run_composed_backtest
from composer import BLOCK_CATALOG, load_catalog
from data import load_btc_bars
from wiki_helper import read_wiki_page, wiki_link_md


st.set_page_config(page_title="Backtest", layout="wide")
st.title("🧪 Backtest")
st.caption("Runs a saved strategy on the Nautilus BacktestEngine.")


@st.cache_data(show_spinner="Loading BTC-USD data…")
def cached_bars() -> pd.DataFrame:
    return load_btc_bars()


bars = cached_bars()

catalog = load_catalog()
if not catalog:
    st.warning(
        "No saved strategy yet. Add one from the **Create Strategy** page."
    )
    st.stop()

names = {s.id: f"{s.name}  (id={s.id}, {len(s.blocks)} block)" for s in catalog}
selected_id = st.selectbox(
    "Select strategy", options=list(names.keys()), format_func=lambda k: names[k]
)
spec = next(s for s in catalog if s.id == selected_id)

st.markdown(
    f"**Description:** {spec.description or '_(none)_'}  ·  "
    f"**Trade size:** `{spec.trade_size} BTC`  ·  "
    f"**Created:** `{spec.created_at}`"
)

with st.expander("Signal blocks"):
    for b in spec.blocks:
        st.markdown(
            f"- **{BLOCK_CATALOG[b.type]['label']}** ({b.role}) — "
            f"`{', '.join(f'{k}={v}' for k, v in b.params.items())}`"
        )

run = st.button("▶ Run backtest", type="primary")

if run:
    with st.spinner(f"Running BacktestEngine · {len(bars):,} bars…"):
        result = run_composed_backtest(spec, bars, iteration_id=0, rationale="user-run")
    st.session_state.last_result = result

if "last_result" not in st.session_state:
    st.info("Press the **Run backtest** button.")
    st.stop()

r = st.session_state.last_result

st.divider()
st.subheader("📊 Results")

if r.error:
    st.error(f"Backtest error: {r.error}")
    st.stop()

m = r.metrics
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("PnL ($)", f"{m.get('pnl', 0):,.2f}")
c2.metric("Sharpe (252d)", f"{m.get('sharpe', 0):.2f}")
c3.metric("Trade count", f"{m.get('n_trades', 0)}")
c4.metric("Win rate", f"{m.get('win_rate', 0) * 100:.1f}%")
c5.metric("Max drawdown", f"{m.get('max_dd', 0) * 100:.2f}%")

if r.equity_curve:
    st.subheader("Equity curve")
    st.line_chart(pd.DataFrame({"equity": r.equity_curve}), height=280)

st.divider()

diag, wiki = st.columns([2, 3])

with diag:
    st.subheader("🔀 Order Flow (in this backtest)")
    st.caption("Matches the wiki's Order Flow Pipeline page exactly.")
    st.code(
        "ComposedStrategy.on_bar(bar)\n"
        "        │\n"
        "        ▼\n"
        "  entry/exit signals evaluated\n"
        "        │\n"
        "        ▼\n"
        "  self.order_factory.market(...)\n"
        "        │\n"
        "        ▼\n"
        "  self.submit_order(order)\n"
        "        │\n"
        "        ▼\n"
        "[Order Emulator]   (none — no emulation trigger defined for this strategy)\n"
        "        │\n"
        "        ▼\n"
        "[Execution Algorithm]   (none — no ExecAlgorithmId provided)\n"
        "        │\n"
        "        ▼\n"
        "  RiskEngine          ✅ pre-trade validation\n"
        "        │\n"
        "        ▼\n"
        "  YAHOO venue         → SimulatedExchange fill\n"
        "        │\n"
        "        ▼\n"
        "  ExecutionEngine     → Position tracking, PnL\n",
        language=None,
    )
    st.markdown(
        " ".join(
            [
                wiki_link_md("wiki/concepts/order_flow_pipeline.md"),
                wiki_link_md("wiki/entities/execution_engine.md"),
                wiki_link_md("wiki/entities/risk_engine.md"),
            ]
        )
    )

with wiki:
    st.subheader("📖 Wiki: Order Flow Pipeline")
    with st.container(border=True, height=520):
        st.markdown(read_wiki_page("wiki/concepts/order_flow_pipeline.md"))
