"""Dependency-free shared constants — every module can read it without circular imports.

- ``STARTING_CASH``: the shared starting cash for backtest.py and composer.py
  (kept here so it comes from a single source; a duplicated constant could silently diverge).
- ``NO_WINDOW_FLAGS``: the ``creationflags`` value that prevents subprocesses on
  Windows from opening a console window.
"""

from __future__ import annotations

import os
import subprocess

STARTING_CASH = 10_000.0

# On Windows, when a CONSOLE application (claude CLI, bash/gunzip/awk) is launched,
# a terminal window opens and closes on every call — even if the server runs
# consoleless via pythonw, because a consoleless parent creates a NEW window for a
# console-bearing child. CREATE_NO_WINDOW never creates the console; `startupinfo`/`windowsHide`
# is ignored by Windows Terminal on this machine, so this is the only reliable way.
# On POSIX it MUST be 0 — subprocess rejects non-zero creationflags there.
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
