"""composer.py public-surface golden set (composer.py decomposition, Adım 0b —
permanent safety net, not a code move).

Cheapest possible guard against ruff's F401 unused-import autofix silently
deleting a re-export the moment a later decomposition step (block_meta.py,
block_library_classic.py, block_library_nau.py, ...) moves code out of
composer.py: this file already has that exact failure class in its own
history, four separate times, across agent.py's decomposition. A frozen
name set that can only grow, never shrink, turns a silent deletion into an
immediate, obvious test failure instead of a deploy-time AttributeError.

Not scoped to any one extraction step — this stays green (and keeps
protecting) through every future composer.py decomposition step, including
ones this plan defers.

Wiki References
---------------
See: [[webapp_module_map]].
"""

from __future__ import annotations

import composer

# Captured 2026-08-09, before any composer.py extraction step. Every name
# below must remain reachable as composer.<name> forever — extraction steps
# add re-exports, they never remove names from this set. New names (e.g. a
# future block_registry.py's re-exports) are fine and do not need to be
# added here; this is a floor, not a mirror.
_GOLDEN_SET = frozenset(
    {
        "Any",
        "AverageTrueRange",
        "BLOCK_CATALOG",
        "BLOCK_REGISTRY",
        "Bar",
        "BarType",
        "BlockRole",
        "BlockType",
        "BollingerBands",
        "CATALOG_FILE",
        "ComposedStrategy",
        "ComposedStrategyConfig",
        "ComposedStrategySpec",
        "Decimal",
        "EntryExitLogic",
        "ExponentialMovingAverage",
        "InstrumentId",
        "Literal",
        "NAU_WINDOW",
        "OrderSide",
        "OrderType",
        "OrderTypeOpt",
        "Path",
        "RelativeStrengthIndex",
        "SLType",
        "SignalBlock",
        "SimpleNamespace",
        "Strategy",
        "StrategyConfig",
        "TPType",
        "TimeInForce",
        "TradeSizeMode",
        "TriggerType",
        "UTC",
        "_BUILTIN_META",
        "_CATALOG_LOCK",
        "_CATALOG_RAW_CACHE",
        "_NAU_RECURSIVE_BLOCKS",
        "_PortfolioView",
        "_REGISTRY_LOCK",
        "_as_bool",
        "_catalog_block_names",
        "_catalog_is_valid",
        "_eval_adx_threshold",
        "_eval_atr_stop",
        "_eval_bollinger_break",
        "_eval_donchian_channel",
        "_eval_ema_cross",
        "_eval_ma_cross",
        "_eval_macd_cross",
        "_eval_momentum",
        "_eval_price_breakout",
        "_eval_rsi_threshold",
        "_eval_stoch_rsi_cross",
        "_eval_volume_spike",
        "_eval_wave_trend_cross",
        "_lb_adx_threshold",
        "_lb_atr_stop",
        "_lb_bollinger_break",
        "_lb_donchian_channel",
        "_lb_ema_cross",
        "_lb_ma_cross",
        "_lb_macd_cross",
        "_lb_momentum",
        "_lb_price_breakout",
        "_lb_rsi_threshold",
        "_lb_stoch_rsi_cross",
        "_lb_volume_spike",
        "_lb_wave_trend_cross",
        "_load_custom_blocks",
        "_load_module_from_path",
        "_nau_win",
        "_onstart_atr_stop",
        "_onstart_bollinger_break",
        "_onstart_ema_cross",
        "_onstart_macd_cross",
        "_onstart_rsi_threshold",
        "_read_catalog_raw",
        "_rebuild_catalog",
        "_signal_matches_role",
        "_snap_adx_threshold",
        "_snap_atr_stop",
        "_snap_bollinger_break",
        "_snap_donchian_channel",
        "_snap_ema_pair",
        "_snap_ma_cross",
        "_snap_momentum",
        "_snap_price_breakout",
        "_snap_rsi_threshold",
        "_snap_stoch_rsi_cross",
        "_snap_volume_spike",
        "_snap_wave_trend_cross",
        "_validate_atr_stop",
        "_validate_cross_fast_slow",
        "annotations",
        "append_to_catalog",
        "asdict",
        "build_spec",
        "dataclass",
        "datetime",
        "deque",
        "field",
        "json",
        "load_catalog",
        "logging",
        "math",
        "mutate_catalog",
        "new_spec_id",
        "os",
        "register_custom_block",
        "register_custom_from_disk",
        "save_catalog",
        "threading",
        "unregister_custom_block",
        "uuid",
    }
)


def test_every_golden_name_is_still_reachable_on_the_composer_module():
    current = set(dir(composer))
    missing = _GOLDEN_SET - current

    assert not missing, (
        f"{len(missing)} name(s) vanished from composer.<name>: {sorted(missing)} "
        "— likely ruff's F401 autofix silently dropping a re-export before "
        "its consuming code (or __all__ entry) existed in the same edit"
    )
