"""OHLC (high/low) block access tests.

The user wanted high/low-requiring indicators like ADX/ATR/WaveTrend;
blocks only saw closes+volumes. These tests pin that the high/low series
reach custom blocks (isolated, aligned with closes) and that a real
OHLC-based custom block opens a trade end-to-end in a backtest.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from composer import ComposedStrategySpec, SignalBlock

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


class TestAdapterExposesHighLow:
    """register_custom_from_disk wrapper passes highs/lows through isolated."""

    _CODE = (
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    hi = indicators.get('highs') or []\n"
        "    lo = indicators.get('lows') or []\n"
        "    state['n_hi'] = len(hi)\n"
        "    state['n_lo'] = len(lo)\n"
        "    state['hi_last'] = hi[-1] if hi else None\n"
        "    state['lo_last'] = lo[-1] if lo else None\n"
        "    hi.append(-999.0)  # mutation must NOT LEAK into the buffer\n"
        "    lo.append(-999.0)\n"
        "    return None\n"
    )

    def test_highs_lows_aligned_and_isolated(self, tmp_path, monkeypatch):
        import custom_block_store as cbs
        from composer import BLOCK_REGISTRY, register_custom_from_disk

        monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
        monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
        name = "ohlc_probe_block"
        cbs.save_custom(name, {"label": "OHLC Probe", "params": {}}, self._CODE)
        try:
            register_custom_from_disk(name)
            entry = BLOCK_REGISTRY[name]
            buf_cap = 8
            strat = SimpleNamespace(
                _closes=[100.0 + i for i in range(40)],
                _volumes=[1.0] * 40,
                _highs=[100.5 + i for i in range(40)],
                _lows=[99.5 + i for i in range(40)],
                _prev_state={},
                _indicators={},
                _buf_cap=buf_cap,
                portfolio=None,
            )
            block = SimpleNamespace(params={}, role="entry", type=name)
            entry["eval"](strat, 0, block, strat._closes)
            st = strat._prev_state["custom_state_0"]
            # Window bounded by buf_cap, high>close>low aligned
            assert st["n_hi"] == buf_cap and st["n_lo"] == buf_cap
            assert st["hi_last"] == 139.5 and st["lo_last"] == 138.5
            # Mutation did not leak into the real buffers
            assert strat._highs[-1] == 139.5 and len(strat._highs) == 40
            assert strat._lows[-1] == 138.5 and len(strat._lows) == 40
        finally:
            BLOCK_REGISTRY.pop(name, None)


def _breakout_bars(n: int = 300) -> pd.DataFrame:
    """Trend + pronounced intra-bar range: produces a real high/low breakout."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    close = 30_000 + 3_000 * np.sin(t / 40.0) + t * 3.0
    open_ = np.concatenate([[close[0]], close[:-1]])
    # Intra-bar swing bigger than closes → high/low NOT equal to closes
    high = np.maximum(open_, close) + 80 + 40 * np.abs(np.sin(t / 5.0))
    low = np.minimum(open_, close) - 80 - 40 * np.abs(np.cos(t / 5.0))
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


class TestOhlcBlockBacktest:
    """Real OHLC custom block (Stochastic %K — requires both high and low) e2e.

    Stochastic cannot be computed from closes alone (the highest-high /
    lowest-low window needs high/low) — opening a trade is proof that both
    series reach the block with real values.
    """

    _STOCH = (
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    highs = indicators.get('highs') or []\n"
        "    lows = indicators.get('lows') or []\n"
        "    n = int(block.params.get('period', 14))\n"
        "    if len(highs) < n or len(lows) < n:\n"
        "        return None\n"
        "    hh = max(highs[-n:]); ll = min(lows[-n:])\n"
        "    if hh - ll <= 0:\n"
        "        return None\n"
        "    k = (closes[-1] - ll) / (hh - ll) * 100.0\n"
        "    if k < 25.0:\n"
        "        return 'long'\n"
        "    if k > 75.0:\n"
        "        return 'exit'\n"
        "    return None\n"
    )

    def test_stochastic_ohlc_opens_trades(self, tmp_path, monkeypatch):
        import custom_block_store as cbs
        from backtest import run_composed_backtest
        from composer import BLOCK_REGISTRY, register_custom_from_disk
        from sandbox import _build_instrument_bar_type

        monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
        monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
        name = "stoch_k"
        cbs.save_custom(
            name,
            {"label": "Stoch %K", "params": {"period": {"type": "int", "default": 14}}},
            self._STOCH,
        )
        try:
            register_custom_from_disk(name)
            spec = ComposedStrategySpec(
                id="ohlc1",
                name="OHLC Stochastic E2E",
                description="",
                blocks=[
                    SignalBlock(type=name, role="entry", params={"period": 14}),
                    SignalBlock(type=name, role="exit", params={"period": 14}),
                ],
                trade_size=0.1,
            )
            instrument, bar_type = _build_instrument_bar_type(_RECIPE)
            r = run_composed_backtest(
                spec,
                _breakout_bars(),
                iteration_id=1,
                rationale="ohlc e2e",
                instrument=instrument,
                bar_type=bar_type,
                venue=instrument.id.venue,
            )
            assert r.error is None, f"OHLC block backtest error: {r.error}"
            assert (r.metrics or {}).get("n_trades", 0) > 0, (
                "Stochastic opened no trades — high/low may not be reaching the block"
            )
        finally:
            BLOCK_REGISTRY.pop(name, None)
