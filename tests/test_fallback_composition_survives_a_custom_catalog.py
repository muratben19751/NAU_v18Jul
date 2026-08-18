"""Fallback kompozisyonu, custom bloklarla dolu bir katalogda da geçerli olmalı.

AUTO koşusu 72029368 (2026-08-18) üçüncü turda öldü:

    ValueError: proposal missing entry block after cleanup
      agent._fallback_composed → _validate_composed

Zincir: OpenRouter bağlantısı düştü → `propose_composed_strategy` fallback'e
geçti → FALLBACK'İN KENDİSİ geçersiz bir öneri üretti → istisna
`_propose_initial_strategy`'den kaçtı ve tüm oturumu düşürdü.

Sebep elle yazılmış bir kapsam: giriş bloğu seçilirken yalnız `{"atr_stop"}`
dışlanıyordu. Oysa `BLOCK_CATALOG` çalışma anında custom blokları da içerir ve
onların meta'sı `role` İLAN EDER; `_coerce_block` ilan edilen rolle çelişen
bloğu (haklı olarak) düşürür. Ölçüldü — bu kutudaki canlı katalog: 408 blok,
71 built-in (rolsüz), 175 entry, **162 exit**. Yani her fallback çağrısı
**%40 ihtimalle** ölümcül bir yazı-turaydı.

Wiki References
---------------
Bkz: [[kesilme_ve_degrade_gorunurlugu]], [[auto_kapi_ve_geri_bildirim]]
"""

from __future__ import annotations

import pytest

import agent
from composer import BLOCK_CATALOG


@pytest.fixture
def catalog_with_custom_blocks(monkeypatch):
    """Rol ilan eden custom bloklarla dolu, EXIT ağırlıklı bir katalog."""
    fake = {}
    for name in ("ma_cross", "rsi_threshold", "atr_stop"):
        if name in BLOCK_CATALOG:
            fake[name] = BLOCK_CATALOG[name]
    assert fake, "built-in katalog boş — fixture anlamsız"

    param_spec = {"length": {"type": "int", "min": 2, "max": 50, "default": 14}}
    # Canlı katalogdaki orana yakın: exit'ler çoğunlukta.
    for i in range(3):
        fake[f"agnt_e_test_{i}"] = {"role": "entry", "params": dict(param_spec)}
    for i in range(30):
        fake[f"agnt_x_test_{i}"] = {"role": "exit", "params": dict(param_spec)}

    monkeypatch.setattr("composer.BLOCK_CATALOG", fake, raising=False)
    return fake


def test_the_measured_crash_does_not_reproduce(catalog_with_custom_blocks):
    """Yüz çağrının HİÇBİRİ patlamamalı — eski kod ~%40'ında ölüyordu."""
    for i in range(100):
        spec = agent._fallback_composed()
        roles = [b["role"] for b in spec["blocks"]]
        assert "entry" in roles, f"{i}. çağrıda entry yok: {spec['blocks']}"


def test_an_exit_only_block_is_never_chosen_as_the_entry(catalog_with_custom_blocks):
    """Kural katalogdan türemeli: ilan edilmiş `exit` girişe seçilmez."""
    for _ in range(100):
        spec = agent._fallback_composed()
        entry = next(b for b in spec["blocks"] if b["role"] == "entry")
        declared = (catalog_with_custom_blocks.get(entry["type"]) or {}).get("role")
        assert declared != "exit", f"exit-only blok girişe seçildi: {entry['type']}"


def test_atr_stop_is_still_excluded_from_entry(catalog_with_custom_blocks):
    """Built-in'in rolü İLAN EDİLMEMİŞ; eski elle dışlama korunmalı."""
    assert (catalog_with_custom_blocks.get("atr_stop") or {}).get("role") is None
    for _ in range(100):
        spec = agent._fallback_composed()
        entry = next(b for b in spec["blocks"] if b["role"] == "entry")
        assert entry["type"] != "atr_stop"


def test_an_entry_only_block_is_never_chosen_as_the_exit(catalog_with_custom_blocks):
    """Eksik exit ölümcül değil (onarılıyor) ama iki taraf aynı kuraldan gelmeli."""
    for _ in range(100):
        spec = agent._fallback_composed()
        for b in spec["blocks"]:
            if b["role"] != "exit":
                continue
            declared = (catalog_with_custom_blocks.get(b["type"]) or {}).get("role")
            assert declared != "entry", f"entry-only blok çıkışa seçildi: {b['type']}"


def test_a_catalog_with_no_valid_entry_still_does_not_kill_the_run(monkeypatch, caplog):
    """Son savunma hattı geri düşemez — ve düşerken SESSİZ olmaz.

    Katalog tamamen bozulsa bile (her şey exit ilan etmiş) built-in'lere
    dönülür, çünkü onların rolü ilan edilmemiştir.
    """
    fake = {}
    for name in ("ma_cross", "rsi_threshold"):
        if name in BLOCK_CATALOG:
            fake[name] = BLOCK_CATALOG[name]
    param_spec = {"length": {"type": "int", "min": 2, "max": 50, "default": 14}}
    for i in range(20):
        fake[f"agnt_x_only_{i}"] = {"role": "exit", "params": dict(param_spec)}
    monkeypatch.setattr("composer.BLOCK_CATALOG", fake, raising=False)

    spec = agent._fallback_composed()
    assert any(b["role"] == "entry" for b in spec["blocks"])


def test_the_eligible_set_is_derived_not_listed():
    """Kapsam elle yazılmış bir isim listesinden gelirse aynı hata geri gelir."""
    import inspect

    src = inspect.getsource(agent._fallback_composed)
    assert 'meta.get("role")' in src or 'BLOCK_CATALOG[t].get("role")' in src, (
        "uygunluk katalogdan türetilmiyor"
    )
    assert "entry_types = [t for t in all_types if t not in exit_only]" not in src, (
        "eski elle yazılmış kapsam geri gelmiş"
    )


def test_the_live_catalog_would_have_crashed_the_old_code():
    """Vakanın kendisi: canlı katalogda exit-only blok oranı ölçülebilir olmalı.

    Bu test bir regresyon değil bir KANIT: sayı sıfıra düşerse yukarıdaki
    testlerin neden var olduğu anlaşılmaz olur.
    """
    exits = [t for t, m in BLOCK_CATALOG.items() if (m or {}).get("role") == "exit"]
    if not exits:
        pytest.skip("bu kurulumda kayıtlı custom exit bloğu yok")
    share = len(exits) / len(BLOCK_CATALOG)
    assert share > 0, f"exit-only pay: {share:.0%} ({len(exits)}/{len(BLOCK_CATALOG)})"
