"""Kök modüller kurulu bir bağımlılığın tepe adını GÖLGELEMESİN.

Depo düz yerleşimde: 43 modül/paket kökte duruyor ve hem `python server.py` hem
`pytest` depo kökünü `sys.path`'in BAŞINA koyar. Yani kökteki her ad,
site-packages'taki aynı adı yener. Adların çoğu da jenerik: `data`, `state`,
`agent`, `constants`, `sandbox`, `composer`, `indicators`, `strategies`, `web`,
`server`. Yarın içinde ``import state`` geçen bir bağımlılık kurulursa bizim
`state.py`'mizi alır — hata (eğer bir hata verirse) kurulu paketin İÇİNDEN
gelir, sebebi hiçbir yerde yazmaz ve aramak saatler yer.

DeepR 2026-08-11 [ORTA] tam bu yüzden kök modüllerin paketlenmesini istedi. O
taşıma ayrı ve riskli bir tur; bu dosya onun YERİNE geçmez, riski görünür kılar:
çakışma bir gün gerçekleşirse süit, kimse garip bir ImportError'ün peşine
düşmeden önce kırmızıya döner ve hangi dağıtımla çakışıldığını söyler.

Kapsam sınırı: yalnız KURULU dağıtımlara bakar. `pip install X` bir gün `state`
adında bir tepe modül getirirse bu test yakalar; PyPI'da öyle bir paketin
var olması tek başına bir şey söylemez, çünkü çakışma ancak kurulunca doğar.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

from importlib.metadata import distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _root_import_names() -> set[str]:
    """Depo kökünden import edilebilen tepe adlar.

    Lint'ten dışlanan dizinler (`legacy`, `debug`, `work`…) burada BİLEREK
    dışlanmıyor: bir dizin `__init__.py` taşıyorsa, biçimlendirilmiyor olması
    onu import yolundan çıkarmaz — gölgeleme riski aynen sürer.
    """
    names: set[str] = set()
    for p in ROOT.iterdir():
        if p.name.startswith("."):
            continue
        if p.suffix == ".py":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
    return names


def _installed_top_levels() -> dict[str, str]:
    """Kurulu her tepe modül adı → onu getiren dağıtım."""
    owner: dict[str, str] = {}
    for dist in distributions():
        dist_name = dist.metadata["Name"] or "?"
        declared = dist.read_text("top_level.txt")
        if declared:
            tops = {ln.strip() for ln in declared.splitlines() if ln.strip()}
        else:
            # top_level.txt yoksa (modern wheel'lerin çoğu) dosya listesinden
            # çıkar: kökteki `x.py` ve `x/__init__.py`.
            tops = set()
            for f in dist.files or ():
                parts = f.parts
                if len(parts) == 1 and parts[0].endswith(".py"):
                    tops.add(parts[0][:-3])
                elif len(parts) == 2 and parts[1] == "__init__.py":
                    tops.add(parts[0])
        for t in tops:
            if not t.startswith("_") and not t.endswith(".dist-info"):
                owner.setdefault(t, dist_name)
    return owner


@pytest.fixture(scope="module")
def installed() -> dict[str, str]:
    owner = _installed_top_levels()
    # Çıpalar: tarama bozulursa boş sözlükle "geçmesin".
    assert len(owner) >= 50, f"kurulu tepe ad taraması beklenmedik ({len(owner)})"
    for required in ("pandas", "fastapi", "numpy"):
        assert required in owner, (
            f"{required} kurulu tepe adlar arasında yok — tarama bozuk "
            "(dağıtım meta verisi okunamıyor olabilir)"
        )
    return owner


class TestTheFlatLayoutShadowsNothingInstalled:
    def test_no_root_module_shadows_an_installed_distribution(self, installed):
        clashes = {
            name: installed[name]
            for name in sorted(_root_import_names())
            if name in installed
        }
        assert not clashes, (
            "Kökteki şu modüller kurulu bir dağıtımın tepe adını gölgeliyor "
            f"(ad → dağıtım): {clashes}. Depo kökü `sys.path[0]` olduğu için "
            "kazanan BİZİM dosyamız: o dağıtım kendi modülünü import ettiğinde "
            "bizimkini alır. Ya kök modülü yeniden adlandırın ya da bağımlılığı "
            "değiştirin — düz yerleşimde üçüncü bir seçenek yok."
        )

    def test_the_scan_actually_sees_this_repos_modules(self):
        """Çıpa: kök taraması boşalırsa yukarıdaki test 'geçmesin'."""
        names = _root_import_names()
        assert len(names) >= 20, f"kök taraması çok az ad buldu ({len(names)})"
        for known in ("data", "server", "nau_data", "web"):
            assert known in names, f"{known} kök taramasında yok — tarama bozuk"

    def test_the_check_would_actually_catch_a_clash(self, installed):
        """Çıpa: gerçek bir çakışma verildiğinde kontrol kırmızıya döner."""
        pretend_root = {"pandas", "data"}
        assert {n: installed[n] for n in pretend_root if n in installed} == {
            "pandas": "pandas"
        }
