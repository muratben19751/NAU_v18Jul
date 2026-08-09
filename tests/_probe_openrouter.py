"""Stand-in for ``agent._openrouter_process_main`` used by the killable-process
characterization tests.

It lives in a file of its own, checked in, for the same reason as
``tests/_probe_unit.py``: ``multiprocessing``'s "spawn" context pickles the
``target=`` callable BY REFERENCE, so the spawned child re-imports whatever
module currently defines it to unpickle the call — a function defined inline
in the test file, or a lambda/closure, is not importable by the child and
would fail before the behavior under test ever runs.

Kept import-light on purpose: a spawn worker pays this import on every call.

Wiki References
---------------
See: [[llm_maliyet_kaldiraclari]].
"""

from __future__ import annotations


def probe_openrouter_main(conn, config: dict, request: dict) -> None:
    """Signature-matched stand-in for ``agent._openrouter_process_main``.

    Driven entirely through ``request["_probe_mode"]`` so the real
    ``_run_openrouter_killable`` orchestrator (parent-side Pipe/Process/poll/
    kill mechanics) runs completely unmodified against this — only the
    child's own body (a real network call to OpenRouter) is swapped out.
    """
    import time
    from pathlib import Path

    mode = request.get("_probe_mode", "ok")
    try:
        if mode == "ok":
            conn.send(
                {
                    "ok": True,
                    "text": request.get("_probe_text", "probe response"),
                    "finish_reason": "stop",
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "cost_usd": 0.0033,
                }
            )
        elif mode == "error":
            conn.send(
                {
                    "ok": False,
                    "type": request.get("_probe_error_type", "RateLimitError"),
                    "message": request.get("_probe_error_message", "rate limited"),
                    "status_code": request.get("_probe_status_code", 429),
                }
            )
        elif mode == "hang":
            # Sleep FIRST, marker write second: if the parent kills this
            # process (proc.kill()) while it is asleep, the marker is never
            # written -- proving the child was actually terminated, not
            # merely that the parent's poll loop gave up waiting on it.
            time.sleep(float(request.get("_probe_sleep", 30.0)))
            Path(request["_probe_marker_path"]).write_text("reached", encoding="utf-8")
            conn.send(
                {"ok": True, "text": "should never be seen", "finish_reason": "stop"}
            )
        else:
            raise ValueError(f"unknown _probe_mode: {mode!r}")
    finally:
        conn.close()
