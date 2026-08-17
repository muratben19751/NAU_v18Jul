"""`web.shared.load_result_snapshot` — `run_id` bir yol parçası, doğrulanmalı.

`GET /backtest/result/{run_id}` URL'den gelen dizgeyi doğrudan dosya adına
çeviriyordu. ÖLÇÜLDÜ (2026-08-17, düzeltmeden önce): `..%5C..%5Cevil` isteği
HTTP 200 döndü ve okuma `C:\\Users\\MYDESK\\evil.json`'a çözüldü — kutudaki
herhangi bir JSON, sonuç ekranı üzerinden okunabiliyordu.

Kardeş yüzey `custom_block_store.list_agent_blocks` aynı kimliği zaten
`re.fullmatch(r"[a-z0-9]{8}")` ile doğruluyordu; atlanan bu yüzeydi. Ders,
yol birleştirmenin kendisinde değil: **URL'den gelen her parça, dosya adına
dönüşmeden önce ŞEKLİYLE sınanmalı** — sonuçtaki yolu kontrol etmek de
çalışırdı ama sembolik bağlar ve sürücü harfleriyle daha kırılgan.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import json

import pytest

from web import shared


@pytest.fixture()
def results_dir(tmp_path, monkeypatch):
    d = tmp_path / "bt_results"
    d.mkdir()
    monkeypatch.setattr(shared, "_RESULTS_DIR", d)
    return d


ESCAPES = [
    "../../evil",
    "..\\..\\evil",
    "../" * 8 + "evil",
    "/etc/passwd",
    "C:\\Users\\MYDESK\\evil",
    "abcd1234/../../evil",
    "....//....//evil",
]


@pytest.mark.parametrize("run_id", ESCAPES)
def test_no_escape_reaches_the_filesystem(run_id, results_dir, tmp_path):
    """Hedef dosya GERÇEKTEN var olsa bile okunamamalı."""
    outside = tmp_path / "evil.json"
    outside.write_text(json.dumps({"secret": "okundu"}), encoding="utf-8")

    assert shared.load_result_snapshot(run_id) is None
    assert shared._snapshot_path(run_id) is None


@pytest.mark.parametrize("run_id", ESCAPES)
def test_writing_is_guarded_by_the_same_rule(run_id, results_dir):
    """Yazma yolu bugün hep bizim ürettiğimiz id'yi alıyor; kural yine de TEK
    yerde duruyor — iki yüzeyin ıraksaması bu depoda tekrarlayan bir kusur."""
    shared.save_result_snapshot(run_id, {"anything": 1})

    assert list(results_dir.rglob("*.json")) == []


@pytest.mark.parametrize(
    "run_id",
    ["", "ABCD1234", "abcd123", "abcd12345", "abcd-123", "zzzzzzzz", "abcd 123"],
)
def test_only_eight_lowercase_hex_digits_are_a_run_id(run_id, results_dir):
    """`uuid.uuid4().hex[:8]` her iki çağrı yerinde de tam olarak bu şekli
    üretiyor: büyük harf, kısa, uzun ve hex olmayan hepsi reddedilir."""
    assert shared._snapshot_path(run_id) is None


def test_a_real_run_id_still_round_trips(results_dir):
    """Kapı, asıl işi kapatmıyor."""
    shared.save_result_snapshot("a1b2c3d4", {"metrics": {"n_trades": 7}})

    assert (results_dir / "a1b2c3d4.json").exists()
    assert shared.load_result_snapshot("a1b2c3d4") == {"metrics": {"n_trades": 7}}


def test_the_route_answers_a_probe_the_same_as_a_missing_run():
    """Reddi 400 ile ayırmak, prob atana hangi yolların var olduğunu söylerdi.
    İki durum da aynı "artık saklanmıyor" panelini vermeli."""
    from fastapi.testclient import TestClient

    import server

    client = TestClient(server.app)
    probe = client.get("/backtest/result/..%5C..%5Cevil")
    missing = client.get("/backtest/result/deadbeef")

    assert probe.status_code == missing.status_code
    assert "evil" not in probe.text
