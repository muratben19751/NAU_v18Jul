"""İpucu-sadık keşif: hint belirgin indikatör içerince agent o sette kalmalı.

Kullanıcı 'RSI+ADX+ATR' verdiğinde idea-prompt'u 'farklı indikatör ailesi seç'
yerine 'bu sette kal, kombinasyon/parametre varyasyonlarını tara' yönergesi
kullanmalı — yoksa agent başka indikatörlere kayıyordu.
"""

from __future__ import annotations

from agent import _exploration_directive, _hint_indicators


class TestHintIndicators:
    def test_detects_named_indicators(self):
        inds = _hint_indicators("RSI VE ADX VE ATR ye gore")
        assert "RSI" in inds and "ADX/DMI" in inds and "ATR" in inds

    def test_english_and_turkish(self):
        assert "Stochastic" in _hint_indicators("use stochastic oscillator")
        assert "Hacim" in _hint_indicators("hacim patlaması stratejisi")
        assert "MACD" in _hint_indicators("MACD histogram cross")

    def test_empty_hint_no_indicators(self):
        assert _hint_indicators("") == []
        assert _hint_indicators("kârlı bir şey bul") == []

    def test_no_false_positive_substring(self):
        # 'ma' RSI/ADX içermeyen kelimelerde yanlış eşleşmemeli
        assert _hint_indicators("maksimum kar minimum drawdown") == []
        # 'smart' içindeki 'ma' SMA sanılmamalı
        assert "SMA/MA" not in _hint_indicators("smart momentum idea")


class TestExplorationDirective:
    def test_faithful_when_indicators_present(self):
        d = _exploration_directive("RSI + ADX + ATR")
        assert "ÇEKİRDEĞİ" in d  # istenen set stratejinin çekirdeği
        assert "RSI" in d and "ADX/DMI" in d and "ATR" in d
        # Yaratıcı tamamlayıcı ekleme yönergesi
        assert "YARATICI" in d and "TAMAMLAYICI" in d
        # Seti FARKLI bir aileyle DEĞİŞTİR yönergesi OLMAMALI
        assert "FARKLI bir indikatör ailesi seç" not in d

    def test_diverse_when_no_indicators(self):
        d = _exploration_directive("")
        assert "FARKLI bir indikatör ailesi seç" in d
        assert "BU SETTE KAL" not in d

    def test_directive_fills_prompt_without_keyerror(self):
        """_AGENT_IDEA_PROMPT.format çağrısı yeni placeholder'la kırılmamalı."""
        from agent import _AGENT_IDEA_PROMPT

        out = _AGENT_IDEA_PROMPT.format(
            market_tr="kripto trading",
            market_note="",
            exploration_directive=_exploration_directive("RSI ADX ATR"),
            history="yok",
            used_concepts="yok",
            hint="RSI ADX ATR",
        )
        assert "ÇEKİRDEĞİ" in out and "YASAK" in out
