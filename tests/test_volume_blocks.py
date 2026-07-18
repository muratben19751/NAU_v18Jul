"""Volume support regression tests.

User bug: the "Volume-focused strategies ..." hint could not produce
volume-based strategies — blocks only saw `closes`, the codegen contract
explicitly forbade volume.

1. `_eval_volume_spike` unit behavior (edge triggering, dry-up, exit role).
2. volume_spike builtin block opens trades in a real backtest on synthetic
   volume bursts (end-to-end: bar.volume → _volumes → eval → order).
3. Custom block adapter injects `indicators["volumes"]`.
4. Codegen smoke-exec provides a volume series — generated code that reads
   volume passes the smoke test.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from composer import ComposedStrategySpec, SignalBlock, _eval_volume_spike

_RECIPE = {"symbol": "BTCUSDT", "interval": "60", "category": "linear"}


def _fake_strategy(vols: list[float]) -> SimpleNamespace:
    # _volumes is a flat list buffer in the real strategy (perf: deque→list transition)
    return SimpleNamespace(
        _volumes=list(vols),
        _prev_state={},
        _indicators={},
        _buf_cap=len(vols) + 5,
        portfolio=None,
    )


def _block(role: str = "entry", **params) -> SimpleNamespace:
    p = {"period": 10, "mult": 2.0, "direction": "above"}
    p.update(params)
    return SimpleNamespace(params=p, role=role, type="volume_spike")


class TestEvalVolumeSpike:
    def test_spike_fires_long_on_up_bar(self):
        strat = _fake_strategy([100.0] * 10 + [500.0])
        out = _eval_volume_spike(strat, 0, _block(), [100.0, 101.0])
        assert out == "long"

    def test_spike_fires_short_on_down_bar(self):
        strat = _fake_strategy([100.0] * 10 + [500.0])
        out = _eval_volume_spike(strat, 0, _block(), [101.0, 100.0])
        assert out == "short"

    def test_edge_trigger_no_refire_while_sustained(self):
        """Should not re-fire on the second bar while the condition persists."""
        strat = _fake_strategy([100.0] * 10 + [500.0])
        assert _eval_volume_spike(strat, 0, _block(), [100.0, 101.0]) == "long"
        strat._volumes.append(600.0)  # spike continues
        assert _eval_volume_spike(strat, 0, _block(), [101.0, 102.0]) is None

    def test_dry_up_direction_below(self):
        strat = _fake_strategy([100.0] * 10 + [10.0])
        out = _eval_volume_spike(strat, 0, _block(direction="below"), [100.0, 101.0])
        assert out == "long"

    def test_exit_role_returns_exit(self):
        strat = _fake_strategy([100.0] * 10 + [500.0])
        out = _eval_volume_spike(strat, 0, _block(role="exit"), [100.0, 101.0])
        assert out == "exit"

    def test_insufficient_history_returns_none(self):
        strat = _fake_strategy([100.0] * 5)
        assert _eval_volume_spike(strat, 0, _block(), [100.0, 101.0]) is None

    def test_no_fire_on_flat_volume(self):
        strat = _fake_strategy([100.0] * 11)
        assert _eval_volume_spike(strat, 0, _block(), [100.0, 101.0]) is None


def _volume_spike_bars(n: int = 400) -> pd.DataFrame:
    """Trend + sine price, a 10x volume burst every 40 bars."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    t = np.arange(n)
    close = 30_000 + 2_000 * np.sin(t / 30.0) + t * 2.0
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = np.full(n, 100.0)
    volume[::40] = 1_000.0  # periodic spike
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 10,
            "low": np.minimum(open_, close) - 10,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def _volume_spec() -> ComposedStrategySpec:
    return ComposedStrategySpec(
        id="volspike",
        name="Volume Spike E2E",
        description="",
        blocks=[
            SignalBlock(
                type="volume_spike",
                role="entry",
                params={"period": 20, "mult": 2.0, "direction": "above"},
            ),
            SignalBlock(
                type="ma_cross",
                role="exit",
                params={"fast": 5, "slow": 20, "direction": "down"},
            ),
        ],
        trade_size=0.1,
    )


class TestVolumeSpikeBacktest:
    def test_volume_spike_opens_trades(self):
        """bar.volume → _volumes → eval → order chain runs end-to-end."""
        from backtest import run_composed_backtest
        from sandbox import _build_instrument_bar_type

        instrument, bar_type = _build_instrument_bar_type(_RECIPE)
        r = run_composed_backtest(
            _volume_spec(),
            _volume_spike_bars(),
            iteration_id=1,
            rationale="volume regression",
            instrument=instrument,
            bar_type=bar_type,
            venue=instrument.id.venue,
        )
        assert r.error is None, f"volume_spike backtest raised an error: {r.error}"
        m = r.metrics or {}
        assert (m.get("n_trades") or 0) > 0, (
            "no trades were opened — volume spikes did not produce a signal"
        )


class TestCustomAdapterVolumes:
    _CODE = (
        "def evaluate(state, block, closes, indicators, portfolio):\n"
        "    vols = indicators.get('volumes') or []\n"
        "    if len(vols) >= 2 and vols[-1] > vols[-2] * 1.5:\n"
        "        return 'long'\n"
        "    return None\n"
    )

    def test_adapter_injects_volumes(self, tmp_path, monkeypatch):
        """The register_custom_from_disk wrapper should pass the volume series."""
        import custom_block_store as cbs
        from composer import BLOCK_REGISTRY, register_custom_from_disk

        monkeypatch.setattr(cbs, "STORE_DIR", tmp_path)
        monkeypatch.setattr(cbs, "REGISTRY_FILE", tmp_path / "registry.json")
        name = "vol_test_block"
        cbs.save_custom(name, {"label": "Vol Test", "params": {}}, self._CODE)
        try:
            register_custom_from_disk(name)
            entry = BLOCK_REGISTRY[name]
            strat = _fake_strategy([100.0, 300.0])
            block = SimpleNamespace(params={}, role="entry", type=name)
            assert entry["eval"](strat, 0, block, [100.0, 101.0]) == "long"
            # None when there is no volume increase
            strat2 = _fake_strategy([100.0, 100.0])
            assert entry["eval"](strat2, 0, block, [100.0, 101.0]) is None
        finally:
            BLOCK_REGISTRY.pop(name, None)


class TestSmokeExecVolumes:
    def test_generated_volume_code_passes_smoke(self):
        """Generated code that reads volume runs on a real series in smoke-exec."""
        from agent import _test_execute_generated

        src = (
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    vols = indicators.get('volumes') or []\n"
            "    n = int(block.params.get('period', 20))\n"
            "    if len(vols) < n + 1:\n"
            "        return None\n"
            "    avg = sum(vols[-n-1:-1]) / n\n"
            "    if avg > 0 and vols[-1] / avg >= 1.2:\n"
            "        return 'long'\n"
            "    return None\n"
        )
        meta = {
            "label": "Vol Smoke",
            "params": {"period": {"type": "int", "default": 20}},
        }
        # Passes if it does not raise (we do not expect GeneratedCodeError)
        _test_execute_generated(src, meta)
