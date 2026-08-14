"""What produced a run: code revision, dependency versions, config overrides.

The AUTO session log answered every question about a run *except* the first one
a reviewer asks a week later — "which code was this?". Metrics, prompts and
decisions were all recorded against an unnamed build, so a behaviour change
between two runs could not be attributed to a commit, a dependency bump or an
env override. This module supplies that missing half.

Two rules shape it:

* **Never block a run.** Every probe is wrapped; a missing ``git``, a detached
  worktree or an unreadable metadata directory degrades to a stated ``None``,
  never an exception in the caller's start-up path.
* **Never log a secret.** Environment overrides are recorded because they
  change behaviour, but any name that looks like a credential is reduced to
  ``"<set>"``/absent. An audit log is read by more people than a shell is.

Wiki References
---------------
See: [[auto_mission_control]], [[webapp_module_map]]
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

from app_constants import NO_WINDOW_FLAGS

# Dependencies whose version can change a backtest's *numbers*, not just its
# speed. A full `pip freeze` would bury these in 200 lines of irrelevance.
_TRACKED_DISTRIBUTIONS = (
    "nautilus_trader",
    "pandas",
    "numpy",
    "anthropic",
    "fastapi",
    "pyarrow",
)

# Prefixes of env vars that steer this app. Everything else in the environment
# belongs to the machine, not to the run.
_ENV_PREFIXES = ("NAU_", "NAUTILUS_")

# A name containing any of these is a credential: record that it was set, never
# what it was set to.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

_GIT_TIMEOUT_S = 5.0


def _git(*args: str, cwd: Path) -> str | None:
    """One git command, or None if git/the repo is unavailable."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            creationflags=NO_WINDOW_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_revision(repo_root: Path | None = None) -> dict:
    """Commit, branch and whether the tree was dirty when the run started.

    ``dirty`` is the field that matters: a run from an uncommitted tree cannot
    be reproduced from its SHA, and saying so is the difference between an
    audit record and a decoration.
    """
    root = Path(repo_root or Path(__file__).resolve().parent)
    sha = _git("rev-parse", "HEAD", cwd=root)
    if sha is None:
        return {"available": False}
    status = _git("status", "--porcelain", cwd=root)
    return {
        "available": True,
        "sha": sha,
        "short": sha[:9],
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root) or "",
        "committed_at": _git("log", "-1", "--format=%cI", cwd=root) or "",
        # status is None only when the command itself failed — an unknown
        # cleanliness must not be reported as clean.
        "dirty": None if status is None else bool(status),
        "dirty_files": len([ln for ln in (status or "").splitlines() if ln.strip()]),
    }


def dependency_versions() -> dict:
    """Installed versions of the dependencies that can move a backtest's numbers."""
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str | None] = {}
    for dist in _TRACKED_DISTRIBUTIONS:
        try:
            out[dist] = version(dist)
        except (PackageNotFoundError, ValueError):
            out[dist] = None
    return out


def env_overrides(environ: dict | None = None) -> dict:
    """App env vars in effect, with credential-shaped values withheld.

    Only *set* variables appear: an absent override is the documented default
    and listing forty of them would drown the two that were actually changed.
    """
    src = os.environ if environ is None else environ
    out = {}
    for name, value in sorted(src.items()):
        if not name.startswith(_ENV_PREFIXES):
            continue
        upper = name.upper()
        if any(marker in upper for marker in _SECRET_MARKERS):
            out[name] = "<set>"
        else:
            out[name] = str(value)[:200]
    return out


def process_rss_bytes() -> int | None:
    """Resident memory of THIS process, or None where it cannot be read.

    Written by hand rather than pulled from ``psutil`` because psutil is not a
    dependency of this app and a heartbeat is not worth one. Both branches are
    stdlib; anything unexpected degrades to None, which the reader renders as
    "ölçülemedi" — a heartbeat that raises would be worse than no heartbeat.
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _COUNTERS()
            counters.cb = ctypes.sizeof(_COUNTERS)
            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            # restype/argtypes are NOT optional here: ctypes defaults to c_int,
            # which truncates the 64-bit pseudo-handle and the call fails.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_COUNTERS),
                wintypes.DWORD,
            ]
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            return int(counters.WorkingSetSize) if ok else None
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        return int(rss if sys.platform == "darwin" else rss * 1024)
    except Exception:
        return None


def run_provenance(repo_root: Path | None = None, environ: dict | None = None) -> dict:
    """The full "what produced this run" record, safe to write to an audit log."""
    from app_constants import DATA_DIR

    return {
        "git": git_revision(repo_root),
        "packages": dependency_versions(),
        "env_overrides": env_overrides(environ),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "data_dir": str(DATA_DIR),
        "cwd": os.getcwd(),
    }
