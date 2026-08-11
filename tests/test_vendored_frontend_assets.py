"""htmx + Chart.js yerelleştirmesi — CDN'e dönüş bir daha sessizce olmasın.

DeepR 2026-08-11 [ORTA]: `base.html` htmx'i unpkg'den, Chart.js'i jsdelivr'dan
`integrity`/`crossorigin` OLMADAN çekiyordu. İki yönlü sonuç:

* güvenlik — CDN tarafında bir ele geçirme ya da MITM, uygulamanın kendi
  origin'inde tam JS yürütmesi demekti; htmx tüm etkileşim katmanı olduğu için
  strateji/veri/LLM yüzeyinin tamamı düşerdi ve uygulama cloudflared tüneliyle
  internete bakıyor;
* kullanılabilirlik — internet/DNS kesildiğinde sayfa HTML'i normal görünüyor
  (CSS yerel) ama htmx yüklenmediği için hiçbir düğme, polling ya da sekme
  çalışmıyordu; `htmx.config…` satırı `htmx is not defined` ile patlayıp
  sonraki inline script'leri de bozuyordu.

Çözüm SRI eklemek değil, kütüphaneleri `web/static/` altına almaktı
(`lightweight-charts.js` zaten öyleydi). Bu dosya üç şeyi çiviliyor: dosyalar
var ve DOĞRU SÜRÜM, şablon onları yerelden çekiyor, ve şablonda hiç uzak
script/stylesheet kalmadı.

Wiki References
---------------
Bkz: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server

STATIC = Path(server.__file__).resolve().parent / "web" / "static"
BASE_HTML = (
    Path(server.__file__).resolve().parent / "web" / "templates" / "base.html"
).read_text(encoding="utf-8")

# Sürümler AYNEN korunmalı — yerelleştirme bir yükseltme fırsatı değildi;
# davranış değişikliği riski almadan tedarik zinciri kapatıldı.
VENDORED = [
    ("htmx.min.js", "1.9.12"),
    ("chartjs.umd.min.js", "4.4.4"),
]


class TestFilesArePresentAndPinned:
    @pytest.mark.parametrize(("name", "version"), VENDORED)
    def test_file_exists_and_is_not_a_stub(self, name, version):
        path = STATIC / name

        assert path.exists(), f"{name} vendored edilmemiş"
        # Bir hata sayfası ya da kırpılmış indirme 40 KB'ın altında kalırdı.
        assert path.stat().st_size > 40_000

    @pytest.mark.parametrize(("name", "version"), VENDORED)
    def test_version_string_is_the_one_the_cdn_served(self, name, version):
        src = (STATIC / name).read_text(encoding="utf-8", errors="replace")

        assert version in src, f"{name} beklenen sürümü ({version}) taşımıyor"

    def test_htmx_bundle_actually_defines_htmx(self):
        src = (STATIC / "htmx.min.js").read_text(encoding="utf-8", errors="replace")

        assert "e.htmx=e.htmx||t()" in src or "htmx" in src[:500]

    def test_chartjs_bundle_is_the_umd_build(self):
        src = (
            (STATIC / "chartjs.umd.min.js")
            .read_text(encoding="utf-8", errors="replace")
            .lower()
        )

        assert "chart.js v4.4.4" in src


class TestTemplateLoadsThemLocally:
    def test_base_html_points_at_static(self):
        assert "/static/htmx.min.js" in BASE_HTML
        assert "/static/chartjs.umd.min.js" in BASE_HTML

    def test_no_remote_script_tags_remain(self):
        """Şablonda hiçbir `<script src="http…">` kalmamalı."""
        remote = re.findall(r'<script[^>]+src="(https?:)?//[^"]+"', BASE_HTML)

        assert remote == [], f"hâlâ uzak script var: {remote}"

    def test_the_two_cdn_hosts_are_gone(self):
        assert "unpkg.com" not in BASE_HTML
        assert "cdn.jsdelivr.net" not in BASE_HTML

    def test_vendored_tags_carry_the_cache_busting_version(self):
        """Projenin `static_version()` mekanizmasına uyulmalı — yoksa bir gün
        sürüm yükseltilir ve tarayıcı eski paketi cache'ten servis eder."""
        for name, _ in VENDORED:
            assert f"/static/{name}?v={{{{ static_version }}}}" in BASE_HTML


class TestStaticVersionCoversTheBundles:
    def test_hash_changes_when_a_vendored_bundle_changes(self, tmp_path, monkeypatch):
        """`_static_version()` bu dosyaları da hash'lemeli."""
        fake = tmp_path / "web" / "static"
        fake.mkdir(parents=True)
        (fake / "htmx.min.js").write_text("v1", encoding="utf-8")
        monkeypatch.setattr(server, "BASE_DIR", tmp_path)

        before = server._static_version()
        (fake / "htmx.min.js").write_text("v2", encoding="utf-8")
        after = server._static_version()

        assert before != after


class TestTheyAreActuallyServed:
    @pytest.mark.parametrize(("name", "_v"), VENDORED)
    def test_static_mount_serves_the_bundle(self, monkeypatch, name, _v):
        monkeypatch.setattr(server, "_ACCESS_TOKEN", "")
        client = TestClient(server.app)

        r = client.get(f"/static/{name}")

        assert r.status_code == 200
        assert len(r.content) > 40_000
