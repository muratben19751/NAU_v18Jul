"""`auto/` alan katmanı web katmanına bağlanmasın — ve sandbox onu web'siz koşsun.

DeepR 2026-08-11 [YÜKSEK]: robustluk suite'i `web/routes/agent_backtest.py`
içindeydi; `sandbox._robustness_child` (ayrı bir süreç) onu
`import web.routes.agent_backtest as ab` ile çekip `ab._IPC_Q = q` diyerek başka
bir modülün private global'ini set ediyordu. Yani HTTP servis etmeyen bir worker
sırf suite'i çağırabilmek için tüm FastAPI router ağacını yüklüyordu ve statik
import grafiğinde iki gerçek döngü vardı:

    sandbox -> web.routes.agent_backtest -> sandbox
    parallel_exec -> sandbox -> web.routes.agent_backtest -> parallel_exec

Suite `auto/robustness.py`'ye taşındı. Bu dosya iki şeyi bağlar:

  1. `auto/*` içinde hiçbir yerde (üst seviye VEYA fonksiyon içi gecikmeli
     import) `web...` import'u olmasın — AST ile denetlenir, yani kural
     import-zamanı yan etkisine değil kaynağa bakar,
  2. `sandbox`'ı import etmek web katmanını sürüklemesin (gerileme kanıtı:
     eskiden bu iddia yanlıştı).

Wiki References: [[webapp_module_map]], [[nau_deepr_mimari_katman_ayrimi]]
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imported_modules(tree: ast.AST) -> set[str]:
    """Her `import x` / `from x import y` hedefi — fonksiyon içindekiler dâhil."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Göreli import (`from . import x`) kendi paketini işaret eder.
            if node.level == 0 and node.module:
                out.add(node.module)
    return out


class TestAutoPackageHasNoWebDependency:
    def test_no_module_under_auto_imports_web(self):
        violations = []
        files = sorted((ROOT / "auto").rglob("*.py"))
        assert files, "auto/ paketi bulunamadı — test kendini kandırmasın"
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in _imported_modules(tree):
                if name == "web" or name.startswith("web."):
                    violations.append(f"{path.relative_to(ROOT)} → {name}")
        assert not violations, (
            "auto/ alan katmanı web katmanını import ediyor "
            f"(bağımlılık yönü tersine dönmüş): {violations}"
        )

    def test_sandbox_no_longer_imports_the_agent_route_module(self):
        """Kaynak seviyesinde: sandbox.py'de `web.routes.agent_backtest` geçmesin."""
        tree = ast.parse((ROOT / "sandbox.py").read_text(encoding="utf-8"))
        offenders = {
            n for n in _imported_modules(tree) if n == "web" or n.startswith("web.")
        }
        assert not offenders, (
            f"sandbox.py bir web modülü import ediyor: {sorted(offenders)}"
        )


class TestImportingSandboxDoesNotPullInTheWebLayer:
    def test_fresh_interpreter_stays_web_free(self):
        """Ayrı bir yorumlayıcıda `import sandbox` → sys.modules'te web yok.

        Süreç içi kontrol işe yaramaz: pytest zaten server'ı import etmiş
        olabilir. Bu yüzden temiz bir alt süreçte ölçülür.
        """
        code = (
            "import sys, sandbox, auto.robustness;"
            "print(sorted(m for m in sys.modules "
            "if m == 'web' or m.startswith('web.') or m == 'fastapi'))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]", (
            f"sandbox/auto import'u web katmanını sürükledi: {proc.stdout.strip()}"
        )
