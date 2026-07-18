"""Performans optimizasyonu sonuç-birebir guard'ları.

deque→list buffer + _current_equity hızlı yolu + fills dict lookup'ının
SONUCU DEĞİŞTİRMEDİĞİNİ sabitler. Altın değerler optimizasyon ÖNCESİ koddan
alındı (perf parite koşusu, sabit seed=42 sentetik veri) — bu test bozulursa
determinizm bozulmuş demektir; optimizasyon geri alınmalı.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from composer import ComposedStrategySpec, SignalBlock

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _synth_bars(n: int) -> pd.DataFrame:
    """perf_parity.py ile birebir aynı üreteç (seed=42)."""
    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    rng = np.random.default_rng(42)
    close = (
        30_000
        + 2_000 * np.sin(t / 30.0)
        + t * 0.5
        + rng.normal(0, 120, n).cumsum() * 0.05
    )
    close = np.maximum(close, 1000)
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = 100 + 50 * np.abs(np.sin(t / 7.0)) + rng.uniform(0, 30, n)
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 15,
            "low": np.minimum(open_, close) - 15,
            "close": close,
            "volume": vol,
        },
        index=idx,
    )


def _run(spec, bars):
    from backtest import run_composed_backtest
    from sandbox import _build_instrument_bar_type

    instrument, bar_type = _build_instrument_bar_type(_RECIPE)
    return run_composed_backtest(
        spec,
        bars,
        iteration_id=0,
        rationale="parity",
        instrument=instrument,
        bar_type=bar_type,
        venue=instrument.id.venue,
    )


def _trades_sha(trades) -> str:
    return hashlib.sha256(
        json.dumps(trades or [], sort_keys=True, default=str).encode()
    ).hexdigest()


class TestGoldenParity:
    """Altın değerler (20k bar, seed=42).

    L13 (2026-07): delay_fill artık ÇIKIŞLARA da uygulanıyor (giriş simetrisi).
    M5 (2026-07): Bybit komisyonları AÇILDI (maker/taker + MakerTakerFeeModel)
    — PnL artık net-komisyon. İki bilinçli davranış değişikliği; altın değerler
    yeniden üretildi.
    """

    def test_ma_cross_golden(self):
        spec = ComposedStrategySpec(
            id="p_ma",
            name="p_ma",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
                SignalBlock(
                    type="ma_cross",
                    role="exit",
                    params={"fast": 5, "slow": 20, "direction": "down"},
                ),
            ],
            trade_size=0.1,
        )
        r = _run(spec, _synth_bars(20_000))
        assert r.error is None
        m = r.metrics
        assert round(m["pnl"], 3) == 40455.724
        assert m["n_trades"] == 106
        assert _trades_sha(r.trades).startswith("be4c21d2e6d8")

    def test_breakout_golden(self):
        spec = ComposedStrategySpec(
            id="p_brk",
            name="p_brk",
            description="",
            blocks=[
                SignalBlock(
                    type="price_breakout",
                    role="entry",
                    params={"lookback": 30, "direction": "high"},
                ),
                SignalBlock(
                    type="price_breakout",
                    role="exit",
                    params={"lookback": 15, "direction": "low"},
                ),
            ],
            trade_size=0.1,
        )
        r = _run(spec, _synth_bars(20_000))
        assert r.error is None
        assert round(r.metrics["pnl"], 3) == 38420.341
        assert r.metrics["n_trades"] == 107
        assert _trades_sha(r.trades).startswith("914415a578d8")

    def test_volume_and_rsi_golden(self):
        spec = ComposedStrategySpec(
            id="p_mom_vol",
            name="p_mom_vol",
            description="",
            blocks=[
                SignalBlock(
                    type="momentum",
                    role="entry",
                    params={"lookback": 12, "sign": "positive"},
                ),
                SignalBlock(
                    type="volume_spike",
                    role="entry",
                    params={"period": 15, "mult": 1.5, "direction": "above"},
                ),
                SignalBlock(
                    type="rsi_threshold",
                    role="exit",
                    params={"period": 14, "threshold": 70.0, "cross": "above"},
                ),
            ],
            trade_size=0.1,
            entry_logic="OR",
        )
        r = _run(spec, _synth_bars(20_000))
        assert r.error is None
        # H6 (2026-07): rsi_threshold artık RSI'yı 0-100 ölçeğine çekiyor —
        # eskiden exit bloğu ÖLÜ koddu (n_trades=1, sona kadar tut); şimdi
        # gerçekten ateşliyor (106 çıkış). Golden yeniden üretildi.
        assert round(r.metrics["pnl"], 3) == 360.482
        assert r.metrics["n_trades"] == 106
        assert _trades_sha(r.trades).startswith("77080483ec85")


class TestCustomAdapterIsolation:
    """Adaptör kullanıcı koduna pencere-KOPYASI vermeli (buffer sızmasın)."""

    def test_window_capped_and_isolated(self, tmp_path, monkeypatch):
        import custom_block_store as cbs
        from composer import BLOCK_REGISTRY, register_custom_from_disk

        monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
        monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
        name = "iso_test_block"
        code = (
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    state['n_closes'] = len(closes)\n"
            "    state['n_vols'] = len(indicators.get('volumes') or [])\n"
            "    closes.append(-1.0)  # mutasyon buffer'a SIZMAMALI\n"
            "    (indicators.get('volumes') or []).append(-1.0)\n"
            "    return None\n"
        )
        cbs.save_custom(name, {"label": "Iso", "params": {}}, code)
        try:
            register_custom_from_disk(name)
            entry = BLOCK_REGISTRY[name]

            from types import SimpleNamespace

            buf_cap = 10
            strat = SimpleNamespace(
                _closes=[float(i) for i in range(50)],  # buffer > cap
                _volumes=[float(i) for i in range(50)],
                _prev_state={},
                _indicators={},
                _buf_cap=buf_cap,
                portfolio=None,
            )
            block = SimpleNamespace(params={}, role="entry", type=name)
            entry["eval"](strat, 0, block, strat._closes)
            state = strat._prev_state["custom_state_0"]
            # Pencere eski deque genişliğiyle sınırlı
            assert state["n_closes"] == buf_cap
            assert state["n_vols"] == buf_cap
            # Mutasyon buffer'lara sızmadı
            assert len(strat._closes) == 50 and strat._closes[-1] == 49.0
            assert len(strat._volumes) == 50 and strat._volumes[-1] == 49.0
        finally:
            BLOCK_REGISTRY.pop(name, None)
