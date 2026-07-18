"""Re-entrant lock deadlock regresyonu (2026-07-14 canlı olayı).

`_agent_worker`'daki stop-check, `with _AGENT_LOCK:` bloğu İÇİNDEN
`_add_step` çağırıyordu; `_add_step` de aynı kilidi aldığından worker kendini
kilitledi — kilidi tutarak. Ardından diğer worker'lar ve `/agent/run`
endpoint'i (event loop!) aynı kilitte sonsuza dek bekledi → sunucu tamamen
dondu ("pipeline başlatılıyor deyip durdu").

Bu test AST ile dosyanın TAMAMINI tarar: `_AGENT_LOCK` (veya `_LOCK`) ile
açılan her `with` bloğunun gövdesinde, aynı kilidi yeniden almaya çalışan
bilinen helper'ların çağrısı YASAKTIR.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Dosya → (kilit adları, o kilidi kendi içinde alan helper'lar)
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
                # with gövdesi (context_expr hariç) içindeki çağrılar
                body_calls = set()
                for stmt in node.body:
                    body_calls |= _called_names(stmt)
                bad = body_calls & helpers
                if bad:
                    violations.append(f"{rel}:{node.lineno} → {sorted(bad)}")
        assert not violations, (
            "Kilit bloğu içinde aynı kilidi alan helper çağrısı (re-entrant "
            f"deadlock riski): {violations}"
        )

    def test_stop_check_still_breaks_loop(self):
        """Fix davranışı korumalı: stop_requested → _add_step + break kilit
        DIŞINDA, kaynakta mevcut."""
        src = (ROOT / "web/routes/agent_backtest.py").read_text(encoding="utf-8")
        i = src.find("Durdurma sinyali alındı")
        assert i != -1, "stop-check adım mesajı kayboldu"
        # Mesajın bulunduğu satırdan geriye bak: aynı girinti düzeyinde
        # önce `if stop_hit:` gelmeli (with bloğunun içinde değil).
        window = src[max(0, i - 400) : i]
        assert "if stop_hit:" in window, "stop-check kilit dışına taşınmamış"
