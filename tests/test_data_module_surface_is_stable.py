"""`data` modülünün testlerin YAMALADIĞI yüzeyi yerinde kalsın.

DeepR 2026-08-11 [ORTA] `data.py`'nin 3000+ satırını bölmeyi istiyor — dört veri
kaynağı, önbellek, kilitleme ve sunum satırı üretimi tek dosyada. Bölme doğru,
ama bu modülde onu SESSİZCE tehlikeli yapan bir şey var: test paketi 45 ayrı
`data.*` adını monkeypatch ediyor (kökler, TTL'ler, `_fetch_bybit_page`,
`_cache_lock`, önbellek sözlükleri…).

Naif bölme — kodu alt modüllere taşıyıp `data.py`'yi yeniden-dışa-verim
kabuğuna çevirmek — bu yamaların çoğunu **sessizce etkisiz kılar**:
``monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp)`` kabuktaki adı
değiştirir, ama değeri gerçekten okuyan alt modül kendi bağını okumaya devam
eder ([[import_aninda_yakalanan_referans]]). Testler YEŞİL kalır ve gerçek
`~/.cache/nautilus_web_app` kataloğuna yazmaya başlarlar. Bu, bu turdaki her
ajana verilen tek sert kuralın ihlali olurdu.

Bu dosya o riski görünür kılar. İki iddia:

  1. Yamalanan her ad hâlâ `data`'nın bir özniteliği (bölme birini kabukta
     unutursa `AttributeError` yerine anlamlı bir hata verir),
  2. bu adlardan **fonksiyon** olanların çağrı yerleri `data.py` içinde kalır —
     yani `data.py`'de tanımlı bir fonksiyon onları modül-global araması ile
     çözer ve yama etkili olur. Alt modüle taşınan bir çağrı yeri bunu bozar.

Yani: bölmeyi yasaklamıyor, GÜVENLİ bölmeyi tarif ediyor. Bir sonraki tur
`data.py`'yi paketlerken bu dosya kırmızıya dönerse taşınan şey yanlış şeydir.

Wiki References: [[webapp_module_map]], [[import_aninda_yakalanan_referans]]
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import data

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

_SETATTR_ALIAS = re.compile(r"setattr\(\s*data\s*,\s*[\"']([A-Za-z_0-9]+)[\"']")
_SETATTR_PATH = re.compile(r"setattr\(\s*[\"']data\.([A-Za-z_0-9]+)[\"']")


def _patched_names() -> set[str]:
    names: set[str] = set()
    files = sorted(TESTS.rglob("*.py"))
    assert len(files) > 50, f"tarama çok az test dosyası buldu ({len(files)})"
    for p in files:
        t = p.read_text(encoding="utf-8", errors="replace")
        names |= set(_SETATTR_ALIAS.findall(t)) | set(_SETATTR_PATH.findall(t))
    return names


@pytest.fixture(scope="module")
def patched() -> set[str]:
    names = _patched_names()
    # Çıpa: regex bozulursa boş küme ile "geçmesin".
    assert len(names) >= 30, f"yamalanan ad sayısı beklenmedik ({len(names)})"
    return names


class TestThePatchedSurfaceStillLivesOnData:
    def test_every_patched_name_is_an_attribute_of_data(self, patched):
        missing = sorted(n for n in patched if not hasattr(data, n))
        assert not missing, (
            "Testlerin monkeypatch ettiği şu adlar `data` üzerinde yok — bir "
            f"bölme onları kabukta bırakmayı unutmuş olabilir: {missing}"
        )

    def test_the_storage_roots_are_all_reachable(self):
        """Yamaların en tehlikelileri: bunlar kaçarsa gerçek kataloğa yazılır."""
        for root in (
            "CACHE_DIR",
            "BYBIT_CACHE_DIR",
            "EXTERNAL_CACHE_DIR",
            "NAUTILUS_CATALOG_DIR",
            "EQUITY_CATALOG_DIR",
            "INDEX_CACHE_DIR",
            "INDEX_ROOT",
            "TICKER_REGISTRY",
        ):
            assert hasattr(data, root), f"{root} `data` üzerinde yok"


class TestPatchedCallablesAreResolvedThroughDatasGlobals:
    """Yamanın ETKİLİ olması, çağrı yerinin `data`'da olmasına bağlı.

    Bir fonksiyon `data.py` içinde tanımlıysa gövdesindeki çıplak ad her
    çağrıda `data.__dict__`'ten çözülür — yama görülür. Aynı fonksiyon alt
    modüle taşınırsa kendi modül-global'ini okur ve yama GÖRÜLMEZ, üstelik
    sessizce.
    """

    @staticmethod
    def _defined_in_data() -> set[str]:
        tree = ast.parse((ROOT / "data.py").read_text(encoding="utf-8"))
        return {
            n.name
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_patched_functions_are_defined_in_data_py_itself(self, patched):
        defined = self._defined_in_data()
        strays = sorted(
            n for n in patched if callable(getattr(data, n, None)) and n not in defined
        )
        assert not strays, (
            "Bu fonksiyonlar testlerde `data.<ad>` olarak yamalanıyor ama artık "
            "`data.py` içinde TANIMLI değiller. Yamalar sessizce etkisiz olabilir: "
            f"{strays}. Ya çağrı yerlerini `data.py`'de tutun ya da testleri "
            "gerçek tanım yerine taşıyın."
        )

    def test_the_check_would_actually_catch_a_stray(self, patched):
        """Çıpa: yukarıdaki test, taranan küme boşalırsa 'geçmesin'."""
        defined = self._defined_in_data()
        overlap = {n for n in patched if callable(getattr(data, n, None))} & defined
        assert len(overlap) >= 5, (
            f"yamalanan fonksiyonların yalnız {len(overlap)} tanesi data.py'de "
            "tanımlı görünüyor — tarama bozulmuş olabilir"
        )
