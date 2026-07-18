"""Streamlit UI for the autonomous Nautilus backtest loop."""

from __future__ import annotations

import threading
import time

import pandas as pd
import streamlit as st

from composer import load_catalog
from data import load_btc_bars
from loop_runner import run_loop
from state import get_state


st.set_page_config(page_title="Nautilus Autonomous Backtest Agent", layout="wide")

st.title("Nautilus Autonomous Backtest Agent")
st.caption(
    "Claude Sonnet 4.6 → strategy suggestion → NautilusTrader backtest → repeat. "
    "Can also run the catalog of saved strategies."
)

with st.sidebar:
    st.markdown("### Pages")
    st.page_link("app.py", label="🏠 Autonomous Agent")
    st.page_link("pages/1_Strateji_Yarat.py", label="🧩 Create Strategy")
    st.page_link("pages/2_Backtest.py", label="🧪 Backtest (single run)")


@st.cache_data(show_spinner="Loading BTC-USD data…")
def cached_bars() -> pd.DataFrame:
    return load_btc_bars()


try:
    bars = cached_bars()
except Exception as e:
    st.error(f"Could not load data: {e}")
    st.stop()

state = get_state()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Bar count", f"{len(bars):,}")
col2.metric("Start", str(bars.index[0].date()))
col3.metric("End", str(bars.index[-1].date()))
col4.metric("Last price", f"${bars['close'].iloc[-1]:,.0f}")

st.divider()

catalog = load_catalog()
mode_options = ["agent"]
if catalog:
    mode_options.append("catalog")

cc1, cc2, cc3, cc4 = st.columns([1, 1, 1, 3])
mode = cc1.selectbox(
    "Mode",
    options=mode_options,
    format_func=lambda m: {
        "agent": "🤖 LLM Agent",
        "catalog": f"📦 Catalog ({len(catalog)})",
    }[m],
    disabled=state.running,
)
start = cc2.button("▶ Start", disabled=state.running, type="primary")
stop = cc3.button("■ Stop", disabled=not state.running)
cc4.write(
    f"**Status:** `{state.last_status}`  •  iteration: **{len(state.iterations)}**"
    + ("  •  🔴 running" if state.running else "")
)

if start and not state.running:
    t = threading.Thread(target=run_loop, args=(state, bars, mode), daemon=True)
    t.start()
    state.thread_started = True
    st.rerun()

if stop and state.running:
    state.stop_requested = True

st.divider()

iters, best, running, status = state.snapshot()

left, right = st.columns([2, 1])

with left:
    st.subheader("Iterations")
    if iters:
        rows = []
        for r in reversed(iters):
            row = {
                "id": r.id,
                "strategy": r.strategy,
                "params": str(r.params),
                "pnl": r.metrics.get("pnl", None) if r.error is None else None,
                "sharpe": r.metrics.get("sharpe", None) if r.error is None else None,
                "trades": r.metrics.get("n_trades", None) if r.error is None else None,
                "win_rate": r.metrics.get("win_rate", None)
                if r.error is None
                else None,
                "max_dd": r.metrics.get("max_dd", None) if r.error is None else None,
                "error": r.error,
                "time": r.timestamp.strftime("%H:%M:%S"),
            }
            rows.append(row)
        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("No iterations yet. Press the **Start** button.")

with right:
    st.subheader("Best")
    if best is not None:
        st.metric("PnL ($)", f"{best.metrics.get('pnl', 0):,.0f}")
        st.metric("Sharpe", f"{best.metrics.get('sharpe', 0):.2f}")
        st.metric("Trade count", f"{best.metrics.get('n_trades', 0)}")
        st.metric("Win rate", f"{best.metrics.get('win_rate', 0) * 100:.1f}%")
        st.metric("Max drawdown", f"{best.metrics.get('max_dd', 0) * 100:.1f}%")
        st.write(f"**Strategy:** `{best.strategy}`")
        st.write(f"**Parameters:** `{best.params}`")
        if best.rationale:
            with st.expander("Agent's rationale"):
                st.write(best.rationale)
    else:
        st.write("_No successful iteration yet._")

if best is not None and best.equity_curve:
    st.subheader("Best strategy's equity curve")
    ec = pd.DataFrame({"equity": best.equity_curve})
    st.line_chart(ec, height=300)

if running:
    time.sleep(2)
    st.rerun()
