"""custom_block prompt'undaki izin listeleri, DAYATILAN listelerden türemeli.

Ölçülen çelişki (2026-08-20, yerel 14B ile custom_block A/B'si): prompt üç satır
arayla iki zıt şey söylüyordu —

    ⭐ INDICATOR LIBRARY `ind` … USE IT, do NOT hand-roll the math:
      * `ind.calc_atr(highs, lows, closes, period)` → float | None
    …
    - Only these attributes may be accessed: .params, .role, .type, .get,
      .keys, .values, .items, .value, .upper, .lower, .middle, .initialized,
      .is_net_long, .is_net_short, .is_flat, math/statistics module functions.

`.calc_atr` ikinci listede YOK. Yani "bunu kullan" denen çağrı, "yalnız şunlara
erişebilirsin" listesinde geçmiyordu. codegate gerçekte 74 attribute'a izin
veriyor; prompt 15 sayıyordu.

Elle yazılmış bir izin listesi, dayattığı listeden ayrı yaşadığı sürece çürür —
ve çürüdüğünde model YANLIŞ yönde kısıtlanır: yasak olmayanı yasak sanır,
etrafından dolaşmak için daha karmaşık kod yazar.

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[nau_yerel_model_secimi_2026_08_20]]
"""

from __future__ import annotations

import agent
import codegate


# `meta["params"]` bir DEĞER sözlüğü DEĞİL, ŞEMADIR. İlk onarım sürümü bunu
# değer sanmıştı ve hiç ateşlemedi; testi de aynı yanlış varsayımla yazıldığı
# için yeşil kaldı — yani test, kodu değil benim zihin modelimi doğruladı.
_REAL_META = {
    "label": "X",
    "params": {
        "period": {"type": "int", "min": 5, "max": 30, "default": 14},
        "oversold": {"type": "float", "min": 20.0, "max": 50.0, "default": 40.0},
    },
}


def test_the_fixture_matches_the_shape_the_store_actually_holds():
    """Kurgu üretimden ıraksarsa bu test kırılsın — sessizce yanlış test etmeyelim."""
    for spec in _REAL_META["params"].values():
        assert isinstance(spec, dict)
        assert {"type", "min", "max", "default"} <= set(spec)
        assert spec["type"] in ("int", "float")


def _rendered() -> str:
    return agent._render_block_prompt(agent.CUSTOM_BLOCK_SYSTEM_PROMPT)


def test_every_enforced_attribute_is_offered_to_the_model():
    """Dayatılan her ad prompt'ta geçmeli — eksik ad, gereksiz kısıt demektir."""
    text = _rendered()
    missing = sorted(a for a in codegate._ALLOWED_ATTRS if f".{a}" not in text)
    assert not missing, f"prompt bu attribute'ları saklıyor: {missing}"


def test_every_enforced_builtin_is_offered_to_the_model():
    text = _rendered()
    missing = sorted(b for b in codegate._ALLOWED_BUILTINS if b not in text)
    assert not missing, f"prompt bu built-in'leri saklıyor: {missing}"


def test_the_indicator_library_is_not_contradicted():
    """Prompt'un "bunu kullan" dediği her `ind.*` çağrısı beyaz listede olmalı.

    Ölçülen vaka bu testin tam olarak yakaladığı şeydi: `ind.calc_atr` kullanılması
    ISTENIYOR ama erişilebilir attribute listesinde YOKTU.
    """
    import re

    text = _rendered()
    used = set(re.findall(r"`ind\.(\w+)\(", text))
    assert used, "prompt artık `ind.` örneği içermiyor — test kapsamını yitirdi"
    missing = sorted(n for n in used if n not in codegate._ALLOWED_ATTRS)
    assert not missing, (
        f"prompt kullanılmasını istiyor ama codegate yasaklıyor: {missing}"
    )


def test_the_lists_are_derived_not_handwritten():
    """Kaynağı tutar: elle yazılmış bir kopya geri gelirse test kırılır."""
    raw = agent.CUSTOM_BLOCK_SYSTEM_PROMPT
    assert "{allowed_attrs}" in raw and "{allowed_builtins}" in raw
    # Ham prompt'ta uzun bir elle-liste kalmamalı.
    assert ".is_net_long, .is_net_short" not in raw

    import inspect

    # Attribute listesi ortak yardımcıya taşındı; ikisi birlikte okunmalı.
    src = inspect.getsource(agent._render_block_prompt) + inspect.getsource(
        agent._allowed_attrs_line
    )
    assert "_ALLOWED_ATTRS" in src and "_ALLOWED_BUILTINS" in src


def test_the_rendered_prompt_leaves_no_placeholder():
    """Sızan bir yer tutucu modele anlamsız bir jeton gönderirdi."""
    text = _rendered()
    assert "{allowed_attrs}" not in text and "{allowed_builtins}" not in text


def test_max_lookback_is_still_demanded():
    """Yerelin en sık başarısızlığı buydu; şart prompt'tan düşmemeli."""
    text = _rendered()
    assert "max_lookback(params)" in text
    assert "is required" in text or "MUST also define" in text


# ---------------------------------------------------------------------------
# Eksik `max_lookback`: reddetmek yerine ONARMAK
# ---------------------------------------------------------------------------


def test_missing_max_lookback_is_synthesized_from_the_params():
    """ÖLÇÜLDÜ (40 çağrı, yerel 14B): 13 başarısızlığın 6'sı SADECE bu eksikti.

    Şart prompt'ta açıkça yazılı ve yeniden-deneme mesajı hatayı birebir
    taşıyor — yani eksik olan bilgi değil UYUM. Daha fazla prompt metni bunu
    kapatmıyor; kapatan şey, türetilebilir bir eksiği türetmek.

    M16'nın korktuğu şey fonksiyon yokken pencerenin sessizce 55 bara
    kırpılmasıydı; parametrelerden türetilmiş bir tavan ondan da, bloğu tamamen
    kaybetmekten de iyidir.
    """
    code = "def evaluate(c, i, p, s, b):\n    return None\n"
    out = agent._synthesize_max_lookback(code, _REAL_META)
    assert out is not None, "onarım gerçek meta şemasında ateşlemiyor"
    assert "def max_lookback(params)" in out
    assert 'params.get("period", 30)' in out  # int spec'in `max` degeri
    # Türetilen kod GERÇEKTEN çalışmalı, sadece metin olarak durmamalı.
    ns: dict = {}
    exec(compile(out, "<t>", "exec"), ns)  # noqa: S102 - kurgu kod, testin konusu
    assert ns["max_lookback"]({"period": 20}) == 50


def test_the_repair_declines_when_it_would_be_guessing():
    """Sayısal parametre yoksa uydurmak yerine ret doğru."""
    code = "def evaluate(c, i, p, s, b):\n    return None\n"
    only_float = {"params": {"k": {"type": "float", "min": 0.5, "max": 2.0}}}
    assert agent._synthesize_max_lookback(code, only_float) is None
    assert agent._synthesize_max_lookback(code, {"params": {"mode": "up"}}) is None


def test_the_repair_never_overwrites_an_existing_definition():
    code = "def max_lookback(params):\n    return 5\n"
    assert agent._synthesize_max_lookback(code, {"params": {"period": 3}}) is None
    assert agent._synthesize_max_lookback(code, _REAL_META) is None


def test_the_repair_still_runs_every_safety_gate():
    """Onarım codegate'i ATLAMAMALI — yoksa güvenlik kapısı delinir."""
    import inspect

    src = inspect.getsource(agent.propose_custom_block)
    seg = src[src.index('if "max_lookback" in last_error') :][:1400]
    assert "_validate_generated_code(_ml)" in seg
    assert "_test_execute_generated(" in seg
    assert "max_lookback_synthesized" in seg


def test_the_retry_message_reads_the_same_whitelist_as_the_gate():
    """İkinci bir elle-liste, ilkiyle aynı sebeple ıraksıyordu."""
    import inspect

    src = inspect.getsource(agent.propose_custom_block)
    assert "_allowed_attrs_line()" in src
    assert ".params/.role/.value/.upper" not in src, "elle yazılmış kopya geri gelmiş"
    line = agent._allowed_attrs_line()
    assert ".calc_atr" in line and ".setdefault" in line
