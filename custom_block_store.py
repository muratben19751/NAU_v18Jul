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
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp
from typing import Any

log = logging.getLogger(__name__)

# M(store): in-process lock for registry.json + block .py writes — if the agent
# worker thread (entry+exit back to back) runs save/delete concurrently with
# /lab or /strategy, a read-modify-write lost-update could silently destroy a
# block's registration (RLock: reentrant within the same thread).
_STORE_LOCK = threading.RLock()
_REGISTRY_GUARD_STATE = threading.local()

STORE_DIR = Path.home() / ".cache" / "nautilus_web_app" / "custom_blocks"
REGISTRY_FILE = STORE_DIR / "registry.json"
_REGISTRY_LOCK_FILE = "registry.lock"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Auto-generated block names carry these prefixes: ``desc_*`` from the AI
# suggest/edit flows, ``agnt_*`` from the autonomous agent. User-authored blocks
# are named via _slugify(label) and never start with these. Used by list_custom
# to keep the UI list free of bulk ephemeral blocks (see the 06 · Custom Blocks
# panel bloat found in the /studio QA pass).
_EPHEMERAL_PREFIXES = ("desc_", "agnt_")

# AUTO names are part of the persisted artifact contract.  Older runs used
# ``agnt_e_<run>_<iteration>``; current runs include ``_r<round>`` so a single
# run cannot overwrite its earlier candidate.  Keep both shapes readable: a
# restart must be able to migrate safe legacy artifacts before it loads them.
_AGENT_BLOCK_RE = re.compile(
    r"^agnt_(?P<role>[ex])_(?P<run>[a-z0-9]{8})(?:_r[1-9][0-9]*)?_[0-9]+$"
)


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


@contextmanager
def _registry_guard():
    """Serialize registry RMW operations across threads and processes.

    The lock is separate from the JSON payload, so replacing registry.json
    atomically never invalidates a lock held by a concurrent writer.
    """
    with _STORE_LOCK:
        # ``save_custom_batch(..., validate=...)`` can register the blocks it
        # just staged. The OS advisory lock is not re-entrant, so keep it held
        # only once for nested work in the same thread.
        depth = getattr(_REGISTRY_GUARD_STATE, "depth", 0)
        if depth:
            _REGISTRY_GUARD_STATE.depth = depth + 1
            try:
                yield
            finally:
                _REGISTRY_GUARD_STATE.depth = depth
            return

        handle = None
        locked = False
        try:
            _ensure_dir()
            lock_path = STORE_DIR / _REGISTRY_LOCK_FILE
            lock_path.touch(exist_ok=True)
            handle = lock_path.open("r+b")
            if os.name == "nt":
                import msvcrt

                if lock_path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            else:  # pragma: no cover - Linux deployments
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise RegistryUnavailable(
                f"cannot lock custom block registry: {exc}"
            ) from exc

        try:
            _REGISTRY_GUARD_STATE.depth = 1
            yield
        finally:
            _REGISTRY_GUARD_STATE.depth = 0
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - Linux deployments
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    log.exception("could not release custom block registry lock")
            if handle is not None:
                handle.close()


def _read_registry() -> dict[str, dict[str, Any]]:
    """Return the registry mapping or raise ``RegistryUnavailable``.

    Only a genuine PARSE failure (unreadable JSON / wrong shape) quarantines the
    file to `.json.bak` and then raises; it is never treated as empty. Any I/O error is transient by assumption and is retried,
    then raised — the previous behaviour quarantined on *any* exception, so a
    single Windows sharing violation during a concurrent save renamed the whole
    registry away and every custom block registration was lost.
    """
    if not REGISTRY_FILE.exists():
        # A malformed registry is moved aside below for operator recovery. Do
        # not let a later caller mistake that quarantined state for a newly
        # created, empty catalog and prune every strategy that uses a custom
        # block.
        if REGISTRY_FILE.with_suffix(".json.bak").exists():
            raise RegistryUnavailable("registry.json is quarantined as malformed")
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
            # Preserve bad content for recovery, but never start empty:
            # composer would otherwise prune dependent catalog entries.
            corrupt = REGISTRY_FILE.with_suffix(".json.bak")
            try:
                REGISTRY_FILE.replace(corrupt)
            except OSError:
                pass
            log.warning("registry.json unparsable (%s) — quarantined to %s", e, corrupt)
            raise RegistryUnavailable(
                f"registry.json is malformed; refusing empty-registry fallback: {e}"
            ) from e
    raise RegistryUnavailable(f"cannot read {REGISTRY_FILE}: {last_err}")


def _write_registry(reg: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    # Atomic write with a unique sibling temp. A fixed .json.tmp lets two
    # processes clobber one another's staging file even if replace() is atomic.
    fd, tmp_name = mkstemp(prefix="registry.", suffix=".tmp", dir=STORE_DIR)
    tmp = Path(tmp_name)
    # UTF-8 pinned to match the read side: prompts/meta carry non-ASCII (Turkish
    # text, arrows) and the Windows locale codec would raise on write.
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(reg, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(REGISTRY_FILE)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def is_valid_name(name: str) -> bool:
    """Names must be lowercase snake_case, start with a letter, 2-40 chars."""
    return bool(name and _NAME_RE.match(name))


def agent_block_identity(name: str) -> dict[str, str] | None:
    """Return immutable AUTO identity encoded in a generated block name.

    ``None`` means this is a user/describe block, for which no automatic
    lifecycle or role policy is inferred.  The role is deliberately inferred
    from the *name*, not an LLM-provided metadata field: legacy AUTO files did
    not persist that field and must not be reusable in the opposite role.
    """
    match = _AGENT_BLOCK_RE.fullmatch(str(name or ""))
    if match is None:
        return None
    return {
        "role": "entry" if match.group("role") == "e" else "exit",
        "run_id": match.group("run"),
    }


def ensure_agent_role_metadata(name: str) -> str | None:
    """Backfill the role of a legacy AUTO block, or reject a forged conflict.

    Returns the inferred role for AUTO names and ``None`` for every other
    custom block.  The registry update is atomic and idempotent.  A stored role
    that conflicts with the reserved ``agnt_e``/``agnt_x`` prefix is unsafe, so
    callers must quarantine it rather than guessing which semantics to run.
    """
    identity = agent_block_identity(name)
    if identity is None:
        return None
    inferred = identity["role"]
    with _registry_guard():
        reg = _read_registry()
        info = reg.get(name)
        if info is None:
            raise ValueError(f"no such custom block: {name}")
        meta = dict(info.get("meta") or {})
        stored = meta.get("role")
        if stored in {"entry", "exit"} and stored != inferred:
            raise ValueError(
                f"AUTO block {name!r} has conflicting role metadata "
                f"{stored!r}; name requires {inferred!r}"
            )
        if stored != inferred:
            meta["role"] = inferred
            info = dict(info)
            info["meta"] = meta
            reg[name] = info
            _write_registry(reg)
    return inferred


def list_agent_blocks(run_id: str | None = None) -> list[dict[str, Any]]:
    """List AUTO artifacts with their parsed identity; this never deletes.

    Deletion needs catalog-awareness, which belongs to the AUTO orchestrator.
    Exposing a precise list lets it retain promoted dependencies while cleaning
    failed, degraded, or abandoned candidates without a broad glob delete.
    """
    wanted = str(run_id or "")
    if run_id is not None and not re.fullmatch(r"[a-z0-9]{8}", wanted):
        raise ValueError("run_id must be the eight-character AUTO id")
    out: list[dict[str, Any]] = []
    for item in list_custom(include_ephemeral=True):
        identity = agent_block_identity(item["name"])
        if identity is None or (run_id is not None and identity["run_id"] != wanted):
            continue
        out.append({**item, **identity})
    return out


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
    with _registry_guard():
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


@contextmanager
def _registry_transaction():
    """Snapshot the registry + every touched file; roll both back on failure.

    ``save_custom_batch`` and ``delete_custom_batch`` each mutate a set of
    files and the shared registry as one atomic unit — both used to hand-roll
    the same "snapshot old registry + old file contents, write, and on
    failure restore every file and the registry" sequence independently.

    Yields ``(reg, old_files)``: ``reg`` is the live registry dict to mutate
    in place; ``old_files`` is a ``name -> previous content | None`` map the
    caller must populate (``None`` meaning the file didn't exist yet) *before*
    writing/deleting each file, so a rollback knows whether to restore it or
    remove it. Call ``_write_registry(reg)`` yourself once all files are
    written — the transaction only *restores* on an exception, it doesn't
    commit for you.
    """
    reg = _read_registry()
    old_registry = dict(reg)
    old_files: dict[str, str | None] = {}
    try:
        yield reg, old_files
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
        try:
            _write_registry(old_registry)
        except OSError:
            log.exception("could not roll back custom block registry")
        raise


def save_custom_batch(
    blocks: list[dict[str, Any]],
    *,
    validate: Callable[[], None] | None = None,
) -> list[Path]:
    """Persist a related set of blocks as one registry transaction.

    Files are staged first and the registry is replaced only after every write
    succeeds. ``validate`` runs while the store transaction is still open; AUTO
    uses it to register the pair in-memory and validate the composed spec. If
    writing, registration, or validation fails, both files *and registry* are
    restored. This prevents a half-visible pair or an ``Unknown block type``
    validation race.
    """
    if not blocks:
        return []
    names = [str(item.get("name") or "") for item in blocks]
    if len(set(names)) != len(names) or any(not is_valid_name(name) for name in names):
        raise ValueError("batch contains duplicate or invalid custom block names")
    _ensure_dir()
    with _registry_guard(), _registry_transaction() as (reg, old_files):
        paths: list[Path] = []
        for item, name in zip(blocks, names, strict=True):
            path = module_path(name)
            old_files[name] = (
                path.read_text(encoding="utf-8") if path.exists() else None
            )
            path.write_text(str(item.get("code") or ""), encoding="utf-8")
            paths.append(path)
            reg[name] = {
                "meta": dict(item.get("meta") or {}),
                "module_file": path.name,
                "generated_at": datetime.now(UTC).isoformat(),
                "prompt": str(item.get("prompt") or ""),
            }
        _write_registry(reg)
        if validate is not None:
            validate()
        return paths


def delete_custom(name: str) -> bool:
    """Remove a custom block from disk, registry, and in-memory BLOCK_REGISTRY."""
    if not is_valid_name(name):
        return False
    with _registry_guard():
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


def delete_custom_batch(names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Atomically remove known custom blocks and unregister them after commit.

    Missing names are harmless. Unlike looping over :func:`delete_custom`, this
    either removes every selected file+registry entry or restores both. AUTO
    uses it for a failed run's entry/exit pair so a crash cannot leave a single
    orphan that is imported on the next server start.
    """
    selected = list(dict.fromkeys(str(name) for name in names))
    if any(not is_valid_name(name) for name in selected):
        raise ValueError("batch contains an invalid custom block name")
    if not selected:
        return []
    with _registry_guard(), _registry_transaction() as (reg, old_files):
        removed = [name for name in selected if name in reg]
        if not removed:
            return []
        for name in removed:
            path = module_path(name)
            old_files[name] = (
                path.read_text(encoding="utf-8") if path.exists() else None
            )
            path.unlink(missing_ok=True)
            del reg[name]
        _write_registry(reg)
    # Do not take composer._REGISTRY_LOCK while the store transaction is open;
    # composer reads the store before taking that lock. Commit first, then make
    # the process-local registry agree with disk.
    try:
        from composer import unregister_custom_block

        for name in removed:
            unregister_custom_block(name)
    except Exception:
        log.exception("could not unregister deleted custom blocks from memory")
    return removed


def cleanup_agent_run(run_id: str, *, keep_names: set[str] | None = None) -> list[str]:
    """Remove non-retained AUTO artifacts for one completed/aborted run.

    This is intentionally explicit rather than a background TTL delete. The
    caller supplies catalog dependencies in ``keep_names``; therefore a winning
    strategy is never broken merely because its source run is old.
    """
    keep = {str(name) for name in (keep_names or set())}
    candidates = [
        item["name"] for item in list_agent_blocks(run_id) if item["name"] not in keep
    ]
    return delete_custom_batch(candidates)
