"""GET "/" (dashboard) — the app's actual first screen.

DeepR 2026-08-08 [YÜKSEK]: no test file in the suite ever called
`client.get("/")` — a regression anywhere in its chain (composer.load_catalog,
get_market_info, or a template global like first_studio_strategy_id()) would
surface only as a blank/500 page for a real user, unreproduced in CI.

2026-08-17: sayfa Loop kokpiti olmaktan çıkıp İNCE bir giriş sayfası oldu
(durum tutmuyor, hiçbir şey yoklamıyor). Testler de artık onu sınıyor.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from server import app

    return TestClient(app)


class TestDashboardRoute:
    def test_root_renders_200(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_root_shows_dashboard_page_title(self, client):
        r = client.get("/")
        assert "Dashboard" in r.text

    def test_root_no_longer_polls_anything(self, client):
        """İnce dashboard'ın sözleşmesi: tek istekte çözülür.

        Eski sayfa dört ayrı uca 2-4 saniyede bir soruyordu
        (`/fragments/loop_status|iterations|best|equity.json`). Hepsi legacy
        AppState'ten besleniyordu ve o kalktı; geriye poll eden bir iskelet
        kalırsa kullanıcı boş panellerin yenilenmesini izler.
        """
        r = client.get("/")

        assert "/fragments/" not in r.text
        for gone in (
            "loop-status",
            "loop-controls",
            'id="iters"',
            'id="best"',
            "equity-canvas",
        ):
            assert gone not in r.text, gone
        # NOT: base.html'in token rozeti hâlâ 60 sn'de bir yenileniyor — o
        # sayfanın değil YERLEŞİMİN parçası ve her yüzeyde var. İddia "bu sayfa
        # hiç poll etmesin" değil, "kaldırılan panelleri poll etmesin".

    def test_root_shows_the_catalog_size(self, client):
        """Sayfanın taşıdığı TEK durum bu — ve gerçekten hesaplanıyor mu."""
        from composer import load_catalog

        r = client.get("/")

        assert str(len(load_catalog() or [])) in r.text
