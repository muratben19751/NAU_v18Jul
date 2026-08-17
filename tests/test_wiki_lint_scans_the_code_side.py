"""`wiki_tools lint` köprünün KOD yakasını da taramalı.

Köprü iki yönlü: sayfalar koda atıfta bulunur, modüller de docstring'lerindeki
`Wiki References` bloğuyla sayfalara. Lint uzun süre yalnız `wiki/` altındaki
.md'leri taradı; modül bağlarını hiç okumadı. Sonuç, denetimin sessizce yeşil
yanmasıydı — `broken_links (0)` cümlesinin öznesi sistem değil ARACIN kapsamı.

Ölçüldü (2026-08-18): lint altı kategoride de sıfır verirken kod yakasında
373 bağın 30'u çözülmüyordu. Hepsinin tek sebebi vardı — aynı ajan iki bilgi
tabanına birden yazıyor ve kişisel vault'ta gerçek olan bir sayfa adını proje
modülüne kopyalıyor (`[[deepr_skill]]` ×11 gibi). İki vault'un ad uzayı ayrı
olduğu için bağ orada çözülüyor, burada çözülmüyor.

Bu testler ARACIN davranışını sınar, deponun o anki temizliğini değil: kaç
kırık bağ olduğu zamanla değişir, taramanın var olup olmadığı değişmemeli.

Wiki References
---------------
See: [[kod_dokuman_koprusu_denetlenmiyor]], [[webapp_module_map]]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "nautilus_wiki" / "tools" / "wiki_tools.py"


@pytest.fixture(scope="module")
def wt():
    """`wiki_tools`'u yol üzerinden yükle — paket değil, tek dosyalık CLI."""
    if not TOOL.exists():
        pytest.skip("nautilus_wiki/tools/wiki_tools.py yok")
    spec = importlib.util.spec_from_file_location("wiki_tools_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestItReadsTheCodeSide:
    def test_it_finds_wiki_references_in_modules(self, wt):
        """Tarama bir şey BULMALI — sıfır sonuç, taramanın çalıştığını göstermez."""
        links = wt._code_bridge_links()

        assert len(links) > 100, (
            f"yalnız {len(links)} bağ bulundu — kapsam sessizce daralmış olabilir"
        )
        assert all(rel.endswith(".py") for rel, _ in links)

    def test_the_report_has_a_code_category(self, wt, capsys):
        wt.cmd_lint()
        out = capsys.readouterr().out

        assert "# code_broken_links" in out

    def test_a_broken_code_link_changes_the_exit_code(self, wt, monkeypatch):
        """Rapora yazıp yeşil yanmak, kapatmaya çalıştığı deseni yeniden üretirdi."""
        monkeypatch.setattr(
            wt, "_code_bridge_links", lambda: [("x.py", "yok_boyle_sayfa")]
        )

        assert wt.cmd_lint() == 2

    def test_a_clean_code_side_does_not_fail_the_lint(self, wt, monkeypatch):
        """Ayırt edicilik: kırık YOKKEN kırmızı yanan bir denetim de işe yaramaz."""
        monkeypatch.setattr(wt, "_code_bridge_links", lambda: [])

        assert wt.cmd_lint() == 0


class TestItDoesNotManufactureFindings:
    def test_syntax_examples_in_backticks_are_not_links(
        self, wt, tmp_path, monkeypatch
    ):
        """`wiki_helper.py` wikilink SÖZDİZİMİNİ anlatıyor — örneği bağ saymak,
        gürültülü bir denetim üretir ve gürültülü denetim terk edilir."""
        mod = tmp_path / "ornek_modul.py"
        mod.write_text(
            '"""Bir şey.\n\nWiki References\n---------------\n'
            "Bağ şöyle yazılır: `[[bu_bir_ornek]]`, gerçek bağ: [[webapp_module_map]]\n"
            '"""\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(wt, "CODE_ROOT", tmp_path)

        targets = [t for _, t in wt._code_bridge_links()]

        assert "bu_bir_ornek" not in targets
        assert "webapp_module_map" in targets

    def test_modules_without_the_block_are_ignored(self, wt, tmp_path, monkeypatch):
        """Niyet beyanı olmayan bir `[[...]]` bağ değildir."""
        mod = tmp_path / "baska.py"
        mod.write_text(
            '"""Bir liste: [[a, b], [c, d]] — köprü bloğu yok."""\n', encoding="utf-8"
        )
        monkeypatch.setattr(wt, "CODE_ROOT", tmp_path)

        assert wt._code_bridge_links() == []

    def test_an_unparseable_module_is_skipped_not_fatal(
        self, wt, tmp_path, monkeypatch
    ):
        """Bozuk bir dosya tüm denetimi düşürmemeli."""
        (tmp_path / "bozuk.py").write_text("def (((\n", encoding="utf-8")
        (tmp_path / "iyi.py").write_text(
            '"""X.\n\nWiki References\n---------------\n[[webapp_module_map]]\n"""\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(wt, "CODE_ROOT", tmp_path)

        assert [t for _, t in wt._code_bridge_links()] == ["webapp_module_map"]

    def test_the_venv_is_not_scanned(self, wt, tmp_path, monkeypatch):
        """Üçüncü parti kod bizim köprümüz değil."""
        venv = tmp_path / ".venv" / "pkg"
        venv.mkdir(parents=True)
        (venv / "m.py").write_text(
            '"""X.\n\nWiki References\n---------------\n[[baskasinin_sayfasi]]\n"""\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(wt, "CODE_ROOT", tmp_path)

        assert wt._code_bridge_links() == []
