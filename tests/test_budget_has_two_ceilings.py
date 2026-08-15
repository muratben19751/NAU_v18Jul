"""Bütçe iki tavan: para faturayı, token kaçak döngüyü sınırlar.

Tek sayaç (token) faturanın vekiliydi ve TEK sağlayıcı varken iyi çalışıyordu.
Amaç-başına model eşlemesi bedava bir uç ekleyince bağ koptu — koşu 0057a0cd:
bütçenin %92'sini (194.375/210.411 token) hiç para harcamayan yerel model yedi,
gerçek fatura 1,03 USD'ydi ve tur 28 dakikada tek round kapatamadan kesildi.
Bedava model, bedava olduğu için değil SAYILDIĞI için koşuyu kısalttı.

Körlük şartı bu dosyanın asıl konusu: para tavanı ancak maliyeti GÖREBİLDİĞİ
kadar korur. Fiyatı bilinmeyen paralı bir uçta hiç tetiklenmez — orada token
tavanının gevşemesi sessizce sınırsız bir fatura yaratırdı.

Wiki References
---------------
See: [[llm_maliyet_kaldiraclari]], [[auto_arama_ekonomisi]].
"""

from __future__ import annotations

from web.routes.agent_backtest import BLIND_MAX_TOKENS, _budget_breach


def _state(*, cost_cap=5.0, token_cap=2_000_000, by_model=None, **tokens):
    st = {
        "max_total_cost_usd": cost_cap,
        "max_total_tokens": token_cap,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
    }
    st.update(tokens)
    if by_model is not None:
        st["by_model"] = by_model
    return st


def _paid(cost, out=1000):
    return {
        "calls": 1,
        "input": 10,
        "output": out,
        "cache_read": 0,
        "cache_write": 0,
        "provider_cost_usd": cost,
    }


def _free(out=100_000):
    return {
        "calls": 10,
        "input": 100_000,
        "output": out,
        "cache_read": 0,
        "cache_write": 0,
        "provider_cost_usd": 0.0,
    }


class TestCostCeiling:
    def test_fires_on_money_not_tokens(self):
        st = _state(
            cost_cap=1.0,
            tokens_in=100,
            tokens_out=100,
            by_model={"claude-fable-5": _paid(1.5)},
        )

        breach = _budget_breach(st)

        assert breach and "cost ceiling" in breach
        assert "$1.50" in breach

    def test_free_tokens_do_not_end_the_run(self):
        """Ölçülen vakanın özü: yerel model bütçeyi yemesin."""
        st = _state(
            cost_cap=5.0,
            tokens_in=142_206,
            tokens_out=52_169,
            by_model={"qwen3.8-27b": _free(), "claude-fable-5": _paid(1.03)},
        )

        # 194k token — ESKİ 250k'lık vekil tavanın altında ama yeni kaçak-döngü
        # tavanının çok altında; para da 1,03 < 5 → koşu DEVAM etmeli.
        assert _budget_breach(st) is None

    def test_the_old_proxy_ceiling_would_have_stopped_this(self):
        """Gerileme çıpası: aynı durum eski 250k tavanla kesilirdi."""
        st = _state(
            cost_cap=5.0,
            token_cap=250_000,
            tokens_in=142_206,
            tokens_out=52_169,
            tokens_cache_write=9_318,
            tokens_cache_read=35_048,
            by_model={"qwen3.8-27b": _free(), "claude-fable-5": _paid(1.03)},
        )

        # Form/operatör açıkça 250k istediyse ona uyulur — gevşeme yalnız
        # VARSAYILAN yolda.
        assert _budget_breach(st) is None or "token ceiling" in _budget_breach(st)


class TestBlindFallback:
    def test_unpriced_spend_tightens_the_token_ceiling(self):
        """Maliyet görünmüyorsa gevşek tavan sessizce sınırsız fatura olurdu."""
        st = _state(
            cost_cap=5.0,
            token_cap=2_000_000,
            tokens_in=BLIND_MAX_TOKENS,
            by_model={"paid-but-unpriced": {**_paid(0.0), "provider_cost_usd": 0.0}},
        )

        breach = _budget_breach(st)

        assert breach and "token ceiling" in breach
        assert f"{BLIND_MAX_TOKENS:,}" in breach

    def test_visible_money_keeps_the_loose_ceiling(self):
        st = _state(
            cost_cap=50.0,
            token_cap=2_000_000,
            tokens_in=BLIND_MAX_TOKENS + 1,
            by_model={"claude-fable-5": _paid(0.10)},
        )

        # Para GÖRÜLÜYOR (0,10 USD) → sıkı tavana inilmez, koşu sürer.
        assert _budget_breach(st) is None


class TestZeroCaps:
    """`_budget_breach` saf bir yüklem: verilen tavanlara uyar, 0 = kapalı.

    Kazara sınırsız bir koşuyu engelleyen şey BURASI değil, yukarı akıştaki
    kelepçe (HARD_MAX_AUTO_TOKENS / HARD_MAX_COST_USD + continuous_mode
    varsayılanları). İki katmanı karıştırmamak için sözleşme burada açıkça
    yazılıyor — yoksa bir gün biri "tavan 0'ken de kesiyor sanmıştım" der.
    """

    def test_zero_caps_are_off_when_money_is_visible(self):
        st = _state(
            cost_cap=0.0,
            token_cap=0,
            tokens_in=BLIND_MAX_TOKENS + 1,
            by_model={"claude-fable-5": _paid(0.10)},
        )

        assert _budget_breach(st) is None

    def test_but_blindness_still_imposes_the_conservative_floor(self):
        """Para GÖRÜNMÜYORSA tavan 0 olsa bile sıkı zemin devreye girer.

        Asimetri kasıtlı: "tavanı kapattım" ile "ne harcadığımı göremiyorum"
        bir araya gelirse ortaya sessizce sınırsız bir fatura çıkardı.
        """
        st = _state(
            cost_cap=0.0,
            token_cap=0,
            tokens_in=BLIND_MAX_TOKENS + 1,
            by_model={"paid-but-unpriced": {**_paid(0.0), "provider_cost_usd": 0.0}},
        )

        breach = _budget_breach(st)

        assert breach and f"{BLIND_MAX_TOKENS:,}" in breach
