"""`nau_data/` yamalanabilir yüzeye DOKUNMASIN — sınırı yorum değil AST korusun.

`nau_data/__init__.py` paketin giriş şartını yazıyor: buraya yalnız durum
taşımayan kod girer. Kardeş dosya `test_data_module_surface_is_stable.py`
sınırın `data` TARAFINI tutuyor — yamalanan 45 ad hâlâ `data`'nın özniteliği,
yamalanan FONKSİYONLAR hâlâ `data.py`'de tanımlı. Bu dosya sınırın öbür
tarafını tutar ve kardeşinin yapısal olarak göremediği deliği kapatır:

    # nau_data/paths.py
    NAUTILUS_CATALOG_DIR = Path.home() / ".cache" / "nautilus_web_app"
    # data.py
    from nau_data.paths import NAUTILUS_CATALOG_DIR

Kardeş test bundan memnun kalır: ad hâlâ `data`'nın özniteliğidir ve fonksiyon
olmadığı için "data.py'de tanımlı mı" kontrolüne hiç girmez. Ama
``monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path)`` artık yalnız
kabuktaki bağı değiştirir; değeri gerçekten okuyan `nau_data` modülü kendi
modül-global'ini okumaya devam eder ([[import_aninda_yakalanan_referans]]).
Süit YEŞİL kalır ve testler kullanıcının gerçek `~/.cache/nautilus_web_app`
kataloğuna yazmaya başlar — DeepR 2026-08-11 turunun tek sert kuralının, hem de
sessizce, ihlali.

Kural bu yüzden ada değil YERE bakar: yamalanan adların hiçbiri `nau_data/`
altında geçemez ve hiçbir modül `data`'yı import edemez. Bölmenin kalanı (dört
kaynağın kendi modüllerine ayrılması) bu iki dosyanın çizdiği koridorda
ilerler; kırmızıya dönen taraf taşınan şeyin yanlış olduğunu söyler.

Wiki References: [[webapp_module_map]], [[import_aninda_yakalanan_referans]]
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_data_module_surface_is_stable import _patched_names

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "nau_data"


@pytest.fixture(scope="module")
def patched() -> set[str]:
    names = _patched_names()
    # Çıpa: tarama bozulursa boş küme ile "geçmesin".
    assert len(names) >= 30, f"yamalanan ad sayısı beklenmedik ({len(names)})"
    return names


def _modules() -> list[Path]:
    mods = sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(mods) >= 3, f"`nau_data/` taraması çok az modül buldu ({len(mods)})"
    return mods


def _imported_roots(tree: ast.AST) -> set[str]:
    """Modülün import ettiği TEPE paket adları (`import a.b` → ``a``).

    Göreli import'lar (``from .atomic_io import x``) paket içidir, sayılmaz.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _mentioned(tree: ast.AST) -> set[str]:
    """Modülde GEÇEN her ad: okuma, atama, tanım, import takma adı, öznitelik."""
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            seen |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return seen


def _dynamic_imports(tree: ast.AST) -> set[str]:
    """``import_module("x")`` / ``__import__("x")`` ile istenen tepe adlar."""
    asked: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if called not in {"import_module", "__import__"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            asked.add(first.value.split(".")[0])
    return asked


def _parsed() -> list[tuple[str, ast.AST]]:
    return [
        (m.relative_to(ROOT).as_posix(), ast.parse(m.read_text(encoding="utf-8")))
        for m in _modules()
    ]


class TestTheDataLayerNeverReachesBackIntoData:
    """`nau_data` → `data` yönünde bağ YOK: yaprak katman, döngü riski sıfır."""

    def test_no_module_under_nau_data_imports_data(self):
        offenders = [rel for rel, tree in _parsed() if "data" in _imported_roots(tree)]
        assert not offenders, (
            "`nau_data/` altındaki şu modüller `data`'yı import ediyor: "
            f"{offenders}. Paket yaprak olmalı — `data`'dan bir şeye ihtiyaç "
            "duyuyorsa taşınan kod durum taşıyordur, bu pakete girmemeli."
        )

    def test_no_module_smuggles_data_in_dynamically(self):
        offenders = [rel for rel, tree in _parsed() if "data" in _dynamic_imports(tree)]
        assert not offenders, (
            "Şu modüller `data`'yı `import_module`/`__import__` ile içeri "
            f"alıyor: {offenders}. Statik import yasağını dolaşmak, sınırı "
            "AST'ye görünmez kılmaktan başka bir şey yapmaz."
        )

    def test_the_import_check_would_actually_catch_data(self):
        """Çıpa: yasak gerçekten yakalanıyor mu?"""
        assert "data" in _imported_roots(ast.parse("import data\n"))
        assert "data" in _imported_roots(ast.parse("from data import CACHE_DIR\n"))
        assert "data" in _dynamic_imports(ast.parse("import_module('data')\n"))
        # Göreli import paket içidir — yanlışlıkla yakalanmamalı.
        assert "data" not in _imported_roots(ast.parse("from .bar_math import x\n"))


class TestThePatchedNamesDoNotLeakIntoTheDataLayer:
    """Yamalanan bir ad buraya taşınırsa yama sessizce etkisiz kalır."""

    def test_no_patched_name_appears_under_nau_data(self, patched):
        leaks = {
            rel: sorted(_mentioned(tree) & patched)
            for rel, tree in _parsed()
            if _mentioned(tree) & patched
        }
        assert not leaks, (
            "Testlerin `data.<ad>` olarak monkeypatch ettiği şu adlar artık "
            f"`nau_data/` altında geçiyor: {leaks}. Değeri bu katmandan okuyan "
            "her satır yamayı GÖRMEZ — kabuktaki bağ değişir, alt modül kendi "
            "global'ini okur. Ya adı `data.py`'de bırakın ya da okumayı çağrı "
            "anında `data.<ad>` üzerinden yapın."
        )

    def test_the_leak_check_would_actually_catch_a_moved_root(self, patched):
        """Çıpa: en tehlikeli sızıntı (depolama kökü) gerçekten yakalanıyor mu?"""
        assert "NAUTILUS_CATALOG_DIR" in patched, (
            "yamalanan ad taraması bozulmuş olabilir — depolama kökü listede yok"
        )
        moved = ast.parse("NAUTILUS_CATALOG_DIR = Path.home() / '.cache'\n")
        assert _mentioned(moved) & patched == {"NAUTILUS_CATALOG_DIR"}
        assert not _mentioned(ast.parse("x = 1\n")) & patched
