"""`web/routes/*` ile `server` arasındaki bağımlılık tek yönlü kalsın.

DeepR 2026-08-11 [ORTA]: `server.py` her router'ı import ediyor, router'lar da
`templates` / `get_market_info` / `get_bars` için geri `server`'a bakıyordu.
Döngü olduğu için o import'lar modül seviyesine konulamamış, **54 tanesi
fonksiyon gövdesine** saklanmıştı (birinin yanında ``# local import: avoid
circular`` notu bile vardı — smell biliniyordu, çözülmemişti).

Paylaşılan şey `web/templating.py`'ye indi. Bu dosya kuralı kaynağa bakarak
denetler, çünkü fonksiyon-içi import çalışma zamanına kadar görünmez:

  1. `web/**` altında hiçbir modül `server`'ı import etmesin — fonksiyon içinde
     bile (döngünün geri dönüşünü tam olarak orası gizliyordu),
  2. `web/templating.py` yaprak kalsın: ne `server`, ne `web.routes`,
  3. bir route modülünü tek başına import etmek FastAPI app'ini, erişim
     kapısını ve startup lifespan'ini ayağa kaldırmasın (gerileme kanıtı:
     eskiden bu iddia yanlıştı).

Kardeş kural: [[test_auto_layer_is_web_free]] — aynı duruşun motor katmanı için
olan hâli.

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
            if node.level == 0 and node.module:
                out.add(node.module)
    return out


def _web_modules() -> list[Path]:
    files = sorted((ROOT / "web").rglob("*.py"))
    assert files, "web/ paketi bulunamadı — test kendini kandırmasın"
    return files


class TestWebLayerDoesNotImportServer:
    def test_no_module_under_web_imports_server(self):
        violations = []
        for path in _web_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in _imported_modules(tree):
                if name == "server" or name.startswith("server."):
                    violations.append(f"{path.relative_to(ROOT)} → {name}")
        assert not violations, (
            "web katmanı `server`'ı import ediyor — çift yönlü bağımlılık geri "
            f"geldi. Ortak şeyi `web/templating.py`'ye taşıyın: {violations}"
        )

    def test_the_route_modules_really_were_scanned(self):
        """Çıpa: yukarıdaki test boş bir dosya listesiyle 'geçmesin'."""
        names = {p.name for p in _web_modules()}
        for expected in ("strategy.py", "backtest.py", "agent_backtest.py"):
            assert expected in names, f"{expected} taranmadı"

    def test_templating_is_a_leaf(self):
        """`web/templating.py` ne server'ı ne de route'ları tanımalı."""
        tree = ast.parse((ROOT / "web" / "templating.py").read_text(encoding="utf-8"))
        offenders = {
            n
            for n in _imported_modules(tree)
            if n == "server" or n.startswith(("server.", "web.routes"))
        }
        assert not offenders, f"web/templating.py yaprak değil: {sorted(offenders)}"


class TestImportingARouteDoesNotBootTheServer:
    def test_fresh_interpreter_gets_no_fastapi_app(self):
        """Temiz bir alt süreçte tek bir route modülü import et.

        Süreç içi kontrol işe yaramaz: pytest zaten `server`'ı import etmiş
        olabilir. `server` modülü `sys.modules`'te belirirse döngü geri gelmiş
        demektir — o import FastAPI app'ini, middleware'leri, erişim kapısını
        ve `lifespan`'i de beraberinde getirir.
        """
        code = "import sys, web.routes.strategy;print('server' in sys.modules)"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False", (
            "bir route modülünü import etmek `server`'ı da yükledi — "
            "çift yönlü bağımlılık geri geldi"
        )
