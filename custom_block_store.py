"""On-disk store for user-defined custom signal blocks.

Layout under `~/.cache/nautilus_web_app/custom_blocks/`:
  - registry.json           # index: {name: {meta, module_file, generated_at, prompt}}
  - {name}.py               # one Python module per block

Each `{name}.py` defines a top-level `evaluate(state, block, closes, indicators, portfolio)`
function returning "long" / "short" / "exit" / None. Optional module-level
functions: `max_lookback(params)` and `validate(block)`.

The store never imports the .py files itself — loading is done by composer.py
via `importlib.util.spec_from_file_location` so nothing is added to sys.path.

A read failure here is not a local problem: `composer.load_catalog` treats "which
custom blocks exist" as an input to catalog pruning, so reporting an unreadable
registry as an empty one deletes strategies. Hence `RegistryUnavailable` — an
I/O failure is raised, never flattened into "no blocks".

Wiki References
---------------
Bkz: [[strategy_and_actor]], [[nau_guvenlik_dayaniklilik_duzeltmeleri]]

Block codes are imported at run time; each block is a single function (`evaluate`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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


class RegistryUnavailable(RuntimeError):
    """registry.json exists but could not be read (transient I/O failure).

    Distinct from "registry is empty": callers must NOT treat this as "no custom
    blocks exist", because acting on that assumption deletes registrations
    (a read-modify-write here, catalog pruning in composer.load_catalog).
    """


# A concurrent atomic replace() from save/delete can momentarily deny a reader on
# Windows (sharing violation). The window is sub-millisecond, so a couple of
# short retries clear it.
_READ_RETRIES = 4
_READ_RETRY_SLEEP = 0.05


def _read_registry() -> dict[str, dict[str, Any]]:
    """Return the registry mapping. Raises RegistryUnavailable on read failure.

    Only a genuine PARSE failure (unreadable JSON / wrong shape) quarantines the
    file to `.json.bak`. Any I/O error is transient by assumption and is retried,
    then raised — the previous behaviour quarantined on *any* exception, so a
    single Windows sharing violation during a concurrent save renamed the whole
    registry away and every custom block registration was lost.
    """
    if not REGISTRY_FILE.exists():
        return {}
    last_err: Exception | None = None
    for attempt in range(_READ_RETRIES):
        try:
            raw = REGISTRY_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            last_err = e
            time.sleep(_READ_RETRY_SLEEP * (attempt + 1))
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("registry.json is not a dict")
            return data
        except (ValueError, TypeError) as e:
            # Corrupt content: keep it as .bak so the registry can be rebuilt
            # from the .py files, and start empty.
            corrupt = REGISTRY_FILE.with_suffix(".json.bak")
            try:
                REGISTRY_FILE.replace(corrupt)
            except OSError:
                pass
            log.warning("registry.json unparsable (%s) — quarantined to %s", e, corrupt)
            return {}
    raise RegistryUnavailable(f"cannot read {REGISTRY_FILE}: {last_err}")


def _write_registry(reg: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    # Atomic write: first write to a tmp file, then rename
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    # UTF-8 pinned to match the read side: prompts/meta carry non-ASCII (Turkish
    # text, arrows) and the Windows locale codec would raise on write.
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
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
    # Locked like the writers: an unlocked read could observe the registry mid
    # read-modify-write on another thread (agent worker vs. /studio page load).
    with _STORE_LOCK:
        reg = _read_registry()
    out = []
    for name, info in reg.items():
        if not include_ephemeral and name.startswith(_EPHEMERAL_PREFIXES):
            continue
        out.append({"name": name, **info})
    return out


def get_custom(name: str) -> dict[str, Any] | None:
    with _STORE_LOCK:
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
    # Overwriting a name with DIFFERENT code silently rewrites every strategy
    # that already references it — including ones a robustness scan has already
    # certified (see the agent's per-round block naming). Legitimate callers do
    # overwrite (re-saving an edited block), so this is a warning, not a refusal;
    # what it must never be is invisible.
    try:
        if path.exists():
            _prev = path.read_text(encoding="utf-8")
            if _prev != code:
                logging.warning(
                    "custom block %r overwritten with different code "
                    "(%d → %d chars) — specs referencing this name now run the "
                    "new logic",
                    name,
                    len(_prev),
                    len(code),
                )
    except OSError:
        pass
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


def save_custom_batch(blocks: list[dict[str, Any]]) -> list[Path]:
    """Persist a related set of blocks as one registry transaction.

    Files are staged first and the registry is replaced only after every write
    succeeds.  If anything fails, newly-created files are removed and replaced
    files are restored.  AUTO uses this for entry/exit pairs so a failed exit
    can never leave an orphan entry registered.
    """
    if not blocks:
        return []
    names = [str(item.get("name") or "") for item in blocks]
    if len(set(names)) != len(names) or any(not is_valid_name(name) for name in names):
        raise ValueError("batch contains duplicate or invalid custom block names")
    _ensure_dir()
    with _STORE_LOCK:
        reg = _read_registry()
        old_files: dict[str, str | None] = {}
        paths: list[Path] = []
        try:
            for item, name in zip(blocks, names):
                path = module_path(name)
                old_files[name] = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text(str(item.get("code") or ""), encoding="utf-8")
                paths.append(path)
                reg[name] = {
                    "meta": dict(item.get("meta") or {}),
                    "module_file": path.name,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "prompt": str(item.get("prompt") or ""),
                }
            _write_registry(reg)
            return paths
        except Exception:
            for name, previous in old_files.items():
                path = module_path(name)
                try:
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(previous, encoding="utf-8")
                except OSError:
                    log.exception("could not roll back custom block %s", name)
            raise


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
