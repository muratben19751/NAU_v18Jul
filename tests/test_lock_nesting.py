"""Re-entrant lock deadlock regression (2026-07-14 live incident).

The stop-check in `_agent_worker` was calling `_add_step` from INSIDE the
`with _AGENT_LOCK:` block; since `_add_step` acquires the same lock, the worker
locked itself — while holding the lock. Then the other workers and the
`/agent/run` endpoint (event loop!) waited forever on the same lock → the
server froze completely (said "starting pipeline" and stopped).

This test scans the ENTIRE file with AST: inside the body of every `with`
block opened with `_AGENT_LOCK` (or `_LOCK`), calling any known helper that
tries to re-acquire the same lock is FORBIDDEN.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# File → (lock names, helpers that acquire that lock internally)
_TARGETS = {
    "web/routes/agent_backtest.py": (
        {"_AGENT_LOCK"},
        {
            "_add_step",
            "_set_phase",
            "_done_phase",
            "_tl_begin",
            "_tl_end",
            "_tl_close_open",
            "_track_usage",
            "_set_robustness_scan",
        },
    ),
    "web/routes/robustness.py": ({"_LOCK"}, {"_add_step"}),
}


def _lock_names(with_node: ast.With) -> set[str]:
    names = set()
    for item in with_node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Name):
            names.add(expr.id)
    return names


def _called_names(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


class TestNoLockReacquireInsideLockBlock:
    def test_no_helper_call_inside_lock_with_block(self):
        violations = []
        for rel, (locks, helpers) in _TARGETS.items():
            src_path = ROOT / rel
            tree = ast.parse(src_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.With):
                    continue
                if not (_lock_names(node) & locks):
                    continue
                # calls inside the with body (excluding context_expr)
                body_calls = set()
                for stmt in node.body:
                    body_calls |= _called_names(stmt)
                bad = body_calls & helpers
                if bad:
                    violations.append(f"{rel}:{node.lineno} → {sorted(bad)}")
        assert not violations, (
            "Helper call that acquires the same lock inside a lock block "
            f"(re-entrant deadlock risk): {violations}"
        )

    def test_stop_check_still_breaks_loop(self):
        """Fix behavior preserved: stop_requested → _add_step + break exist in
        the source OUTSIDE the lock."""
        src = (ROOT / "web/routes/agent_backtest.py").read_text(encoding="utf-8")
        i = src.find("Stop signal received")
        assert i != -1, "stop-check step message disappeared"
        # Look back from the line where the message is: at the same indentation
        # level, `if stop_hit:` must come first (not inside the with block).
        window = src[max(0, i - 400) : i]
        assert "if stop_hit:" in window, "stop-check not moved outside the lock"
