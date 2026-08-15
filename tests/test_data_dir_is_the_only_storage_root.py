"""Kalıcı depolama kökü TEK olsun: `NAU_DATA_DIR` her şeyi taşısın.

`app_constants.DATA_DIR` taşınabilir kök olarak kuruldu, ama AUTO'nun asıl
kalıcı çıktıları (oturum logu, backtest/robustness logu, strateji kataloğu,
agent index, rapor yerleşimi) uzun süre kendi köklerini ELLE kurmayı sürdürdü:

    _CACHE_DIR = Path.home() / ".cache" / "nautilus_web_app"

DeepR 2026-08-14 [YÜKSEK] üç sonucunu saydı. (1) `nau_config` kataloğu
`NAU_DATA_DIR`'i "katalog önbelleği, token defteri, **oturum logları**" taşır
diye belgeliyordu — oturum logları için bu gerçekleşmiyordu, yani belge
kodla çelişiyordu. (2) `tests/browser/conftest.py`'nin "bu süit gerçek
``~/.cache`` kataloğuna ASLA dokunmamalı — pazarlık konusu değil" sözleşmesi
AYRI SÜREÇTE koşan sunucuya yalnız env ile kurulabiliyor; elle kurulmuş kökler
o izolasyonun tamamen dışındaydı ve ihlalin bugün görünmemesi mekanizmadan
değil, tek E2E testinin backtest tetiklemiyor olmasından geliyordu. (3) İkinci
bir instance (paralel deney, ikinci operatör) durum karışması olmadan
koşturulamıyordu — iki instance aynı katalog ve log dosyalarını paylaşırdı.

Bu dosya kökün tekliğini iki yönden bağlar:

  1. Bilinen her kalıcı yol `DATA_DIR` altında kalır ve `NAU_DATA_DIR`
     verildiğinde ORAYA taşınır (alt süreçte ölçülür — env import anında
     okunduğu için aynı süreçte sınanamaz),
  2. ürün kodunda yeni bir elle kurulmuş `~/.cache/nautilus_web_app` kökü
     belirirse AST/metin taraması kırmızıya döner.

Wiki References: [[webapp_module_map]], [[import_aninda_yakalanan_referans]]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Kalıcı kökten türemesi gereken adlar: (modül, öznitelik).
_PERSISTENT_PATHS = [
    ("web.shared", "BACKTEST_LOG"),
    ("web.shared", "ROBUSTNESS_LOG"),
    ("web.shared", "SESSION_LOG_DIR"),
    ("composer", "CATALOG_FILE"),
    ("web.routes.reports", "REPORTS_LAYOUT"),
    ("web.routes.agent_backtest", "_AGENT_INDEX_DB"),
    ("custom_block_store", "STORE_DIR"),
    ("custom_block_store", "REGISTRY_FILE"),
    ("token_ledger", "LEDGER_PATH"),
    # X izleyicisi ayrı bir süreçtir ama kalıcı çıktıları aynı köke bağlı olmalı:
    # oturum çerezi (`x_storage_state.json`) repo ağacına düşerse hesaba erişim
    # versiyonlanabilir hâle gelir.
    ("x_watch", "LEDGER_PATH"),
    ("x_watch", "STATE_PATH"),
    ("x_watch", "STORAGE_STATE_PATH"),
]

_PROBE = """
import json, sys
out = {}
for mod_name, attr in %(pairs)r:
    mod = __import__(mod_name, fromlist=["_"])
    out[f"{mod_name}.{attr}"] = str(getattr(mod, attr))
sys.stdout.write("@@JSON@@" + json.dumps(out))
"""


def _resolve_in_subprocess(data_dir: str | None) -> dict[str, str]:
    """Yolları TEMİZ bir yorumlayıcıda çöz.

    `NAU_DATA_DIR` import anında okunuyor ([[import_aninda_yakalanan_referans]]),
    dolayısıyla bu testin kendi süreci onu değiştiremez: modüller çoktan
    yüklenmiştir. Ölçüm ancak ayrı bir süreçte anlamlı.
    """
    env = dict(os.environ)
    env.pop("NAU_DATA_DIR", None)
    if data_dir is not None:
        env["NAU_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE % {"pairs": _PERSISTENT_PATHS}],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    marker = proc.stdout.rfind("@@JSON@@")
    assert marker >= 0, (
        "alt süreç yolları basamadı — import hatası olabilir.\n"
        f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
    )
    return json.loads(proc.stdout[marker + len("@@JSON@@") :])


@pytest.fixture(scope="module")
def default_paths() -> dict[str, str]:
    paths = _resolve_in_subprocess(None)
    assert len(paths) == len(_PERSISTENT_PATHS), "sonda eksik yol döndürdü"
    return paths


class TestEveryPersistentPathFollowsTheSingleRoot:
    def test_the_defaults_all_live_under_the_default_root(self, default_paths):
        """Varsayılan DEĞİŞMEMELİ: mevcut kurulumların verisi yerinde kalsın."""
        expected_root = str(Path.home() / ".cache" / "nautilus_web_app")
        strays = {
            name: p
            for name, p in default_paths.items()
            if not p.startswith(expected_root)
        }
        assert not strays, (
            f"Bu kalıcı yollar varsayılan kökün ({expected_root}) altında değil: "
            f"{strays}. Varsayılanı değiştirmek mevcut kurulumların kataloğunu, "
            "token defterini ve oturum loglarını görünmez kılar."
        )

    def test_nau_data_dir_moves_all_of_them(self, tmp_path, default_paths):
        """Asıl sözleşme: env verilince HEPSİ taşınmalı — biri bile kalmamalı."""
        moved_root = tmp_path / "izole_kok"
        moved = _resolve_in_subprocess(str(moved_root))
        leaks = {
            name: p for name, p in moved.items() if not p.startswith(str(moved_root))
        }
        assert not leaks, (
            f"NAU_DATA_DIR={moved_root} verildiği hâlde şu yollar taşınmadı: "
            f"{leaks}. Kökünü elle kuran her sabit, E2E izolasyonunu (ayrı "
            "süreçte YALNIZ env ile kurulabiliyor) ve ikinci bir instance "
            "koşturma imkânını sessizce bozar — `app_constants.DATA_DIR`'den türet."
        )
        # Çıpa: taşıma gerçekten ölçüldü mü, yoksa sonda mı boş döndü?
        assert len(moved) == len(default_paths) >= 8


class TestNoModuleRebuildsTheStorageRootByHand:
    """Altıncı bir sabit sessizce eklenmesin."""

    _HAND_ROLLED = re.compile(
        r"""Path\.home\(\)\s*/\s*["']\.cache["']\s*/\s*["']nautilus_web_app["']"""
    )
    # Kökün TANIMLANDIĞI yer — tek meşru geçiş.
    _ALLOWED = {"app_constants.py"}
    _SKIP_DIRS = {
        ".git",
        "__pycache__",
        ".venv",
        "legacy",
        "design2",
        "debug",
        "work",
        "unsloth_compiled_cache",
        "node_modules",
        "nautilus_wiki",
        ".claude",
        ".tmp",
    }

    def _sources(self) -> list[Path]:
        files = [
            p
            for p in ROOT.rglob("*.py")
            if not (set(p.relative_to(ROOT).parts) & self._SKIP_DIRS)
        ]
        assert len(files) > 100, f"kaynak taraması çok az dosya buldu ({len(files)})"
        return files

    def test_only_app_constants_builds_the_root(self):
        offenders = {}
        for p in self._sources():
            rel = p.relative_to(ROOT).as_posix()
            if rel in self._ALLOWED or rel.startswith("tests/"):
                continue
            hits = self._HAND_ROLLED.findall(p.read_text(encoding="utf-8"))
            if hits:
                offenders[rel] = len(hits)
        assert not offenders, (
            "Şu modüller kalıcı depolama kökünü ELLE kuruyor: "
            f"{offenders}. `NAU_DATA_DIR` bu yolları taşıyamaz; "
            "`from app_constants import DATA_DIR` kullanın."
        )

    def test_the_scan_would_actually_catch_a_hand_rolled_root(self):
        """Çıpa: regex hâlâ ısırıyor mu?"""
        assert self._HAND_ROLLED.search(
            'X = Path.home() / ".cache" / "nautilus_web_app" / "y.json"'
        )
        assert not self._HAND_ROLLED.search('X = DATA_DIR / "y.json"')
        # Kökü tanımlayan dosya gerçekten deseni içermeli (allowlist ölü olmasın).
        src = (ROOT / "app_constants.py").read_text(encoding="utf-8")
        assert self._HAND_ROLLED.search(src), (
            "app_constants.py artık kökü bu desenle kurmuyor — allowlist bayat, "
            "test yanlış şeyi koruyor olabilir."
        )
