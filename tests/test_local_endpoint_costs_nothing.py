"""Sıfır maliyet ile BİLİNMEYEN maliyet aynı şey değildir.

Token tavanının iki sürümü var: gevşek (kaçak-döngü emniyeti, 2.000.000) ve
sıkı (para vekili, 250.000). Gevşek sürüm ancak para tavanı işini
yapabildiğinde geçerli — fiyatı bilinmeyen PARALI bir uçta gevşetmek faturayı
korumasız bırakırdı. Kontrol şuydu:

    if not spent:   # None ya da 0.0 → parayı göremiyoruz
        cap = BLIND_MAX_TOKENS

`0.0` ile `None` aynı kefeye konuyordu. Ama YEREL bir uçta maliyet bilinmez
değil, **sıfır olduğu bilinir.**

ÖLÇÜLEN VAKA (koşu 32877fc0, 2026-08-20): `custom_block` pini kaldırılıp dört
yol da yerele alındıktan sonra koşu **252.419 token'da, 1,3 turda** kesildi —
hiç para harcamadan. Üstelik yerele geçmek token TÜKETİMİNİ artırmıştı (artık
tüm çağrılar aynı sayaca yazılıyor), yani sıkı tavan var olmayan bir faturayı
korurken 5 kat hızlanmanın getirisini yiyordu.

Aynı hata bu dosyada ikinci kez: 2026-08-15'te de "bedava model BEDAVA OLDUĞU
İÇİN değil SAYILDIĞI İÇİN koşuyu kısalttı" diye kayda geçmiş ve iki-tavan
tasarımı o zaman gelmişti. Körlük şartı o turda doğru kuruldu ama "yerel uç"
vakası kapsanmadı.

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[model_secici_ve_gorunurluk]]
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def ab(monkeypatch):
    """Modülü verilen uç adresiyle yeniden yükleyen yardımcı."""

    def _load(base_url: str | None):
        if base_url is None:
            os.environ.pop("OPENROUTER_BASE_URL", None)
        else:
            os.environ["OPENROUTER_BASE_URL"] = base_url
        import web.routes.agent_backtest as mod

        return importlib.reload(mod)

    yield _load
    os.environ.pop("OPENROUTER_BASE_URL", None)
    import web.routes.agent_backtest as mod

    importlib.reload(mod)


def test_a_localhost_endpoint_is_recognised(ab):
    for url in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:8080/v1",
        "http://[::1]:1234/v1",
    ):
        assert ab(url)._endpoint_is_local(), url


def test_a_remote_endpoint_is_not_local(ab):
    for url in ("https://openrouter.ai/api/v1", "https://api.example.com/v1", ""):
        assert not ab(url)._endpoint_is_local(), url
    assert not ab(None)._endpoint_is_local()


def test_an_all_local_run_has_a_KNOWN_zero_cost(ab):
    """Ölçülen vaka: harcama 0,0 ama görünürlük TAM — sıkı tavana düşülmemeli."""
    mod = ab("http://127.0.0.1:11434/v1")
    state = {"by_model": {"qwen2.5-coder:14b": {"calls": 12}}}
    assert mod._cost_is_visible(state, 0.0) is True
    assert mod._cost_is_visible(state, None) is True


def test_a_single_paid_call_restores_the_strict_ceiling(ab):
    """Karışım gevşetmeyi HAK ETMEZ — bir tek paralı çağrı yeter."""
    mod = ab("http://127.0.0.1:11434/v1")
    mixed = {"by_model": {"qwen2.5-coder:14b": {}, "claude-fable-5": {}}}
    assert mod._cost_is_visible(mixed, 0.0) is False


def test_an_unpriced_REMOTE_endpoint_stays_blind(ab):
    """Asıl korunan durum: fiyatı bilinmeyen ama PARALI uç."""
    mod = ab("https://openrouter.ai/api/v1")
    state = {"by_model": {"some/paid-model": {"calls": 3}}}
    assert mod._cost_is_visible(state, 0.0) is False


def test_visible_spend_alone_is_enough(ab):
    """Fiyatlanmış harcama varsa uç neresi olursa olsun görünürlük vardır."""
    mod = ab("https://openrouter.ai/api/v1")
    assert mod._cost_is_visible({"by_model": {"claude-opus-5": {}}}, 1.25) is True


def test_an_empty_run_is_not_treated_as_visible(ab):
    """Hiç çağrı yokken "görüyorum" demek, ilk çağrıdan önce gevşetmek olurdu."""
    mod = ab("http://127.0.0.1:11434/v1")
    assert mod._cost_is_visible({}, 0.0) is False
    assert mod._cost_is_visible({"by_model": {}}, 0.0) is False


def test_the_gate_reads_the_helper_not_the_old_falsy_check():
    """Kaynağı tutar: `if not spent` geri gelirse 0,0 yine körlük sayılır."""
    import inspect

    import web.routes.agent_backtest as mod

    src = inspect.getsource(mod._budget_breach)
    assert "_cost_is_visible(state, spent)" in src
    assert "if not spent:" not in src


def test_the_ledger_records_models_WITHOUT_the_or_prefix():
    """Kurgu üretimden ıraksamasın — ilk sürüm tam bu yüzden hiç ateşlemedi.

    `or:` seçim katmanında kalıyor; defter ham id yazıyor (ölçüldü koşu
    400c7922: `model: 'qwen2.5-coder:14b'`, `pricing_model` aynısı,
    `cost_usd: 0.0`). Önekle yapılan bir "yerel mi" testi bu yüzden çalışmaz —
    ayrım fiyat TABLOSUNDAN türetilmeli.
    """
    import token_ledger

    # Paralı aile tabloda: fiyatlanabiliyor.
    assert token_ledger.cost_usd({"input": 1, "output": 1}, "claude-fable-5") is not None
    # Yerel model tabloda yok: fiyatlanamıyor.
    assert token_ledger.cost_usd({"input": 1, "output": 1}, "qwen2.5-coder:14b") is None


def test_locality_is_not_decided_by_a_name_prefix():
    """Kaynağı tutar: önek tahmini geri gelirse test kırılır."""
    import inspect

    import web.routes.agent_backtest as mod

    src = inspect.getsource(mod._cost_is_visible)
    # Yorumları ele: `or:` öneki KODDA olmamalı ama neden yanlış olduğunu
    # anlatan yorumda geçmesi gerekiyor. Ham metin araması bu ikisini
    # ayıramaz — ve testin ilk hâli tam da kendi açıklamama takıldı.
    code_only = chr(10).join(
        line.split("#", 1)[0] for line in src.splitlines()
    )
    assert 'startswith("or:")' not in code_only, "önek tahmini geri gelmiş"
    assert "token_ledger.cost_usd" in code_only
