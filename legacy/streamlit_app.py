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


st.set_page_config(page_title="Nautilus Otonom Backtest Ajanı", layout="wide")

st.title("Nautilus Otonom Backtest Ajanı")
st.caption(
    "Claude Sonnet 4.6 → strateji önerisi → NautilusTrader backtest → tekrar. "
    "Kaydedilmiş stratejilerin katalogunu da çalıştırabilir."
)

with st.sidebar:
    st.markdown("### Sayfalar")
    st.page_link("app.py", label="🏠 Otonom Ajan")
    st.page_link("pages/1_Strateji_Yarat.py", label="🧩 Strateji Yarat")
    st.page_link("pages/2_Backtest.py", label="🧪 Backtest (tek koşu)")


@st.cache_data(show_spinner="BTC-USD verisi yükleniyor…")
def cached_bars() -> pd.DataFrame:
    return load_btc_bars()


try:
    bars = cached_bars()
except Exception as e:
    st.error(f"Veri yüklenemedi: {e}")
    st.stop()

state = get_state()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Bar sayısı", f"{len(bars):,}")
col2.metric("Başlangıç", str(bars.index[0].date()))
col3.metric("Bitiş", str(bars.index[-1].date()))
col4.metric("Son fiyat", f"${bars['close'].iloc[-1]:,.0f}")

st.divider()

catalog = load_catalog()
mode_options = ["agent"]
if catalog:
    mode_options.append("catalog")

cc1, cc2, cc3, cc4 = st.columns([1, 1, 1, 3])
mode = cc1.selectbox(
    "Mod",
    options=mode_options,
    format_func=lambda m: {
        "agent": "🤖 LLM Ajan",
        "catalog": f"📦 Katalog ({len(catalog)})",
    }[m],
    disabled=state.running,
)
start = cc2.button("▶ Başlat", disabled=state.running, type="primary")
stop = cc3.button("■ Durdur", disabled=not state.running)
cc4.write(
    f"**Durum:** `{state.last_status}`  •  iterasyon: **{len(state.iterations)}**"
    + ("  •  🔴 çalışıyor" if state.running else "")
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
    st.subheader("İterasyonlar")
    if iters:
        rows = []
        for r in reversed(iters):
            row = {
                "id": r.id,
                "strateji": r.strategy,
                "params": str(r.params),
                "pnl": r.metrics.get("pnl", None) if r.error is None else None,
                "sharpe": r.metrics.get("sharpe", None) if r.error is None else None,
                "trades": r.metrics.get("n_trades", None) if r.error is None else None,
                "win_rate": r.metrics.get("win_rate", None)
                if r.error is None
                else None,
                "max_dd": r.metrics.get("max_dd", None) if r.error is None else None,
                "hata": r.error,
                "zaman": r.timestamp.strftime("%H:%M:%S"),
            }
            rows.append(row)
        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("Henüz iterasyon yok. **Başlat** butonuna basın.")

with right:
    st.subheader("En iyi")
    if best is not None:
        st.metric("PnL ($)", f"{best.metrics.get('pnl', 0):,.0f}")
        st.metric("Sharpe", f"{best.metrics.get('sharpe', 0):.2f}")
        st.metric("İşlem sayısı", f"{best.metrics.get('n_trades', 0)}")
        st.metric("Win rate", f"{best.metrics.get('win_rate', 0) * 100:.1f}%")
        st.metric("Max drawdown", f"{best.metrics.get('max_dd', 0) * 100:.1f}%")
        st.write(f"**Strateji:** `{best.strategy}`")
        st.write(f"**Parametreler:** `{best.params}`")
        if best.rationale:
            with st.expander("Ajanın gerekçesi"):
                st.write(best.rationale)
    else:
        st.write("_Henüz başarılı iterasyon yok._")

if best is not None and best.equity_curve:
    st.subheader("En iyi stratejinin equity eğrisi")
    ec = pd.DataFrame({"equity": best.equity_curve})
    st.line_chart(ec, height=300)

if running:
    time.sleep(2)
    st.rerun()
