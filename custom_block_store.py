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

Blok kodları çalıştırma zamanında import edilir; her blok tek fonksiyon (`evaluate`).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# M(store): registry.json + blok .py yazımları için süreç-içi kilit — agent
# worker thread'i (entry+exit art arda) ile /lab veya /strategy eşzamanlı
# save/delete yaparsa read-modify-write kayıp-güncelleme ile bir bloğun
# kaydını sessizce yok edebiliyordu (RLock: aynı thread reentrant).
_STORE_LOCK = threading.RLock()

STORE_DIR = Path.home() / ".cache" / "nautilus_web_app" / "custom_blocks"
REGISTRY_FILE = STORE_DIR / "registry.json"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


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
        # Bozuk registry.json varsa yedekle, boş başla — sonraki write'da sıfırlanmasın diye
        # mevcut .py dosyalarından yeniden oluşturulabilir.
        corrupt = REGISTRY_FILE.with_suffix(".json.bak")
        try:
            REGISTRY_FILE.replace(corrupt)
        except Exception:
            pass
        return {}


def _write_registry(reg: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    # Atomik write: önce tmp dosyaya yaz, sonra rename
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(REGISTRY_FILE)


def is_valid_name(name: str) -> bool:
    """Names must be lowercase snake_case, start with a letter, 2-40 chars."""
    return bool(name and _NAME_RE.match(name))


def list_custom() -> list[dict[str, Any]]:
    """Return list of {name, meta, module_file, generated_at, prompt} entries."""
    reg = _read_registry()
    out = []
    for name, info in reg.items():
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
    # H(store): composer read_text(encoding="utf-8") ile okuyor; yazımda encoding
    # belirtilmezse Windows'ta locale (cp1254) kullanılır → non-ASCII (→, …,
    # tipografik tırnak) içeren LLM kodu UnicodeEncodeError'la patlar ya da
    # blok kalıcı import edilemez olur. UTF-8 sabitle.
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
    with _STORE_LOCK:  # M(store): kilitli RMW — kayıp güncelleme önlemi
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
    # In-memory temizle — aynı session'da silinmiş blok çalışmasın
    try:
        from composer import unregister_custom_block

        unregister_custom_block(name)
    except Exception:
        pass
    return True
