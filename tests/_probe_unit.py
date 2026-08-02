"""Stand-in for ``parallel_exec._run_unit`` used by the pool timeout test.

It lives in a file of its own, checked in, for one reason: the function is
pickled BY REFERENCE, so every spawn worker imports this module to unpickle the
call item. Writing it at call time raced with that import — a worker could start
before the file was flushed, or after the test's ``finally`` unlinked it, and
the batch failed with a bare ModuleNotFoundError that had nothing to do with the
behaviour under test.

Kept import-light on purpose: a spawn worker pays this import on every unit.
"""

from __future__ import annotations


def probe_run_unit(unit: dict) -> dict:
    """Return a fixed payload, optionally sleeping ``_probe_sleep`` seconds."""
    import time

    s = float(unit.get("_probe_sleep", 0.0))
    if s:
        time.sleep(s)
    return {"key": unit["key"], "metrics": {"ret": 1.0}, "error": None, "n_trades": 3}
