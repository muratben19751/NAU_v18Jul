"""Visual signal-block composer for Nautilus strategies.

A `ComposedStrategy` is a Nautilus `Strategy` subclass whose behavior is
determined by a list of `SignalBlock` records — no dynamic code, safe by
construction. Each block emits +1 (long entry), -1 (long exit), or 0 on
each bar. Signals are OR-combined: any entry block firing opens a long;
any exit block firing closes it.

Aligned with Nautilus wiki (Strategy & Actor + Order Flow Pipeline):
- on_bar callback drives all logic
- self.order_factory.market → self.submit_order (default Risk Engine route)
- self.close_all_positions on exit signal

Wiki References
---------------
See: [[strategy_and_actor]], [[order_flow_pipeline]], [[nau_deepr_toplu_sertlestirme_2026_08]]

Blocks emit signals; the composer wires them into a Nautilus `Strategy`. Order submission enters exactly into [[order_flow_pipeline]] (`submit_order` → OrderEmulator/ExecutionAlgorithms/RiskEngine/Adapter).

`_current_equity`'s constant (`STARTING_CASH`) fallback used to be silent and
uncached (2026-08-08 DeepR finding); it now logs once and caches into
`_equity_mode="constant"` so the rest of the run short-circuits past both
failing real-data paths instead of retrying them every candle.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections import deque
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nautilus_trader.indicators import (
    AverageTrueRange,
)
from nautilus_trader.model import (
    Bar,
    BarType,
    InstrumentId,
)
from nautilus_trader.model.enums import (
    OrderSide,
    OrderType,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

# --------------------------------------------------------------------------
# Built-in block metadata (was BLOCK_CATALOG; now part of BLOCK_REGISTRY meta)
# and built-in eval / lookback / on_start functions. Each function takes
# `strategy` (ComposedStrategy) as first arg so it can read indicators /
# _prev_state / portfolio. Keeps behavior identical to prior monolithic
# _eval_block/on_start.
#
# composer.py decomposition, safe-first slice — Adım 1: _BUILTIN_META
# extracted to block_meta.py. Adım 2: the 9 classic blocks' eval/snapshot/
# on_start/lookback/validate functions extracted to block_library_classic.py.
# Adım 3 (last step this session): NAU_WINDOW/_NAU_RECURSIVE_BLOCKS/_nau_win
# + the 4 NAU-parity blocks' eval/snapshot/lookback functions extracted to
# block_library_nau.py (_nau_win itself has zero consumers outside that
# file, so it is not re-exported). All three re-imported below (ruff's
# isort sorts them alphabetically by module name, not Adım order) —
# BLOCK_REGISTRY, further down, still reads the eval/snapshot/lookback/
# validate names, and ComposedStrategy.__init__ (further down still) reads
# NAU_WINDOW/_NAU_RECURSIVE_BLOCKS directly for its H1940 buffer-sizing fix.
# Both BLOCK_REGISTRY and ComposedStrategy stay in this file — the registry
# core is out of this session's scope, and ComposedStrategy/
# ComposedStrategyConfig never move (see the decomposition plan's
# critical-decision section).
from block_library_classic import (  # noqa: E402
    _eval_atr_stop,
    _eval_bollinger_break,
    _eval_ema_cross,
    _eval_ma_cross,
    _eval_macd_cross,
    _eval_momentum,
    _eval_price_breakout,
    _eval_rsi_threshold,
    _eval_volume_spike,
    _lb_atr_stop,
    _lb_bollinger_break,
    _lb_ema_cross,
    _lb_ma_cross,
    _lb_macd_cross,
    _lb_momentum,
    _lb_price_breakout,
    _lb_rsi_threshold,
    _lb_volume_spike,
    _onstart_atr_stop,
    _onstart_bollinger_break,
    _onstart_ema_cross,
    _onstart_macd_cross,
    _onstart_rsi_threshold,
    _snap_atr_stop,
    _snap_bollinger_break,
    _snap_ema_pair,
    _snap_ma_cross,
    _snap_momentum,
    _snap_price_breakout,
    _snap_rsi_threshold,
    _snap_volume_spike,
    _validate_atr_stop,
    _validate_cross_fast_slow,
)
from block_library_nau import (  # noqa: E402
    _NAU_RECURSIVE_BLOCKS,
    NAU_WINDOW,
    _eval_adx_threshold,
    _eval_donchian_channel,
    _eval_stoch_rsi_cross,
    _eval_wave_trend_cross,
    _lb_adx_threshold,
    _lb_donchian_channel,
    _lb_stoch_rsi_cross,
    _lb_wave_trend_cross,
    _snap_adx_threshold,
    _snap_donchian_channel,
    _snap_stoch_rsi_cross,
    _snap_wave_trend_cross,
)
from block_meta import _BUILTIN_META  # noqa: E402

# Adım 4 (Faz 2): the block-role type vocabulary (7 Literal aliases),
# _signal_matches_role, SignalBlock, ComposedStrategySpec, _as_bool, and
# build_spec (the spec-upsert service) extracted to composer_spec.py.
# build_spec/new_spec_id have zero in-file consumers left (only external
# callers -- strategy.py/backtest.py -- and the golden-set test reach
# them), so they need __all__ protection here for the first time in this
# file's decomposition -- ruff's F401 silently drops a re-export the
# moment nothing in-file references it, same failure class documented in
# agent.py's own __all__ (see [[otofix_kancasi_yarim_kalan_importu_siler]]).
from composer_spec import (  # noqa: E402
    ComposedStrategySpec,
    SignalBlock,
    _signal_matches_role,
    build_spec,
    new_spec_id,
)

__all__ = [
    "build_spec",
    "new_spec_id",
]

# --------------------------------------------------------------------------
# BLOCK_REGISTRY — the single source of truth for block behavior.
# Each entry: { meta, eval, on_start, max_lookback, validate, builtin }
# Custom blocks are added by `_load_custom_blocks()` at import time and via
# `register_custom_block()` at runtime.
BLOCK_REGISTRY: dict[str, dict[str, Any]] = {
    "ma_cross": {
        "meta": _BUILTIN_META["ma_cross"],
        "eval": _eval_ma_cross,
        "snapshot": _snap_ma_cross,
        "on_start": None,
        "max_lookback": _lb_ma_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "rsi_threshold": {
        "meta": _BUILTIN_META["rsi_threshold"],
        "eval": _eval_rsi_threshold,
        "snapshot": _snap_rsi_threshold,
        "on_start": _onstart_rsi_threshold,
        "max_lookback": _lb_rsi_threshold,
        "validate": None,
        "builtin": True,
    },
    "price_breakout": {
        "meta": _BUILTIN_META["price_breakout"],
        "eval": _eval_price_breakout,
        "snapshot": _snap_price_breakout,
        "on_start": None,
        "max_lookback": _lb_price_breakout,
        "validate": None,
        "builtin": True,
    },
    "momentum": {
        "meta": _BUILTIN_META["momentum"],
        "eval": _eval_momentum,
        "snapshot": _snap_momentum,
        "on_start": None,
        "max_lookback": _lb_momentum,
        "validate": None,
        "builtin": True,
    },
    "volume_spike": {
        "meta": _BUILTIN_META["volume_spike"],
        "eval": _eval_volume_spike,
        "snapshot": _snap_volume_spike,
        "on_start": None,
        "max_lookback": _lb_volume_spike,
        "validate": None,
        "builtin": True,
    },
    "ema_cross": {
        "meta": _BUILTIN_META["ema_cross"],
        "eval": _eval_ema_cross,
        "snapshot": _snap_ema_pair,
        "on_start": _onstart_ema_cross,
        "max_lookback": _lb_ema_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "bollinger_break": {
        "meta": _BUILTIN_META["bollinger_break"],
        "eval": _eval_bollinger_break,
        "snapshot": _snap_bollinger_break,
        "on_start": _onstart_bollinger_break,
        "max_lookback": _lb_bollinger_break,
        "validate": None,
        "builtin": True,
    },
    "macd_cross": {
        "meta": _BUILTIN_META["macd_cross"],
        "eval": _eval_macd_cross,
        "snapshot": _snap_ema_pair,
        "on_start": _onstart_macd_cross,
        "max_lookback": _lb_macd_cross,
        "validate": _validate_cross_fast_slow,
        "builtin": True,
    },
    "atr_stop": {
        "meta": _BUILTIN_META["atr_stop"],
        "eval": _eval_atr_stop,
        "snapshot": _snap_atr_stop,
        "on_start": _onstart_atr_stop,
        "max_lookback": _lb_atr_stop,
        "validate": _validate_atr_stop,
        "builtin": True,
    },
    "adx_threshold": {
        "meta": _BUILTIN_META["adx_threshold"],
        "eval": _eval_adx_threshold,
        "snapshot": _snap_adx_threshold,
        "on_start": None,
        "max_lookback": _lb_adx_threshold,
        "validate": None,
        "builtin": True,
    },
    "stoch_rsi_cross": {
        "meta": _BUILTIN_META["stoch_rsi_cross"],
        "eval": _eval_stoch_rsi_cross,
        "snapshot": _snap_stoch_rsi_cross,
        "on_start": None,
        "max_lookback": _lb_stoch_rsi_cross,
        "validate": None,
        "builtin": True,
    },
    "wave_trend_cross": {
        "meta": _BUILTIN_META["wave_trend_cross"],
        "eval": _eval_wave_trend_cross,
        "snapshot": _snap_wave_trend_cross,
        "on_start": None,
        "max_lookback": _lb_wave_trend_cross,
        "validate": None,
        "builtin": True,
    },
    "donchian_channel": {
        "meta": _BUILTIN_META["donchian_channel"],
        "eval": _eval_donchian_channel,
        "snapshot": _snap_donchian_channel,
        "on_start": None,
        "max_lookback": _lb_donchian_channel,
        "validate": None,
        "builtin": True,
    },
}


# BLOCK_CATALOG — meta-only view of BLOCK_REGISTRY. Kept as a plain dict for
# template compatibility (iteration, .items(), [key]). Rebuilt whenever the
# registry changes.

BLOCK_CATALOG: dict[str, dict] = {}


def _rebuild_catalog() -> None:
    # Build the new dict first, then apply it with a single clear+update — narrows
    # the window in which a lock-free reader sees an empty/half catalog (or
    # 'changed size during iteration'). The dict IDENTITY is preserved (template refs).
    new = {k: entry["meta"] for k, entry in BLOCK_REGISTRY.items()}
    BLOCK_CATALOG.clear()
    BLOCK_CATALOG.update(new)


_REGISTRY_LOCK = threading.Lock()
_rebuild_catalog()


class _PortfolioView:
    """L25: minimal Portfolio view passed to custom blocks.

    No existing block uses portfolio in block scanning; the three whitelisted
    queries are exposed as passthroughs (when called without arguments the
    strategy's own instrument is assumed). The real Portfolio's mutation surface
    stays closed to blocks.
    """

    __slots__ = ("_strategy",)

    def __init__(self, strategy) -> None:
        self._strategy = strategy

    def _resolve(self, instrument_id):
        return instrument_id if instrument_id is not None else self._strategy._iid()

    def is_net_long(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_net_long(self._resolve(instrument_id))

    def is_net_short(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_net_short(self._resolve(instrument_id))

    def is_flat(self, instrument_id=None) -> bool:
        return self._strategy.portfolio.is_flat(self._resolve(instrument_id))


def register_custom_block(name: str, entry: dict[str, Any]) -> None:
    """Register a custom block at runtime. `entry` must have keys:
    meta, eval, and optionally on_start, max_lookback, validate.
    """
    required = {"meta", "eval"}
    missing = required - set(entry.keys())
    if missing:
        raise ValueError(f"custom block '{name}' missing keys: {missing}")
    with _REGISTRY_LOCK:
        if name in BLOCK_REGISTRY and BLOCK_REGISTRY[name].get("builtin"):
            raise ValueError(f"cannot override built-in block '{name}'")
        BLOCK_REGISTRY[name] = {
            "meta": entry["meta"],
            "eval": entry["eval"],
            "on_start": entry.get("on_start"),
            "max_lookback": entry.get("max_lookback") or (lambda params: 50),
            "validate": entry.get("validate"),
            "builtin": False,
        }
        _rebuild_catalog()


def unregister_custom_block(name: str) -> None:
    with _REGISTRY_LOCK:
        if name in BLOCK_REGISTRY and not BLOCK_REGISTRY[name].get("builtin"):
            del BLOCK_REGISTRY[name]
            _rebuild_catalog()


def _load_module_from_path(name: str, path: Path):
    """Import a Python file at `path` under module name `name` without sys.path.

    Re-validates the source through ``codegate`` BEFORE executing it. Generation
    time already validates, but a stored ``.py`` could have been hand-edited or
    corrupted on disk; without this check ``exec_module`` would run arbitrary
    code with full privileges at every server startup. ``codegate`` imports only
    ``ast`` so this stays cheap and pulls in no heavy deps.
    """
    import importlib.util
    import math as _math
    import statistics as _statistics

    import indicators as _ind_mod
    from codegate import (
        GeneratedCodeError,
        compile_with_loop_budget,
        safe_builtins,
        safe_module_proxy,
        validate_generated_code,
    )

    src = path.read_text(encoding="utf-8")
    try:
        validate_generated_code(src)
    except GeneratedCodeError as e:
        raise ImportError(f"custom block {name!r} failed AST re-validation: {e}") from e

    spec = importlib.util.spec_from_file_location(
        f"nautilus_custom_blocks.{name}", str(path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # H8: PARITY with the generation smoke environment — the smoke exec injected
    # math/statistics while the loader did not; blocks using math.* silently no-op'd
    # every candle with a NameError (validated live). `ind` = the NAU parity
    # library (indicators.py, M27/M33).
    # Injected as read-only proxies (codegate.safe_module_proxy) so a block
    # cannot rebind `ind.calc_rsi` / `math.pi` for the rest of the process.
    module.__dict__["math"] = safe_module_proxy(_math, "math")
    module.__dict__["statistics"] = safe_module_proxy(_statistics, "statistics")
    module.__dict__["ind"] = safe_module_proxy(_ind_mod, "ind")
    # Restrict builtins to the same whitelist the smoke test uses (parity is
    # asserted by this module's own docstring). Setting `__builtins__` here stops
    # CPython from injecting the FULL builtins on exec, so even a codegate miss
    # cannot resolve eval/exec/open/getattr at runtime — defense in depth.
    module.__dict__["__builtins__"] = safe_builtins()
    # M25: the validated source is compiled with a loop-budget AST — `while True`
    # class infinite loops raise a RuntimeError after 5M steps. The budget also covers
    # module-level validate/max_lookback hooks (this was the only path called on the
    # server without a timeout).
    code = compile_with_loop_budget(src, filename=str(path))
    exec(code, module.__dict__)
    return module


def register_custom_from_disk(name: str) -> None:
    """Load a single custom block from the store and register it.

    Raises on load failure or missing `evaluate` function.
    """
    import custom_block_store as cbs

    info = cbs.get_custom(name)
    if info is None:
        raise ValueError(f"no such custom block: {name}")
    # Current AUTO blocks persist role metadata at generation time. Earlier
    # ``agnt_e``/``agnt_x`` artifacts did not, even though their reserved names
    # already encode the only safe role. Backfill that durable fact before this
    # module enters the registry; a conflicting hand-edited record is rejected
    # rather than being allowed to masquerade as its opposite signal type.
    inferred_role = cbs.ensure_agent_role_metadata(name)
    if inferred_role is not None:
        info = cbs.get_custom(name)
        if info is None:  # defensive: a concurrent cleanup won the race
            raise ValueError(f"custom block disappeared during role migration: {name}")
    meta = dict(info.get("meta") or {})
    declared_role = meta.get("role") if meta.get("role") in {"entry", "exit"} else None
    module = _load_module_from_path(name, cbs.module_path(name))
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"{name}.py has no callable `evaluate`")

    def _eval_wrapper(
        strategy, idx, block, closes, _fn=evaluate, _declared_role=declared_role
    ):
        # Give custom blocks a small mutable state dict scoped by block idx.
        key = f"custom_state_{idx}"
        state = strategy._prev_state.setdefault(key, {})
        # User code may MUTATE the buffer → isolation copy.
        # The window matches the old deque width (buf_cap) exactly: if custom code
        # scans the whole list (e.g. sum(volumes)) it must see the same elements.
        cap = getattr(strategy, "_buf_cap", None)
        closes_view = closes[-cap:] if cap else list(closes)
        # Expose volume + high/low series via indicators — without changing the
        # signature (old blocks ignore them; new ones read volumes/highs/lows).
        # User code may mutate → all are window COPIES, aligned exactly with the
        # old deque width (buf_cap).
        indicators = dict(strategy._indicators.get(idx, {}))

        def _view(series):
            return series[-cap:] if cap else list(series)

        indicators["volumes"] = _view(strategy._volumes)
        indicators["highs"] = _view(getattr(strategy, "_highs", []))
        indicators["lows"] = _view(getattr(strategy, "_lows", []))
        # L25: a view carrying a COPY of params instead of the real SignalBlock —
        # a block that writes `block.params.update(...)` changes its own copy, not
        # the spec's live dict (reported params == the ones that ran).
        # Portfolio is also passed via a minimal facade (no block uses portfolio
        # in block scanning; is_net_long/short/flat passthrough is sufficient).
        block_view = SimpleNamespace(
            params=dict(block.params), role=block.role, type=block.type
        )
        signal = _fn(
            state, block_view, closes_view, indicators, _PortfolioView(strategy)
        )
        # Legacy AUTO files were only syntactically validated, so a rare branch
        # could return the opposite vocabulary despite a normal smoke result.
        # Never reinterpret it at runtime: fail closed to no signal. New blocks
        # are rejected earlier by agent._test_execute_generated; this is the
        # durable catalog/restart defense for legacy artifacts.
        if _declared_role == "entry":
            allowed = {None, "long", "short"}
        elif _declared_role == "exit":
            allowed = {None, "exit"}
        else:
            allowed = None
        if allowed is not None and signal not in allowed:
            logging.warning(
                "custom block '%s' returned %r outside declared %s role; ignored",
                name,
                signal,
                _declared_role,
            )
            return None
        return signal

    max_lookback_fn = getattr(module, "max_lookback", None)
    validate_fn = getattr(module, "validate", None)

    # M16: when the declared lookback is below the period-like values in params,
    # the window was silently trimmed (an 'SMA-200' block only saw the last 55
    # candles). Floor the declared value by the param implication and warn.
    _periodish = ("period", "length", "lookback", "window", "slow", "fast")

    def _lookback_with_floor(params, _decl=max_lookback_fn, _name=name):
        try:
            declared = int(_decl(params)) if callable(_decl) else 50
        except Exception:
            declared = 50
        implied = 0
        for k, v in (params or {}).items():
            if any(p in str(k).lower() for p in _periodish):
                try:
                    implied = max(implied, int(float(v)))
                except (TypeError, ValueError):
                    continue
        if implied and declared < implied + 5:
            logging.warning(
                "custom block '%s': declared lookback %d < param implication %d — "
                "using %d (M16)",
                _name,
                declared,
                implied,
                implied + 5,
            )
            return implied + 5
        return declared

    register_custom_block(
        name,
        {
            "meta": meta,
            "eval": _eval_wrapper,
            "on_start": None,
            "max_lookback": _lookback_with_floor,
            "validate": validate_fn if callable(validate_fn) else None,
        },
    )


def _load_custom_blocks() -> None:
    """Load all custom blocks from the on-disk store into BLOCK_REGISTRY.

    Broken modules are skipped with a logged warning — one bad block must
    not take down the whole catalog. This runs at import time
    (module-level call below), so the same discipline extends one level up:
    a registry.json that is itself unreadable (RegistryUnavailable — see
    custom_block_store's docstring on why that's raised rather than
    flattened to "no blocks") must not fail `import composer` and take down
    every route module that depends on it (2026-08-09 DeepR finding). The
    degrade is a server that starts with zero custom-block evaluators
    registered this run, not a server that cannot start at all.
    """
    try:
        import custom_block_store as cbs
    except Exception as e:  # pragma: no cover
        logging.getLogger(__name__).warning("cannot import custom_block_store: %s", e)
        return
    try:
        custom_blocks = cbs.list_custom()
    except Exception as e:
        logging.getLogger(__name__).warning(
            "custom block registry unavailable at startup — continuing with "
            "zero custom-block evaluators registered this run: %s",
            e,
        )
        return
    for info in custom_blocks:
        name = info["name"]
        try:
            register_custom_from_disk(name)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "skipping broken custom block '%s': %s", name, e
            )


_load_custom_blocks()


# --------------------------------------------------------------------------
# Adım 4 (Faz 2): _signal_matches_role, SignalBlock, ComposedStrategySpec,
# _as_bool, and build_spec (the spec-upsert service) extracted to
# composer_spec.py, imported back near the top of this file.


CATALOG_FILE = Path.home() / ".cache" / "nautilus_web_app" / "strategy_catalog.json"


def _catalog_block_names(spec: ComposedStrategySpec) -> list[str]:
    return [b.type for b in spec.blocks]


def _catalog_is_valid(spec: ComposedStrategySpec, custom_names: set[str]) -> bool:
    # H1338: preserve custom blocks that EXIST on disk (custom_names) but COULD NOT
    # be LOADED into the registry — since spec.validate() looks at the registry it
    # was deleting such a spec as an 'unknown block'. If the block exists on disk,
    # treat the spec as valid (the block can be reloaded; permanent deletion is
    # irreversible data loss).
    on_disk_custom = False
    for block_type in _catalog_block_names(spec):
        if block_type in BLOCK_CATALOG:
            continue
        if block_type in custom_names:
            on_disk_custom = True
            continue
        return False  # neither builtin nor custom-on-disk → genuinely invalid
    if on_disk_custom:
        # validate() fails on a registry miss; preserve a spec with an on-disk
        # custom block using only structural (block-type-independent) checks.
        return not (not spec.blocks or not any(b.role == "entry" for b in spec.blocks))
    return spec.validate() is None


# Perf: memoize the parsed catalog.json by (mtime, size). load_catalog runs on
# hot paths (~18 call sites incl. the agent inner loop) and re-read + re-parsed
# the file every call. The cache stores ONLY the raw list of dicts — fresh
# ComposedStrategySpec objects are rebuilt from it on every call (from_dict is
# read-only over the dicts), so no caller can mutate a shared/cached spec. A
# save (mtime change) invalidates the cache automatically.
_CATALOG_RAW_CACHE: tuple[int, int, list] | None = None


def _read_catalog_raw() -> list | None:
    """Return catalog.json's raw list of dicts, memoized by (mtime, size).

    Returns None for a missing / unparseable / non-list file — callers treat
    that as an empty catalog and NEVER save (M1342), matching the old inline
    behaviour byte-for-byte.
    """
    global _CATALOG_RAW_CACHE
    try:
        st = CATALOG_FILE.stat()
    except OSError:
        return None
    cached = _CATALOG_RAW_CACHE
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        raw = json.loads(CATALOG_FILE.read_text())
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    _CATALOG_RAW_CACHE = (st.st_mtime_ns, st.st_size, raw)
    return raw


def load_catalog() -> list[ComposedStrategySpec]:
    raw = _read_catalog_raw()
    if raw is None:
        return []
    import custom_block_store as cbs

    # H(store): if the registry cannot be READ, we do not know which custom blocks
    # exist. Falling back to an empty set made every custom-block strategy look
    # invalid, and the auto-save below then wrote that pruned catalog to disk —
    # permanent strategy loss from one transient file lock. `None` = unknown:
    # keep every spec and skip the rewrite.
    custom_names: set[str] | None
    try:
        custom_names = {rec["name"] for rec in cbs.list_custom()}
    except Exception as e:
        logging.getLogger(__name__).warning(
            "custom block registry unreadable (%s) — catalog untouched", e
        )
        custom_names = None

    # M1342: per-record try/except — a SINGLE broken record (unknown field,
    # exception-raising custom validate) could turn the whole catalog into [], then
    # via an RMW-save delete 30 strategies. Skip the broken one, keep the rest.
    catalog: list[ComposedStrategySpec] = []
    n_broken = 0
    for d in raw:
        try:
            catalog.append(ComposedStrategySpec.from_dict(d))
        except Exception:
            n_broken += 1
    if custom_names is None:
        return catalog  # registry unreadable → no pruning, no rewrite
    filtered = []
    for spec in catalog:
        try:
            if _catalog_is_valid(spec, custom_names):
                filtered.append(spec)
        except Exception:
            n_broken += 1  # the validate hook blew up — drop the spec but count it
    # Only rewrite under lock when VALID records were filtered out (NO broken
    # parse) — saving while a broken record exists would permanently delete them.
    if len(filtered) != len(catalog) and n_broken == 0:
        with _CATALOG_LOCK:
            save_catalog(filtered)
    return filtered


# M14: in-process lock for catalog mutations — the lab/agent/strategy routes did
# lock-free load→append→save (last writer wins, strategy loss).
# RLock: so the locked auto-save inside load_catalog can re-enter on the same
# thread while append_to_catalog holds the lock (deadlock prevention).
_CATALOG_LOCK = threading.RLock()


def save_catalog(specs: list[ComposedStrategySpec]) -> None:
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # M14: atomic write (tmp + os.replace) — a partial write cannot corrupt the file.
    payload = json.dumps([s.to_dict() for s in specs], indent=2)
    tmp = CATALOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload)
    os.replace(tmp, CATALOG_FILE)


def append_to_catalog(spec: ComposedStrategySpec) -> None:
    """load→append→save to the catalog under a SINGLE lock (M14).

    Prevents two concurrent runs from overwriting each other's new strategy.
    Callers (lab/agent/strategy) should use this instead of a lock-free RMW.
    """
    with _CATALOG_LOCK:
        cat = load_catalog()
        cat.append(spec)
        save_catalog(cat)


def mutate_catalog(fn) -> None:
    """Apply an arbitrary mutation on the catalog under lock: fn(list)→list."""
    with _CATALOG_LOCK:
        cat = load_catalog()
        result = fn(cat)
        # fn may intentionally return an EMPTY list ([]) (delete the last strategy) —
        # `or cat` was silently swallowing this and canceling the deletion. Only
        # treat None as a no-op.
        save_catalog(result if result is not None else cat)


# Adım 4 (Faz 2): new_spec_id extracted to composer_spec.py, imported
# back above.


class ComposedStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    spec_json: str
    trade_size: Decimal = Decimal("0.1")
    # Optional secondary bar type for multi-timeframe trend filter.
    # On the ImportableStrategyConfig path msgspec decodes str -> BarType;
    # in direct construction (backtest.py) a BarType object must be passed.
    secondary_bar_type: BarType | None = None


class ComposedStrategy(Strategy):
    """Nautilus Strategy that interprets a list of SignalBlock records."""

    def __init__(self, config: ComposedStrategyConfig) -> None:
        super().__init__(config)
        spec_dict = json.loads(config.spec_json)
        self.spec = ComposedStrategySpec.from_dict(spec_dict)
        self.instrument = None
        max_lookback = self._max_lookback()
        # Price/volume buffers: flat list + amortized compaction. Formerly a deque,
        # taking a FULL COPY of list(deque) every candle (~0.5s measured over 200k
        # candles). Blocks read queue-relative slices (closes[-n:]) — the values are
        # exactly the same; only the copy cost is removed. The custom-block adapter
        # still gives a window copy for isolation.
        self._buf_cap = max_lookback + 5
        # H1940 (effective fix): the adx/stoch_rsi/wave_trend RECURSIVE (Wilder/EMA)
        # blocks require a FIXED window of the last NAU_WINDOW=260 candles via _nau_win.
        # While the buffer swings between buf_cap↔4·buf_cap, if it never reaches 260
        # (when buf_cap is small) _nau_win returns a swinging window; the recursive seed
        # jumps ~4× on each compaction producing a SPURIOUS cross (metrics/score corrupted).
        # If these blocks are present, keep the buffer at least NAU_WINDOW → compaction
        # never drops below 260, _nau_win consistently returns 260 every candle
        # (same as NAU deque(maxlen=260)).
        if any(b.type in _NAU_RECURSIVE_BLOCKS for b in self.spec.blocks):
            self._buf_cap = max(self._buf_cap, NAU_WINDOW)
        # vol_target sizing reads calc_ewma_vol(self._closes, span); keep the
        # close buffer at least span+5 so the EWMA window is consistent
        # regardless of block lookbacks (buffer is periodically trimmed).
        if self.spec.trade_size_mode == "vol_target":
            self._buf_cap = max(self._buf_cap, int(self.spec.trade_size_vol_span) + 5)
        self._closes: list[float] = []
        # Volume series — for the volume_spike block + custom blocks'
        # indicators["volumes"] access (aligned with closes).
        self._volumes: list[float] = []
        # High/low series — so custom blocks can compute real OHLC-based indicators
        # (ADX/ATR/WaveTrend/Stochastic/Donchian) via indicators["highs"]/["lows"].
        # Aligned with closes/volumes, same buffer lifecycle. The Bar already carries
        # full OHLCV (backtest._bars_from_df); in addition to the close, high/low are
        # captured too.
        self._highs: list[float] = []
        self._lows: list[float] = []
        # so _iid() doesn't do isinstance/from_str on every call (4-6 calls per candle)
        _iid_raw = config.instrument_id
        self._iid_obj = (
            InstrumentId.from_str(_iid_raw) if isinstance(_iid_raw, str) else _iid_raw
        )
        # _current_equity fast path: the first successful strategy is used directly
        # by subsequent ones ("portfolio" | "balances" | None=not yet known)
        self._equity_mode: str | None = None
        self._equity_constant_value: float = 0.0  # set when _equity_mode=="constant"
        self._equity_ccy: str | None = None  # settlement currency code, resolved once
        self._prev_state: dict = {}
        # Per-block Nautilus indicators, keyed by block index.
        self._indicators: dict[int, dict] = {}
        # Shared ATR (for sl/tp=atr or trade_size_mode=atr_target)
        self._atr = None
        # Track blocks whose evaluate() raised, keyed by (idx, error_type) so
        # new error types on the same block are still logged.
        self._eval_failed: set[tuple[int, str]] = set()
        # Pre-partition blocks by role — constant for strategy lifetime.
        self._entry_blocks = [
            (i, b) for i, b in enumerate(self.spec.blocks) if b.role == "entry"
        ]
        self._exit_blocks = [
            (i, b) for i, b in enumerate(self.spec.blocks) if b.role == "exit"
        ]
        # MTM equity snapshots: one value per bar for real drawdown calculation
        self._mtm_equity: list[float] = []
        # L19: bar timestamps (ns) of the MTM snapshots — backtest.py builds a
        # bar-resolution equity_curve_mtm as (ts, eq) pairs.
        self._mtm_ts: list[int] = []
        self._mtm_snapshot_error_logged = False
        # delay_fill buffer: pending entry order side when delay_fill=True
        self._pending_entry: str | None = None  # "BUY" | "SELL" | None
        # Decision log: on each entry/exit signal, the firing blocks +
        # indicator values. Orders are stamped with a "dr:<seq>"/"xr:<seq>" tag;
        # after the backtest, a positions↔fills join produces the per-trade
        # entry/exit reason (harvest: same lifecycle as _mtm_equity).
        self._decision_log: list[dict] = []
        self._decision_seq: int = 0
        # in delay_fill, the reason on the signal candle is carried to the next candle's submit
        self._pending_entry_reason: dict | None = None
        # L13: deferred exit reason in delay_fill (entry symmetry).
        self._pending_exit_reason: dict | None = None
        # Multi-timeframe trend filter state
        trend_period = max(self.spec.trend_ema_period, 10)
        self._trend_closes: deque[float] = deque(maxlen=trend_period + 5)
        self._trend_bias: str | None = None  # "bullish" | "bearish" | None

    def _max_lookback(self) -> int:
        best = 30
        for b in self.spec.blocks:
            entry = BLOCK_REGISTRY.get(b.type)
            if entry is None:
                continue
            try:
                lb = int(entry["max_lookback"](b.params))
            except Exception:
                lb = 50
            best = max(best, lb)
        return best

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self._iid())
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self._iid()}")
            self.stop()
            return
        self.subscribe_bars(self.config.bar_type)

        # Subscribe to secondary (trend) bar feed if trend filter is enabled
        if self.spec.trend_filter and self.config.secondary_bar_type is not None:
            self.subscribe_bars(self.config.secondary_bar_type)

        # Delegate indicator registration to each block's on_start hook.
        for i, b in enumerate(self.spec.blocks):
            entry = BLOCK_REGISTRY.get(b.type)
            if entry is None:
                continue
            hook = entry.get("on_start")
            if hook is not None:
                try:
                    hook(self, i, b)
                except Exception as e:
                    self.log.error(f"on_start hook failed for block {b.type}: {e}")

        # Shared ATR for SL/TP or ATR-target sizing.
        needs_atr = (
            self.spec.sl_type == "atr"
            or self.spec.tp_type == "atr"
            or self.spec.trade_size_mode == "atr_target"
        )
        if needs_atr:
            self._atr = AverageTrueRange(int(self.spec.atr_period))
            self.register_indicator_for_bars(self.config.bar_type, self._atr)

    # ------------------------------------------------------------------
    # Signal evaluation

    def _eval_block(
        self, idx: int, block: SignalBlock, closes: list[float]
    ) -> str | None:
        """Dispatch to the block-type's eval function via BLOCK_REGISTRY.

        Custom-block eval failures are caught and logged once — the block
        yields None for that bar rather than crashing the strategy.
        """
        entry = BLOCK_REGISTRY.get(block.type)
        if entry is None:
            return None
        try:
            return entry["eval"](self, idx, block, closes)
        except Exception as e:
            # Log the first failure. After warmup (enough bars in deque), log
            # every new error type so persistent bugs are visible.
            err_key = (idx, type(e).__name__)
            if err_key not in self._eval_failed:
                self._eval_failed.add(err_key)
                self.log.error(f"block {block.type} eval failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Order helpers

    def _iid(self) -> InstrumentId:
        return self._iid_obj

    def _settlement_ccy_code(self) -> str | None:
        """Currency the equity is denominated in — cached, resolved once."""
        if self._equity_ccy is not None:
            return self._equity_ccy
        for source in (
            lambda: self.portfolio.account(self._iid_obj.venue).base_currency,
            lambda: self.instrument.settlement_currency,
            lambda: self.instrument.quote_currency,
        ):
            try:
                ccy = source()
                if ccy is not None:
                    self._equity_ccy = str(getattr(ccy, "code", ccy))
                    return self._equity_ccy
            except Exception:
                continue
        return None

    def _current_equity(self) -> float:
        """Best-effort account equity. Portfolio.equity() → USDT balance fallback → constant.

        Since it is called every candle (MTM curve), the first successful path is
        saved to ``_equity_mode`` and on subsequent candles the attempt/fallback chain
        is skipped — same API, same value, less Python cost.

        M-equity: ``Portfolio.equity()`` returns **dict[Currency, Money]**, not a
        Money — ``float(dict)`` raised, the except swallowed it, and every run
        silently fell through to the balance scan below. On a CASH account (the
        default whenever ``allow_short`` is off) that balance is the *unspent
        cash*: buying converts USDT into BTC, so the MTM curve dropped to the
        leftover cash for as long as a position was open and froze there. A
        180-day BTCUSDT 1h run reported equity 10084 → 5762 while holding a
        position that closed +$69.70, feeding a fictional -94% max_dd and an
        inflated Sharpe into every consumer of these metrics.

        The dict is exactly what is wanted — per currency it is
        ``balance.total + Σ mark_value(open positions)`` for cash accounts and
        ``balance.total + Σ unrealized_pnl`` for margin ones. Read the entry for
        the settlement currency; the sibling entries (e.g. a BTC holding) are
        the same value in other units, so summing them would double count.
        """
        venue = self._iid_obj.venue
        # Terminal fallback already determined on an earlier candle — return
        # the cached constant directly, skip re-trying two paths already
        # known to fail on every remaining candle of the run.
        if self._equity_mode == "constant":
            return self._equity_constant_value
        # 1) Portfolio.equity(venue) — v2 native path
        if self._equity_mode in (None, "portfolio"):
            try:
                eq = self.portfolio.equity(venue)
                if eq:
                    money = None
                    if isinstance(eq, dict):
                        want = self._settlement_ccy_code()
                        for ccy, m in eq.items():
                            if str(getattr(ccy, "code", ccy)) == want:
                                money = m
                                break
                        if money is None and len(eq) == 1:
                            money = next(iter(eq.values()))
                    else:  # older builds returned a bare Money
                        money = eq
                    if money is not None:
                        self._equity_mode = "portfolio"
                        return float(
                            money.as_double() if hasattr(money, "as_double") else money
                        )
            except Exception as e:
                self.log.warning(f"_current_equity: portfolio.equity() failed: {e}")
        # 2) Scan account balances — USDT/USD preferred, then the first found
        try:
            account = self.portfolio.account(venue)
            if account is not None:
                balances = account.balances()
                # First look for USDT or USD
                for preferred in ("USDT", "USD"):
                    for currency, bal in balances.items():
                        if str(currency) == preferred:
                            for attr in ("total", "free"):
                                v = getattr(bal, attr, None)
                                if v is not None:
                                    try:
                                        _eq = float(
                                            v.as_double()
                                            if hasattr(v, "as_double")
                                            else v
                                        )
                                        # Fast-path lock: on subsequent candles the
                                        # portfolio.equity() attempt is skipped
                                        # (the documented _equity_mode cache;
                                        # 'balances' was NEVER set before).
                                        self._equity_mode = "balances"
                                        return _eq
                                    except Exception:
                                        continue
                # Fallback: first non-None balance (currency unknown, log a warning)
                for bal in balances.values():
                    for attr in ("total", "free"):
                        v = getattr(bal, attr, None)
                        if v is not None:
                            try:
                                result = float(
                                    v.as_double() if hasattr(v, "as_double") else v
                                )
                                self._equity_mode = "balances"  # fast-path lock
                                self.log.warning(
                                    f"_current_equity: USDT/USD not found, using first balance ({result})"
                                )
                                return result
                            except Exception:
                                continue
        except Exception as e:
            self.log.warning(f"_current_equity: balance scan failed: {e}")
        # L43: fallback from a single source (app_constants.STARTING_CASH) — a
        # copied constant could silently diverge. app_constants is an independent
        # module, no circular import risk.
        #
        # Both real paths failed (or returned nothing usable): cache the
        # constant and the "constant" mode itself, so (a) every remaining
        # candle short-circuits straight past both failing paths above instead
        # of re-attempting them, and (b) this degradation is logged exactly
        # once instead of staying invisible — the equity/max_dd/Sharpe this
        # run reports from here on are NOT real P&L.
        from app_constants import STARTING_CASH

        self._equity_mode = "constant"
        self._equity_constant_value = float(STARTING_CASH)
        self.log.warning(
            f"_current_equity: portfolio.equity() and balance scan both failed — "
            f"falling back to constant STARTING_CASH={STARTING_CASH}; equity/"
            f"max_dd/Sharpe for the rest of this run will not reflect real P&L"
        )
        return self._equity_constant_value

    def _compute_qty(self, price: float) -> float:
        mode = self.spec.trade_size_mode
        if mode == "fixed":
            return float(self.spec.trade_size)
        if price <= 0:
            return float(self.spec.trade_size)
        if mode == "fixed_usdt":
            # Fixed dollar → quantity: divide the USDT amount by the price
            return max(0.0, float(self.spec.trade_size_usdt) / price)
        if mode == "percent_equity":
            # Never submit a percent-equity order above account equity. Besides
            # malformed/manual specs, AUTO's exposure setting is environment
            # controlled; fail closed on non-finite account/pct values rather
            # than turning them into an unbounded share quantity.
            try:
                equity = float(self._current_equity())
                pct = float(self.spec.trade_size_percent)
            except (TypeError, ValueError):
                return 0.0
            if not math.isfinite(equity) or not math.isfinite(pct):
                return 0.0
            notional = max(0.0, equity) * min(max(pct, 0.0), 100.0) / 100.0
            return notional / price
        if mode == "atr_target":
            if self._atr is None or not self._atr.initialized or self._atr.value <= 0:
                return float(self.spec.trade_size)
            equity = self._current_equity()
            risk_usd = equity * (self.spec.trade_size_atr_risk / 100.0)
            return max(0.0, risk_usd / self._atr.value)
        if mode == "vol_target":
            # size = (vol_target / ewma_vol) * capital / price — the vol-targeted
            # trend sizing (formerly a standalone strategy). capital is FIXED
            # (spec.trade_size_capital), not live equity. Warmup (<span+1 closes)
            # → fixed trade_size fallback.
            from indicators import calc_ewma_vol

            vol = calc_ewma_vol(self._closes, int(self.spec.trade_size_vol_span))
            if vol is None or vol <= 0:
                return float(self.spec.trade_size)
            cap = float(self.spec.trade_size_capital)
            size = (float(self.spec.trade_size_vol_target) / vol) * cap / price
            # Upper clamp: never risk more than 95% of capital notional (no leverage
            # blowup). make_qty rounds to size_increment; a rounded-to-zero size is
            # skipped by the qty<=0 guard in _submit_entry.
            return max(0.0, min(size, 0.95 * cap / price))
        return float(self.spec.trade_size)

    def _compute_bracket_prices(
        self, side: OrderSide, price: float
    ) -> tuple[float, float | None]:
        """Return (sl_price, tp_price_or_None) for a bracket entry."""
        atr_ready = self._atr is not None and self._atr.initialized

        sl_dist: float
        if self.spec.sl_type == "atr":
            if not atr_ready:
                return 0.0, None  # caller checks sl_price <= 0 and skips order
            sl_dist = float(self._atr.value) * float(self.spec.sl_value)
        else:
            sl_dist = price * (float(self.spec.sl_value) / 100.0)

        if self.spec.tp_type == "off":
            tp_dist: float | None = None
        elif self.spec.tp_type == "atr":
            if not atr_ready:
                return 0.0, None
            tp_dist = float(self._atr.value) * float(self.spec.tp_value)
        else:
            tp_dist = price * (float(self.spec.tp_value) / 100.0)

        if side == OrderSide.BUY:
            sl_price = price - sl_dist
            tp_price = price + tp_dist if tp_dist is not None else None
        else:  # SELL
            sl_price = price + sl_dist
            tp_price = price - tp_dist if tp_dist is not None else None

        return max(sl_price, 0.01), (
            max(tp_price, 0.01) if tp_price is not None else None
        )

    def _entry_limit_price(self, side: OrderSide, price: float) -> float:
        offset = float(self.spec.limit_offset_bps) / 10_000.0
        if side == OrderSide.BUY:
            return price * (1.0 - offset)
        return price * (1.0 + offset)

    # ------------------------------------------------------------------
    # Decision log (entry/exit reasons)

    def _build_reason(self, kind, side, fires_per, blocks_list, bar, closes) -> dict:
        """label+params+indicator-value snapshot of the firing blocks."""
        fired = []
        for (i, b), f in zip(blocks_list, fires_per, strict=True):
            if not f:
                continue
            entry = BLOCK_REGISTRY.get(b.type) or {}
            label = (entry.get("meta") or {}).get("label") or b.type
            values = None
            snap = entry.get("snapshot")
            if snap is not None:
                try:
                    values = snap(self, i, b, closes)
                except Exception:
                    values = None
            fired.append(
                {
                    "idx": i,
                    "type": b.type,
                    "label": label,
                    "params": dict(b.params or {}),
                    "values": values,
                }
            )
        return {
            "seq": None,  # _log_decision sets this
            "kind": kind,
            "side": side,
            "bar_ts": int(bar.ts_event // 1_000_000_000),
            "submit_ts": None,
            "logic": self.spec.entry_logic if kind == "entry" else self.spec.exit_logic,
            "blocks": fired,
            "trend_bias": self._trend_bias,
        }

    def _log_decision(self, reason: dict, submit_ts: int) -> int:
        """Write the decision to the log, return the seq to use in the order tag."""
        self._decision_seq += 1
        reason["seq"] = self._decision_seq
        reason["submit_ts"] = submit_ts
        self._decision_log.append(reason)
        return self._decision_seq

    def _can_submit_entry(self, side: OrderSide, bar: Bar) -> bool:
        """M17: pre-check BEFORE SENDING an order.

        On the flip path the 'close first, then enter' order was leaving the strategy
        unintentionally FLAT if the entry was going to fail anyway (qty→0, make_qty
        error, ATR not ready for the bracket SL) by closing the old position. A flip
        only begins with a close if this check is True.
        """
        price = float(bar.close)
        qty_raw = self._compute_qty(price)
        if qty_raw <= 0:
            return False
        try:
            qty = self.instrument.make_qty(qty_raw)
        except Exception:
            return False
        if float(qty) <= 0:
            return False
        if self.spec.use_bracket:
            sl_price, _tp = self._compute_bracket_prices(side, price)
            if sl_price <= 0:
                return False
        return True

    def _cancel_working(self) -> None:
        """H1999/M1840: cancel the instrument's open (working) orders.

        On exit and flip only close_all_positions was being called; pending
        SL/TP protective orders (bracket/SL-only) and unfilled GTC limit entry
        orders were not canceled. Result: (a) a stale SL/TP could close the next
        position at the old level; (b) unfilled limit entries could accumulate and
        open double/triple positions. Since Nautilus manage_contingent_orders is
        off by default, we do this manually.
        """
        try:
            self.cancel_all_orders(self._iid())
        except Exception as e:
            self.log.error(f"_cancel_working failed: {e}")

    def _rollback_decision(self, seq: int | None) -> None:
        """L12: order could not be sent — roll back the decision just written.

        The decision log is written BEFORE the submit; if the order does not go out
        the 'dr:<seq>' tag is on no order and the log would bloat with ghost 'entry'
        records. The strategy is single-threaded — the pop is safe.
        """
        if seq is None:
            return
        if self._decision_log and self._decision_log[-1].get("seq") == seq:
            self._decision_log.pop()
            self._decision_seq -= 1

    def _submit_entry(
        self, side: OrderSide, bar: Bar, reason_seq: int | None = None
    ) -> bool:
        """Submits the entry order; True IF SENT (L12: bool contract —
        on a False return the caller rolls back the decision)."""
        entry_tags = [f"dr:{reason_seq}"] if reason_seq is not None else None
        price = float(bar.close)
        qty_raw = self._compute_qty(price)
        if qty_raw <= 0:
            return False
        try:
            qty = self.instrument.make_qty(qty_raw)
        except Exception:
            return False
        # Don't send an order with a quantity that drops to zero after rounding
        if float(qty) <= 0:
            return False

        if self.spec.use_bracket:
            sl_price, tp_price = self._compute_bracket_prices(side, price)
            if sl_price <= 0:
                # ATR not ready yet — SL cannot be computed for the bracket order
                return False
            entry_price_obj = None
            entry_order_type = OrderType.MARKET
            if self.spec.order_type == "limit":
                entry_order_type = OrderType.LIMIT
                entry_price_obj = self.instrument.make_price(
                    self._entry_limit_price(side, price)
                )

            if tp_price is None:
                # tp_type == 'off': Nautilus bracket() always sets up a TP LIMIT
                # order and does NOT ACCEPT price=None (TypeError — a live bug seen
                # 8× in the agent logs). Fall back to entry + SL-only: send the entry
                # order normally, add the SL as a reduce-only STOP_MARKET.
                if entry_order_type == OrderType.LIMIT:
                    entry = self.order_factory.limit(
                        instrument_id=self._iid(),
                        order_side=side,
                        quantity=qty,
                        price=entry_price_obj,
                        time_in_force=TimeInForce.GTC,
                        tags=entry_tags,
                    )
                else:
                    entry = self.order_factory.market(
                        instrument_id=self._iid(),
                        order_side=side,
                        quantity=qty,
                        tags=entry_tags,
                    )
                self.submit_order(entry)
                sl_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
                sl_order = self.order_factory.stop_market(
                    instrument_id=self._iid(),
                    order_side=sl_side,
                    quantity=qty,
                    trigger_price=self.instrument.make_price(sl_price),
                    time_in_force=TimeInForce.GTC,
                    reduce_only=True,  # does NOT open a reverse position if no position exists
                    tags=["sl"],
                )
                self.submit_order(sl_order)
                return True

            order_list = self.order_factory.bracket(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                entry_order_type=entry_order_type,
                entry_price=entry_price_obj,
                sl_trigger_price=self.instrument.make_price(sl_price),
                tp_price=self.instrument.make_price(tp_price),
                time_in_force=TimeInForce.GTC,
                emulation_trigger=TriggerType.LAST_PRICE
                if self.spec.emulate
                else TriggerType.NO_TRIGGER,
                entry_tags=entry_tags,
                sl_tags=["sl"],
                tp_tags=["tp"],
            )
            self.submit_order_list(order_list)
            return True

        if self.spec.order_type == "market":
            order = self.order_factory.market(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                tags=entry_tags,
            )
        else:
            order = self.order_factory.limit(
                instrument_id=self._iid(),
                order_side=side,
                quantity=qty,
                price=self.instrument.make_price(self._entry_limit_price(side, price)),
                time_in_force=TimeInForce.GTC,
                tags=entry_tags,
            )
        self.submit_order(order)
        return True

    # ------------------------------------------------------------------
    # Bar handler

    @staticmethod
    def _ema(closes: deque[float], period: int) -> float | None:
        """Simple EMA over the last `period` values. Returns None if not enough data."""
        vals = list(closes)[-period:]
        if len(vals) < period:
            return None
        k = 2.0 / (period + 1)
        ema = vals[0]
        for v in vals[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def on_bar(self, bar: Bar) -> None:
        # Multi-timeframe routing: secondary feed updates trend bias only
        if (
            self.spec.trend_filter
            and self.config.secondary_bar_type is not None
            and bar.bar_type == self.config.secondary_bar_type
        ):
            self._trend_closes.append(float(bar.close))
            ema = self._ema(self._trend_closes, self.spec.trend_ema_period)
            if ema is not None:
                self._trend_bias = "bullish" if float(bar.close) > ema else "bearish"
            return

        # Snapshot MTM equity every bar for real drawdown calculation. Log once
        # per run (not per bar — this runs on every bar and a failing snapshot
        # would otherwise spam the log for the rest of the backtest) so a
        # systemic equity() problem doesn't just quietly thin the curve.
        try:
            eq = self._current_equity()
            if eq > 0:
                self._mtm_equity.append(eq)
                self._mtm_ts.append(int(bar.ts_event))  # L19: aligned time
        except Exception:
            if not self._mtm_snapshot_error_logged:
                self._mtm_snapshot_error_logged = True
                logging.getLogger(__name__).warning(
                    "on_bar: MTM equity snapshot failed (further occurrences this "
                    "run are suppressed)",
                    exc_info=True,
                )

        # L13: delay_fill — the deferred EXIT is processed BEFORE the deferred entry
        # (if both an exit and an entry are queued on the same candle, order: exit → entry;
        # flip semantics stay the same as the close_all in the pending_entry branch).
        if self.spec.delay_fill and self._pending_exit_reason is not None:
            reason = self._pending_exit_reason
            self._pending_exit_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            if self.portfolio.is_net_long(self._iid()) or self.portfolio.is_net_short(
                self._iid()
            ):
                seq = self._log_decision(reason, submit_ts)
                self._cancel_working()  # H1999: stale SL/TP + unfilled limit
                self.close_all_positions(self._iid(), tags=[f"xr:{seq}"])

        # delay_fill: execute deferred entry from previous bar
        if self.spec.delay_fill and self._pending_entry is not None:
            side = self._pending_entry
            self._pending_entry = None
            # The reason accumulated on the signal candle — only reaches the log if
            # the order is ACTUALLY sent (if a position is already open the decision
            # silently drops).
            reason = self._pending_entry_reason
            self._pending_entry_reason = None
            submit_ts = int(bar.ts_event // 1_000_000_000)
            is_long = self.portfolio.is_net_long(self._iid())
            is_short = self.portfolio.is_net_short(self._iid())
            if side == "BUY" and not is_long:
                # M17: flip pre-check (see the synchronous path) — if the entry
                # cannot go out, the old position is not closed.
                if is_short and not self._can_submit_entry(OrderSide.BUY, bar):
                    pass
                else:
                    # H1999 (flip: stale SL/TP) + #4 (flat: unfilled GTC limit
                    # entry). Unconditional — always clear open orders before sending
                    # a new entry so limit entries don't accumulate and fill together.
                    self._cancel_working()
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.BUY, bar, reason_seq=seq):
                        self._rollback_decision(seq)
            elif side == "SELL" and not is_short:
                if is_long and not self._can_submit_entry(OrderSide.SELL, bar):
                    pass
                else:
                    self._cancel_working()  # H1999 (flip) + #4 (flat limit accumulation)
                    self.close_all_positions(self._iid(), tags=["flip"])
                    seq = self._log_decision(reason, submit_ts) if reason else None
                    if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                        self._rollback_decision(seq)

        # Append to the buffers; amortized compaction (trim the queue at 4×cap) — zero-copy
        # sharing instead of a full copy every candle (the old list(deque)).
        # The four series (close/volume/high/low) are trimmed IN SYNC → stay aligned.
        self._closes.append(float(bar.close))
        self._volumes.append(float(bar.volume))
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        if len(self._closes) > 4 * self._buf_cap:
            _cut = len(self._closes) - self._buf_cap
            del self._closes[:_cut]
            del self._volumes[:_cut]
            del self._highs[:_cut]
            del self._lows[:_cut]
        closes = self._closes

        long_fires_per: list[bool] = []
        short_fires_per: list[bool] = []
        for i, b in self._entry_blocks:
            r = self._eval_block(i, b, closes)
            long_fires_per.append(r == "long")
            short_fires_per.append(r == "short")

        exit_fires_per: list[bool] = []
        for i, b in self._exit_blocks:
            r = self._eval_block(i, b, closes)
            # Role contract is deliberately exact. ``bool('long')`` used to
            # turn an entry-style custom block into an exit signal, so malformed
            # generated code could pass smoke/runtime and still win a backtest.
            # Only the exit vocabulary's affirmative value may close a position.
            exit_fires_per.append(_signal_matches_role("exit", r))

        if self.spec.entry_logic == "AND":
            long_fires = bool(self._entry_blocks) and all(long_fires_per)
            short_fires = bool(self._entry_blocks) and all(short_fires_per)
        else:
            long_fires = any(long_fires_per)
            short_fires = any(short_fires_per)

        if not self.spec.allow_short:
            short_fires = False

        # Multi-timeframe trend filter: suppress entries against the trend
        if self.spec.trend_filter and self._trend_bias is not None:
            if self._trend_bias == "bearish":
                long_fires = False  # no longs in downtrend
            elif self._trend_bias == "bullish":
                # Suppress shorts in an uptrend. This branch was formerly guarded
                # by `and not allow_short` — but the ONLY case where shorts fire is
                # allow_short=True; the guard skipped exactly that case and let
                # counter-trend shorts through unfiltered (the filter did nothing).
                # When allow_short=False, short_fires is already False so this line
                # is harmless in that case.
                short_fires = False  # no shorts in uptrend

        if self.spec.exit_logic == "AND":
            exit_fires = bool(self._exit_blocks) and all(exit_fires_per)
        else:
            exit_fires = any(exit_fires_per)

        is_long = self.portfolio.is_net_long(self._iid())
        is_short = self.portfolio.is_net_short(self._iid())

        if exit_fires and (is_long or is_short):
            reason = self._build_reason(
                "exit", None, exit_fires_per, self._exit_blocks, bar, closes
            )
            if self.spec.delay_fill:
                # L13: delay_fill is now applied to EXITS too — formerly entries
                # were delayed by one candle while exits were processed on the
                # signal candle, so exits were systematically one candle early
                # (asymmetric/optimistic timing). A deliberate behavior change.
                self._pending_exit_reason = reason
            else:
                seq = self._log_decision(reason, reason["bar_ts"])
                self._cancel_working()  # H1999: stale SL/TP + unfilled limit
                self.close_all_positions(self._iid(), tags=[f"xr:{seq}"])
            return

        if long_fires and not is_long:
            reason = self._build_reason(
                "entry", "BUY", long_fires_per, self._entry_blocks, bar, closes
            )
            if self.spec.delay_fill:
                self._pending_entry = "BUY"  # execute next bar
                self._pending_entry_reason = reason
            else:
                # M17: on a flip, entry pre-check FIRST — if the new order
                # cannot go out anyway, don't close the old position (avoid staying flat unintentionally).
                if is_short:
                    if not self._can_submit_entry(OrderSide.BUY, bar):
                        return
                    self._cancel_working()  # H1999
                    self.close_all_positions(self._iid(), tags=["flip"])
                else:
                    self._cancel_working()  # #4: prevent unfilled GTC limit entry accumulation when flat
                seq = self._log_decision(reason, reason["bar_ts"])
                if not self._submit_entry(OrderSide.BUY, bar, reason_seq=seq):
                    self._rollback_decision(seq)
        elif short_fires and not is_short:
            reason = self._build_reason(
                "entry", "SELL", short_fires_per, self._entry_blocks, bar, closes
            )
            if self.spec.delay_fill:
                self._pending_entry = "SELL"  # execute next bar
                self._pending_entry_reason = reason
            else:
                if is_long:
                    if not self._can_submit_entry(OrderSide.SELL, bar):
                        return
                    self._cancel_working()  # H1999
                    self.close_all_positions(self._iid(), tags=["flip"])
                else:
                    self._cancel_working()  # #4: prevent unfilled GTC limit entry accumulation when flat
                seq = self._log_decision(reason, reason["bar_ts"])
                if not self._submit_entry(OrderSide.SELL, bar, reason_seq=seq):
                    self._rollback_decision(seq)

    def on_stop(self) -> None:
        self.cancel_all_orders(self._iid())
        self.close_all_positions(self._iid(), tags=["eob"])
