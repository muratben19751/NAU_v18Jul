"""On-disk store for user-defined custom signal blocks.

Layout under `~/.cache/nautilus_web_app/custom_blocks/`:
  - registry.json           # index: {name: {meta, module_file, generated_at, prompt}}
  - {name}.py               # one Python module per block

Each `{name}.py` defines a top-level `evaluate(state, block, closes, indicators, portfolio)`
function returning "long" / "short" / "exit" / None. Optional module-level
functions: `max_lookback(params)` and `validate(block)`.

The store never imports the .py files itself — loading is done by composer.py
via `importlib.util.spec_from_file_location` so nothing is added to sys.path.

Wiki References
---------------
Bkz: [[strategy_and_actor]]

Block codes are imported at run time; each block is a single function (`evaluate`).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# M(store): in-process lock for registry.json + block .py writes — if the agent
# worker thread (entry+exit back to back) runs save/delete concurrently with
# /lab or /strategy, a read-modify-write lost-update could silently destroy a
# block's registration (RLock: reentrant within the same thread).
_STORE_LOCK = threading.RLock()

STORE_DIR = Path.home() / ".cache" / "nautilus_web_app" / "custom_blocks"
REGISTRY_FILE = STORE_DIR / "registry.json"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Auto-generated block names carry these prefixes: ``desc_*`` from the AI
# suggest/edit flows, ``agnt_*`` from the autonomous agent. User-authored blocks
# are named via _slugify(label) and never start with these. Used by list_custom
# to keep the UI list free of bulk ephemeral blocks (see the 06 · Custom Blocks
# panel bloat found in the /studio QA pass).
_EPHEMERAL_PREFIXES = ("desc_", "agnt_")


def _ensure_dir() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)


def _read_registry() -> dict[str, dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return {}
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("registry.json is not a dict")
        return data
    except Exception:
        # If registry.json is corrupt, back it up and start empty — so the next write
        # does not reset it and it can be rebuilt from the existing .py files.
        corrupt = REGISTRY_FILE.with_suffix(".json.bak")
        try:
            REGISTRY_FILE.replace(corrupt)
        except Exception:
            pass
        return {}


def _write_registry(reg: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    # Atomic write: first write to a tmp file, then rename
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(REGISTRY_FILE)


def is_valid_name(name: str) -> bool:
    """Names must be lowercase snake_case, start with a letter, 2-40 chars."""
    return bool(name and _NAME_RE.match(name))


def list_custom(include_ephemeral: bool = True) -> list[dict[str, Any]]:
    """Return list of {name, meta, module_file, generated_at, prompt} entries.

    include_ephemeral=False filters out auto-generated blocks whose names carry
    an ephemeral prefix (``desc_*`` from the AI suggest/edit flows, ``agnt_*``
    from the autonomous agent). These accumulate in bulk (hundreds) and bloat
    the ``06 · Custom Blocks`` UI list; the backtest/resolution path in composer
    still needs the full set, so the default stays True.
    """
    reg = _read_registry()
    out = []
    for name, info in reg.items():
        if not include_ephemeral and name.startswith(_EPHEMERAL_PREFIXES):
            continue
        out.append({"name": name, **info})
    return out


def get_custom(name: str) -> dict[str, Any] | None:
    reg = _read_registry()
    if name not in reg:
        return None
    return {"name": name, **reg[name]}


def module_path(name: str) -> Path:
    return STORE_DIR / f"{name}.py"


def save_custom(name: str, meta: dict, code: str, prompt: str = "") -> Path:
    """Persist a custom block to disk. Returns the module file path.

    Raises ValueError on invalid name.
    """
    if not is_valid_name(name):
        raise ValueError(
            f"invalid block name: {name!r} (must be lowercase snake_case, 2-40 chars)"
        )
    _ensure_dir()
    path = module_path(name)
    # H(store): composer reads with read_text(encoding="utf-8"); if encoding is
    # not specified on write, Windows uses the locale (cp1254) → LLM code
    # containing non-ASCII (→, …, typographic quotes) blows up with
    # UnicodeEncodeError or the block can never be imported. Pin UTF-8.
    with _STORE_LOCK:
        path.write_text(code, encoding="utf-8")
        reg = _read_registry()
        reg[name] = {
            "meta": meta,
            "module_file": path.name,
            "generated_at": datetime.now(UTC).isoformat(),
            "prompt": prompt,
        }
        _write_registry(reg)
    return path


def delete_custom(name: str) -> bool:
    """Remove a custom block from disk, registry, and in-memory BLOCK_REGISTRY."""
    if not is_valid_name(name):
        return False
    with _STORE_LOCK:  # M(store): locked RMW — lost-update prevention
        reg = _read_registry()
        if name not in reg:
            return False
        path = module_path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        del reg[name]
        _write_registry(reg)
    # Clear in-memory — a block deleted in the same session should not run
    try:
        from composer import unregister_custom_block

        unregister_custom_block(name)
    except Exception:
        pass
    return True
