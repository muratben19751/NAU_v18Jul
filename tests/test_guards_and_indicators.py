"""D set regressions: H8 runtime injection, M25 loop budget,
M27/M33 indicators.py integration + new builtin blocks, L14 bollinger mode,
L25 block isolation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

MATH_BLOCK = """\
def max_lookback(params):
    return 30

def evaluate(state, block, closes, indicators, portfolio):
    if len(closes) < 25:
        return None
    m = statistics.mean(closes[-20:])
    s = math.sqrt(max(0.0, closes[-1]))
    if closes[-1] > m and s > 0:
        return "long"
    return None
"""

IND_BLOCK = """\
def max_lookback(params):
    return 60

def evaluate(state, block, closes, indicators, portfolio):
    if len(closes) < 40:
        return None
    rsi = ind.calc_rsi(closes, 14)
    if rsi < 40:
        return "long"
    if rsi > 60:
        return "short"
    return None
"""

LOOP_BLOCK = """\
def max_lookback(params):
    return 10

def evaluate(state, block, closes, indicators, portfolio):
    x = 0
    while x >= 0:
        x = x + 1
    return None
"""

# Infinite loop inside a HELPER — deep-review fix: the budget reset must only
# be at the start of evaluate(). Previously it was placed at the start of EVERY
# function, so a helper called from inside a loop refreshed the budget on every
# iteration and renewed the backstop (the budget never went <0 → the worker hung).
HELPER_LOOP_BLOCK = """\
def max_lookback(params):
    return 10

def spin(n):
    x = 0
    while x >= 0:
        x = x + 1
    return x

def evaluate(state, block, closes, indicators, portfolio):
    return spin(1)
"""


def _load(tmp_path: Path, name: str, src: str):
    from composer import _load_module_from_path

    p = tmp_path / f"{name}.py"
    p.write_text(src, encoding="utf-8")
    return _load_module_from_path(name, p)


CLOSES = [100.0 + i * 0.1 for i in range(300)]
INDICATORS = {
    "volumes": [1000.0] * 300,
    "highs": [c + 0.5 for c in CLOSES],
    "lows": [c - 0.5 for c in CLOSES],
}


class TestRuntimeInjection:
    """H8: the loader must do the same injection as the smoke environment."""

    def test_math_statistics_available_at_runtime(self, tmp_path):
        mod = _load(tmp_path, "t_math", MATH_BLOCK)
        # Previously: NameError: name 'math' is not defined (silent no-op).
        out = mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)
        assert out in (None, "long", "short", "exit")

    def test_ind_library_available_at_runtime(self, tmp_path):
        mod = _load(tmp_path, "t_ind", IND_BLOCK)
        out = mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)
        assert out in (None, "long", "short", "exit")

    def test_existing_catalog_math_blocks_no_nameerror(self):
        """Real catalog blocks using math/statistics must now run."""
        import custom_block_store as cbs
        from composer import _load_module_from_path

        checked = 0
        for info in cbs.list_custom():
            name = info["name"]
            path = cbs.module_path(name)
            try:
                src = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "math." not in src and "statistics." not in src:
                continue
            mod = _load_module_from_path(name, path)
            defaults = {
                k: v.get("default")
                for k, v in (info["meta"].get("params") or {}).items()
                if isinstance(v, dict)
            }
            out = mod.evaluate(
                {}, SimpleNamespace(params=defaults), CLOSES, dict(INDICATORS), None
            )
            assert out in (None, "long", "short", "exit"), name
            checked += 1
        assert checked >= 1, "no math-using catalog block found"


class TestLoopBudget:
    """M25: an infinite loop must raise RuntimeError instead of leaking a thread."""

    def test_infinite_loop_trips_budget(self, tmp_path):
        mod = _load(tmp_path, "t_loop", LOOP_BLOCK)
        with pytest.raises(RuntimeError, match="loop budget exceeded"):
            mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)

    def test_infinite_loop_in_helper_trips_budget(self, tmp_path):
        # The budget reset is only at the start of evaluate() — an infinite loop
        # inside a helper must consume the shared budget and be caught (the helper
        # does NOT reset on each call).
        mod = _load(tmp_path, "t_helper_loop", HELPER_LOOP_BLOCK)
        with pytest.raises(RuntimeError, match="loop budget exceeded"):
            mod.evaluate({}, SimpleNamespace(params={}), CLOSES, INDICATORS, None)

    def test_smoke_rejects_infinite_loop(self):
        from agent import _test_execute_generated
        from codegate import GeneratedCodeError

        with pytest.raises(GeneratedCodeError):
            _test_execute_generated(LOOP_BLOCK)

    def test_smoke_requires_max_lookback_for_new_blocks(self):
        from agent import _test_execute_generated
        from codegate import GeneratedCodeError

        src = (
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    return None\n"
        )
        # Old blocks (require=False) pass…
        _test_execute_generated(src)
        # …mandatory in new generation (M16).
        with pytest.raises(GeneratedCodeError, match="max_lookback"):
            _test_execute_generated(src, require_max_lookback=True)


def _fake_strategy(closes, highs=None, lows=None):
    return SimpleNamespace(
        _highs=highs or [c + 0.5 for c in closes],
        _lows=lows or [c - 0.5 for c in closes],
        _prev_state={},
        _indicators={},
        _volumes=[1000.0] * len(closes),
    )


class TestNewBuiltinBlocks:
    """M27: 4 new builtins built on top of the NAU parity library."""

    def test_registered_as_builtin(self):
        from composer import BLOCK_REGISTRY

        for name in (
            "adx_threshold",
            "stoch_rsi_cross",
            "wave_trend_cross",
            "donchian_channel",
        ):
            assert name in BLOCK_REGISTRY, name
            assert BLOCK_REGISTRY[name]["builtin"] is True

    def test_adx_parity_with_ind(self):
        import indicators as ind
        from composer import _eval_adx_threshold

        closes = [100 + ((i * 7) % 13) - 6 + i * 0.05 for i in range(120)]
        strat = _fake_strategy(closes)
        block = SimpleNamespace(
            params={"period": 14, "threshold": 0.0}, role="entry", type="adx_threshold"
        )
        out = _eval_adx_threshold(strat, 0, block, closes)
        res = ind.calc_adx(strat._highs, strat._lows, closes, 14)
        assert res is not None
        expected = "long" if res["plusDI"] > res["minusDI"] else "short"
        assert out == expected  # threshold=0 → direction exactly from calc_adx

    def test_donchian_breakout_and_revert(self):
        from composer import _eval_donchian_channel

        closes = [100.0] * 30 + [105.0]  # last bar breaks above the previous 30 bars
        strat = _fake_strategy(closes)
        blk = SimpleNamespace(
            params={"period": 20, "mode": "breakout"},
            role="entry",
            type="donchian_channel",
        )
        assert _eval_donchian_channel(strat, 0, blk, closes) == "long"
        blk.params["mode"] = "revert"
        assert _eval_donchian_channel(strat, 0, blk, closes) == "short"

    def test_wave_trend_and_stoch_smoke(self):
        from composer import _eval_stoch_rsi_cross, _eval_wave_trend_cross

        closes = [100 + ((i * 11) % 17) - 8 + i * 0.02 for i in range(200)]
        strat = _fake_strategy(closes)
        for fn, typ in (
            (_eval_stoch_rsi_cross, "stoch_rsi_cross"),
            (_eval_wave_trend_cross, "wave_trend_cross"),
        ):
            blk = SimpleNamespace(params={}, role="entry", type=typ)
            # Two bars in a row: the first fills prev-state, the second looks for a cross.
            fn(strat, 0, blk, closes[:-1])
            out = fn(strat, 0, blk, closes)
            assert out in (None, "long", "short")


class TestBollingerMode:
    """L14: legacy default is preserved; breakout/revert can produce short."""

    def _strat_with_bb(self, upper, lower):
        bb = SimpleNamespace(initialized=True, upper=upper, lower=lower)
        return SimpleNamespace(_indicators={0: {"bb": bb}}, _prev_state={})

    def test_legacy_default_both_bands_long(self):
        from composer import _eval_bollinger_break

        strat = self._strat_with_bb(upper=110.0, lower=90.0)
        up = SimpleNamespace(params={"side": "upper"}, role="entry", type="b")
        dn = SimpleNamespace(params={"side": "lower"}, role="entry", type="b")
        assert _eval_bollinger_break(strat, 0, up, [111.0]) == "long"
        assert _eval_bollinger_break(strat, 0, dn, [89.0]) == "long"

    def test_breakout_and_revert_sides(self):
        from composer import _eval_bollinger_break

        strat = self._strat_with_bb(upper=110.0, lower=90.0)
        up_b = SimpleNamespace(
            params={"side": "upper", "mode": "breakout"}, role="entry", type="b"
        )
        dn_b = SimpleNamespace(
            params={"side": "lower", "mode": "breakout"}, role="entry", type="b"
        )
        up_r = SimpleNamespace(
            params={"side": "upper", "mode": "revert"}, role="entry", type="b"
        )
        assert _eval_bollinger_break(strat, 0, up_b, [111.0]) == "long"
        assert _eval_bollinger_break(strat, 0, dn_b, [89.0]) == "short"
        assert _eval_bollinger_break(strat, 0, up_r, [111.0]) == "short"


class TestBlockIsolation:
    """L25: a custom block must not be able to mutate the spec's live params dict."""

    def test_params_mutation_stays_in_copy(self, tmp_path):
        from composer import (
            BLOCK_REGISTRY,
            register_custom_from_disk,  # noqa: F401 (registration path)
        )

        src = (
            "def max_lookback(params):\n"
            "    return 10\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            '    block.params.update({"period": 999})\n'
            "    return None\n"
        )
        import custom_block_store as cbs

        name = "t_isolation_x"
        meta = {"label": "t", "params": {"period": {"type": "int", "default": 14}}}
        cbs.save_custom(name, meta, src)
        try:
            register_custom_from_disk(name)
            entry = BLOCK_REGISTRY[name]
            real_block = SimpleNamespace(params={"period": 14}, role="entry", type=name)
            strat = _fake_strategy(CLOSES)
            strat._buf_cap = 60
            strat.portfolio = SimpleNamespace(
                is_net_long=lambda _i: False,
                is_net_short=lambda _i: False,
                is_flat=lambda _i: True,
            )
            strat._iid = lambda: "X"
            entry["eval"](strat, 0, real_block, CLOSES)
            # The real spec params MUST NOT change (the copy was mutated).
            assert real_block.params["period"] == 14
        finally:
            from composer import unregister_custom_block

            unregister_custom_block(name)
            try:
                cbs.delete_custom(name)
            except Exception:
                pass
