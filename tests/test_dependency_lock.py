"""Bağımlılık kilidi + PEP 517 build backend çıpaları.

DeepR 2026-08-11 [YÜKSEK]: depoda kilit dosyası yoktu, `[build-system]` yoktu ve
`nautilus_trader` dışındaki her sürüm tabanı serbestti; CI her koşumda
bağımlılıkları yeniden çözüyor ve bunu hiçbir yere kaydetmiyordu. Somut sonucu:
tek satır kod değişmeden, sırf pandas/pyarrow/fastapi yeni sürüm yayınladığı
için main kırmızıya dönebilir — ve dün yeşil olan bir commit bugün aynı ortamda
yeniden üretilemez. Bir backtest laboratuvarında bu ekstra pahalı: Sharpe /
drawdown sayılarının tekrarlanabilirliği (`regression_baseline.json`)
doğrudan pandas/numpy sürümüne bağlı.

Bu testler kilidi "var mı" diye değil, ÇALIŞIR durumda mı diye yoklar:
pyproject'te beyan edilen her doğrudan bağımlılığın kilitte tam sürümüyle
karşılığı olmalı. Yeni bir bağımlılık `uv lock` koşulmadan eklenirse burada
kırılır — CI'daki `uv sync --locked` adımına kalmadan, push'tan önce.

Ağ erişimi yok: yalnızca depodaki iki dosya okunur.

Wiki References: [[deepr_skill]]
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_LOCK = _ROOT / "uv.lock"


def _pyproject() -> dict:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _canonical(name: str) -> str:
    """PEP 503 normalizasyonu — `nautilus_trader` ile `nautilus-trader` aynı paket."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(reqs: list[str]) -> set[str]:
    return {_canonical(re.split(r"[<>=!~\[;\s]", r, maxsplit=1)[0]) for r in reqs}


@pytest.fixture(scope="module")
def locked_versions() -> dict[str, str]:
    """uv.lock içindeki {paket: sürüm} eşlemesi.

    Kilit TOML olduğu için `tomllib` ile okunur; `[[package]]` dizisinin her
    girdisi bir paketi tam sürümüyle sabitler.
    """
    assert _LOCK.exists(), (
        "uv.lock depoda yok — `uv lock` koşun ve dosyayı commit'leyin; "
        "aksi hâlde CI her koşumda bağımlılıkları yeniden çözer"
    )
    with _LOCK.open("rb") as fh:
        data = tomllib.load(fh)
    return {_canonical(p["name"]): p["version"] for p in data.get("package", [])}


class TestBuildSystem:
    def test_pep517_backend_is_declared(self):
        """`[build-system]` yoksa kurulum setuptools'un eski yoluna düşer.

        O yolda hangi setuptools sürümünün, hangi otomatik-keşif kurallarıyla
        çalışacağı makineye göre değişir — yani "kurulum" adımı bile
        tekrarlanabilir değildir.
        """
        build = _pyproject().get("build-system")
        assert build, "pyproject.toml'da [build-system] bloğu yok"
        assert build.get("build-backend")
        assert build.get("requires"), "backend'in kendi sürüm tabanı belirtilmemiş"

    def test_flat_layout_is_declared_as_an_application(self):
        """Depo bir uygulama: setuptools'a kopyalanacak modül OLMADIĞI söylenir.

        Bu satır olmadan düz yerleşimde otomatik keşif "Multiple top-level
        modules discovered" ile durur ve `uv sync` hiç çalışmaz.
        """
        assert (
            _pyproject().get("tool", {}).get("setuptools", {}).get("py-modules") == []
        )


class TestLockCoversEveryDeclaredDependency:
    def test_runtime_dependencies_are_locked(self, locked_versions):
        declared = _requirement_names(_pyproject()["project"]["dependencies"])
        missing = sorted(declared - locked_versions.keys())
        assert not missing, f"kilitte olmayan çalışma-zamanı bağımlılığı: {missing}"

    @pytest.mark.parametrize("extra", ["dev", "legacy"])
    def test_optional_dependencies_are_locked(self, locked_versions, extra):
        declared = _requirement_names(
            _pyproject()["project"]["optional-dependencies"][extra]
        )
        missing = sorted(declared - locked_versions.keys())
        assert not missing, f"kilitte olmayan '{extra}' bağımlılığı: {missing}"

    def test_every_locked_package_has_an_exact_version(self, locked_versions):
        """Kilit "sürüm aralığı" değil TAM sürüm tutmalı."""
        assert locked_versions, "uv.lock hiç paket içermiyor"
        for name, version in locked_versions.items():
            assert re.match(r"^\d", version), (
                f"{name} tam sürümle sabitlenmemiş: {version}"
            )

    def test_nautilus_lock_matches_the_single_supported_version(self, locked_versions):
        """server.py kurulu wheel sürümü sapınca hızlı hata veriyor; kilit ile
        `constants.NAUTILUS_REQUIRED` ayrışırsa CI ilk import'ta ölür."""
        from constants import NAUTILUS_REQUIRED

        assert locked_versions["nautilus-trader"] == NAUTILUS_REQUIRED


class TestCiInstallsFromTheLock:
    def test_workflow_syncs_with_locked(self):
        """CI kilidi OKUMALI; `uv sync --locked` hem yeniden çözmeyi engeller
        hem de kilit pyproject'ten saptıysa kırılır."""
        ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Yorum satırları elenir: workflow'un kendi gerekçe notunda da "uv sync"
        # geçiyor, ama çalıştırılan komut olmadığı için kurala tabi değil.
        sync_lines = [
            ln.strip()
            for ln in ci.splitlines()
            if "uv sync" in ln and not ln.strip().startswith("#")
        ]
        assert sync_lines, "CI'da `uv sync` adımı bulunamadı"
        for line in sync_lines:
            assert "--locked" in line or "--frozen" in line, (
                f"CI bağımlılıkları kilide bakmadan çözüyor: {line}"
            )
