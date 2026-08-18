"""Kabul ölçütü üreticiye ULAŞMALI — hedefi söylemeden hedef tutturulamaz.

ÖLÇÜLDÜ (beş AUTO koşusu, 173 aday, 2026-08-18): kapının ölçüsü al-tut'a göre
Calmar oranı; adayların medyanı ×0,21 ve yalnız %11'i ×1,00'ı geçti. Rahatsız
edici kırılım: LLM erişilemediğinde devreye giren RASTGELE fallback
kompozisyonları medyan ×0,40 / p90 ×1,01 ile üç Claude modelinin hepsini
(×0,14-0,26) geçti — ve onlar `research-only` damgası yüzünden tanım gereği
yayımlanamıyor.

En olası açıklama prompt'ta duruyordu: sistem mesajı kabul ölçütünü hiç
söylemiyordu. Bu testler ölçütün prompt'a girdiğini, kolun kapatılabildiğini ve
hangi kolun koştuğunun KAYDA geçtiğini tutar — son maddesi olmadan iki koşuyu
karşılaştıran kişi neyin değiştiğini bilemez.

Wiki References
---------------
Bkz: [[auto_kapi_ve_geri_bildirim]], [[nau_auto_kosulari_2026_08_18]],
[[auto_arama_ekonomisi]]
"""

from __future__ import annotations

import importlib
import os

import pytest

import agent


def _render(mod) -> str:
    return (
        mod.COMPOSED_SYSTEM_PROMPT.replace("{market_context}", "X")
        .replace("{catalog}", "Y")
        .replace("{objective}", mod._OBJECTIVE_BLOCK if mod.OBJECTIVE_IN_PROMPT else "")
    )


@pytest.fixture
def with_flag(monkeypatch):
    """Bayrağı env'den kurup modülü yeniden yükleyen yardımcı."""

    def _set(value: str | None):
        if value is None:
            os.environ.pop("AGENT_OBJECTIVE_IN_PROMPT", None)
        else:
            os.environ["AGENT_OBJECTIVE_IN_PROMPT"] = value
        return importlib.reload(agent)

    yield _set
    os.environ.pop("AGENT_OBJECTIVE_IN_PROMPT", None)
    importlib.reload(agent)


def test_the_prompt_states_the_actual_bar():
    """Kapının ölçüsü ADIYLA yazılmalı — "iyi bir strateji" hedef değildir."""
    text = _render(agent)
    assert "ACCEPTANCE CRITERIA" in text
    assert "Calmar" in text
    assert "buy-and-hold" in text
    # Ölçülen gerçeklik de yazılı: model neye nişan aldığını bilsin.
    assert "median proposal" in text


def test_the_prompt_states_the_trade_floor():
    """20 işlem eşiği reddin en sık ikinci sebebi; söylenmezse tahmin edilemez."""
    text = _render(agent)
    assert "20 closed trades" in text


def test_the_prompt_warns_that_out_of_sample_checks_follow():
    """Tek enstrümana/döneme özel çözüm zincirin devamında düşüyor."""
    text = _render(agent)
    for token in ("peer instruments", "walk-forward", "Monte Carlo", "sealed"):
        assert token in text, token


def test_the_score_formula_is_deliberately_withheld():
    """`T/(T+20)` çarpanı oynanabilir ve ölçüm frekans-Calmar ilişkisi BULMADI."""
    text = _render(agent)
    assert "T/(T+20)" not in text
    assert "0.7" not in text or "0.7×Calmar" not in text


def test_simplicity_is_offered_as_a_hypothesis_not_a_law():
    """Rastgele tabanın üstünlüğü sadeliği İMA ediyor; örneklem dengesiz."""
    text = _render(agent)
    assert "hypotheses to" in text
    assert "unless" in text, "koşulsuz bir 'az blok kullan' emri aşırı kısıtlar"


def test_the_control_arm_can_be_restored(with_flag):
    """Kolu kapatmak mümkün olmalı, yoksa A/B ölçülemez."""
    off = with_flag("0")
    assert off.OBJECTIVE_IN_PROMPT is False
    text = _render(off)
    assert "ACCEPTANCE CRITERIA" not in text
    assert "{objective}" not in text, "yer tutucu prompt'ta kalmış"


def test_the_flag_defaults_to_on(with_flag):
    on = with_flag(None)
    assert on.OBJECTIVE_IN_PROMPT is True


def test_no_placeholder_survives_rendering():
    """Sızan bir `{objective}` modele anlamsız bir jeton gönderirdi."""
    for value in (None, "0"):
        if value is None:
            os.environ.pop("AGENT_OBJECTIVE_IN_PROMPT", None)
        else:
            os.environ["AGENT_OBJECTIVE_IN_PROMPT"] = value
        mod = importlib.reload(agent)
        assert "{objective}" not in _render(mod)
    os.environ.pop("AGENT_OBJECTIVE_IN_PROMPT", None)
    importlib.reload(agent)


def test_the_run_record_says_which_arm_ran(with_flag):
    """Kayıt kolu taşımazsa iki koşu karşılaştırılamaz."""
    import web.routes.agent_backtest as ab

    with_flag(None)
    assert ab._effective_run_config()["objective_in_prompt"] is True
    with_flag("0")
    assert ab._effective_run_config()["objective_in_prompt"] is False


def test_the_prompt_is_assembled_through_one_place():
    """Üç yer tutucu tek zincirde çözülüyor — ikinci bir kopya ıraksardı."""
    import inspect

    src = inspect.getsource(agent.propose_composed_strategy)
    assert 'replace("{objective}"' in src
    assert "OBJECTIVE_IN_PROMPT" in src
