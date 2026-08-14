"""AUTO kokpiti continuous modda turlar arasında donmasın.

Continuous AUTO, kazanan bulunan HER turun sonunda `done=True` yazıp ~3 saniye
uyuyor, sonra yeni turda `done=False`'a dönüyor (`agent_backtest.py`: "always
set so polling shows result" + `sleep(3)` + tur başı reset). Yani `done=True`
bu modda "koşu bitti" DEĞİL, "bu turun sonucu hazır" demek.

Klasik `/agent` görünümü bu pencereyi `is_continuous` ile köprülüyordu:
`continuous_mode and not continuous_finished` olduğu sürece poll sürer. AUTO
Mission Control fragmenti ise ham `state["done"]`den türeyen `mv.done`e
bakıyordu. Kokpit 1 saniyede bir poll ettiği için 3 saniyelik pencereye bir
poll KESİNLİKLE denk gelir — ve tam o karede poll durur. Sonuç: koşu arkada
tur tur devam ederken operatör KALICI donmuş bir ekrana bakar; üstelik aynı
bayrak START/STOP düğmesini de sürdüğü için ekranda "▶ START" görünür, yani
arayüz koşmayan bir koşuyu koşuyor gibi değil, koşan bir koşuyu bitmiş gibi
gösterir (DeepR 2026-08-14 [YÜKSEK]).

Bu dosya sözleşmeyi üç durumda çiviliyor: tur arası pencere, kalıcı bitiş ve
continuous olmayan koşu.

Wiki References: [[auto_mission_control]], [[webapp_module_map]]
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
import web.routes.agent_backtest as ab


@pytest.fixture
def client():
    return TestClient(server.app)


def _state(**over) -> dict:
    """Kazananlı bir turun sonundaki gerçekçi asgari ilerleme durumu."""
    base = {
        # /agent/progress'in snapshot'ının .get'siz okuduğu alanlar — eksik biri
        # KeyError verir, yani bu küme rotanın zorunlu sözleşmesidir.
        "done": True,
        "error": None,
        "phases": [],
        "steps": [],
        "strategy_name": "probe",
        "winner_result": None,
        "winner_spec_name": None,
        # Continuous köprüsünün okuduğu bayraklar.
        "continuous_mode": True,
        "continuous_finished": False,
        "stop_requested": False,
        "max_total_tokens": 0,
        "max_hours": 0.0,
        "started_at": 0.0,
        "timeline": [],
        "brief": {},
    }
    base.update(over)
    return base


def _mission_html(client, monkeypatch, state: dict) -> str:
    run_id = "cockpit-probe"
    monkeypatch.setitem(ab._AGENT_PROGRESS, run_id, state)
    r = client.get(f"/agent/progress/{run_id}?view=mission")
    assert r.status_code == 200, r.text[:400]
    return r.text


class TestTheCockpitKeepsPollingBetweenContinuousRounds:
    def test_the_inter_round_window_still_polls(self, client, monkeypatch):
        """done=True + continuous_finished=False → koşu SÜRÜYOR."""
        html = _mission_html(client, monkeypatch, _state())
        assert "hx-trigger" in html, (
            "Kokpit turlar arası pencerede poll'u kesti — koşu arkada devam "
            "ederken ekran kalıcı donar (poll bir daha hiç başlamaz)."
        )
        assert "every 1s" in html

    def test_the_inter_round_window_still_offers_stop(self, client, monkeypatch):
        """Aynı bayrak düğmeyi de sürüyor: koşan koşuda START gösterilemez."""
        html = _mission_html(client, monkeypatch, _state())
        assert "/agent/stop/" in html, (
            "Tur arası pencerede STOP düğmesi kayboldu; operatör devam eden "
            "bir koşuyu durduramaz hâle gelir."
        )

    def test_a_permanently_finished_run_stops_polling(self, client, monkeypatch):
        """Çıpa: köprü poll'u SONSUZA dek açık bırakmamalı.

        `continuous_finished` worker döngüden temelli çıkınca yazılır; ondan
        sonra poll sürerse terminal fragman her saniye yeniden render edilir
        (titreme + boşuna CPU/ağ).
        """
        html = _mission_html(client, monkeypatch, _state(continuous_finished=True))
        assert "every 1s" not in html
        assert "mc-run-btn start" in html

    def test_a_finished_single_round_run_stops_polling(self, client, monkeypatch):
        """Continuous olmayan koşuda davranış hiç değişmemeli."""
        html = _mission_html(client, monkeypatch, _state(continuous_mode=False))
        assert "every 1s" not in html

    def test_the_cost_badge_prefers_the_providers_reported_cost(
        self, client, monkeypatch
    ):
        """Aynı snapshot kusurunun ikinci yüzü: maliyet rozeti.

        `_add_tokens` sağlayıcının bildirdiği maliyeti `provider_cost_usd`'de
        biriktiriyor ve tur sonu `token_snapshot` olayı onu doğru yazıyor. Ama
        alan snapshot'a kopyalanmadığı için `/progress`'in `cost_source`
        tercihi hep None okuyordu: rozet, sağlayıcı gerçek rakamı bildirdiğinde
        bile fiyat-tablosu TAHMİNİNİ gösteriyordu. Operatörün harcamayı koşu
        SIRASINDA gördüğü tek yüzey bu.
        """
        run_id = "cost-badge-probe"
        monkeypatch.setitem(
            ab._AGENT_PROGRESS,
            run_id,
            _state(done=False, provider_cost_usd=2.303754, tokens_out=54_269),
        )
        r = client.get(f"/agent/progress/{run_id}?view=mission")
        assert r.status_code == 200
        assert "2.30" in r.text or "2,30" in r.text, (
            "Rozet sağlayıcının bildirdiği maliyeti göstermiyor — "
            "provider_cost_usd snapshot'a kopyalanmamış olabilir."
        )

    def test_a_live_round_polls_in_both_modes(self, client, monkeypatch):
        """Tur ORTASINDA (done=False) her iki modda da poll sürer."""
        for continuous in (True, False):
            html = _mission_html(
                client,
                monkeypatch,
                _state(done=False, continuous_mode=continuous),
            )
            assert "every 1s" in html, f"continuous={continuous}"
