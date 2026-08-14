"""AUTO bütçesi cache okumasını tam fiyattan saymasın.

`max_total_tokens` bir MALİYET tavanı olarak konuldu (bkz.
`DEFAULT_CONTINUOUS_MAX_TOKENS` yorumu: "unbounded bill"). Ama sayaç dört
kalemi — girdi, çıktı, cache okuması, cache yazımı — 1:1 topluyordu. Cache
okuması girdinin ~%10'u kadar fiyatlanır; tam fiyattan sayılması tavanı bir
muhasebe artefaktına çeviriyordu.

DeepR 2026-08-14 [YÜKSEK], canlı koşu 65a87244 ile ölçtü: 3 tur sonunda
235.222/250.000 doldu, **%77'si cache kalemiydi**, sağlayıcının bildirdiği
gerçek maliyet 2,30 USD'ydi. Operatör 4 saat seçmişti; koşu 15. dakikada
`outcome=budget` ile bitti. İkinci ve daha sinsi sonuç: prompt-cache'i
İYİLEŞTİRMEK bile koşuyu uzatmıyordu — kazanılan her cache-hit tavana ceza
olarak yazılıyordu, yani sistem kendi verimliliğini cezalandırıyordu.

Bu dosya iki şeyi bağlar: indirimin uygulandığını ve düzeltmenin yalnız
GEVŞETTİĞİNİ (hiçbir koşu bu değişiklik yüzünden erken bitmemeli).

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import pytest

import web.routes.agent_backtest as ab

# Koşu 65a87244'ün gerçek sayaçları (agent_sessions/65a87244.jsonl,
# token_snapshot olayı) — regresyon çıpası olarak birebir korunuyor.
LIVE_RUN_65a87244 = {
    "tokens_in": 56,
    "tokens_out": 54_269,
    "tokens_cache_read": 94_745,
    "tokens_cache_write": 86_152,
}
LIVE_RUN_CEILING = 250_000


class TestCacheReadsAreDiscounted:
    def test_a_cache_read_costs_a_tenth_of_an_input_token(self):
        assert ab._token_usage({"tokens_cache_read": 1_000}) == 100
        assert ab._token_usage({"tokens_in": 1_000}) == 1_000

    def test_the_other_three_fields_are_still_counted_in_full(self):
        """Yalnız cache OKUMASI indirimli — diğerleri eski ağırlıkta.

        Çıktı/cache-yazımı gerçekte girdiden pahalıdır, ama onları yukarı
        ağırlıklandırmak sayacı SIKARDI ve `DEFAULT_CONTINUOUS_MAX_TOKENS`
        o eski ölçeğe kalibre. Birimi değiştirmek varsayılanı da yeniden
        kalibre etmeyi gerektirir; bu test o kararın sessizce alınmadığını
        garanti eder.
        """
        for field in ("tokens_in", "tokens_out", "tokens_cache_write"):
            assert ab._token_usage({field: 1_000}) == 1_000, field

    def test_every_tracked_field_has_a_weight(self):
        """Yeni bir `tokens_*` alanı ağırlıksız eklenip sessizce düşmesin."""
        assert set(ab._TOKEN_USAGE_FIELDS) == set(ab._TOKEN_USAGE_WEIGHTS)
        assert len(ab._TOKEN_USAGE_WEIGHTS) == 4


class TestTheFixOnlyEverLoosens:
    """Hiçbir ağırlık 1,0'ı aşmamalı: değişiklik kimsenin bütçesini kısmaz."""

    def test_no_weight_exceeds_one(self):
        heavy = {k: w for k, w in ab._TOKEN_USAGE_WEIGHTS.items() if w > 1.0}
        assert not heavy, (
            f"Bu kalemler tavana 1,0'dan fazla sayılıyor: {heavy}. Sayacı "
            "sıkmak, operatörün kalibre ettiği varsayılan tavan altında "
            "koşuların ERKEN bitmesi demektir — birim değişikliği ancak "
            "DEFAULT_CONTINUOUS_MAX_TOKENS ile birlikte yapılabilir."
        )

    def test_the_weighted_total_never_exceeds_the_raw_total(self):
        counted = ab._token_usage(LIVE_RUN_65a87244)
        raw = sum(LIVE_RUN_65a87244.values())
        assert counted <= raw


class TestTheLiveRunThatExposedThisWouldNowSurvive:
    def test_the_run_that_died_at_the_ceiling_now_admits_the_next_call(
        self, monkeypatch
    ):
        """Koşu tavanı DOLDURARAK değil, rezervasyon kapısında ölmüştü.

        Gerçek olay (65a87244): ``llm_budget_rejected`` → "14.778 remaining,
        30.331 reserved". Yani `_admit_llm_budget`, sıradaki çağrının üst
        sınırını sığdıramadı: 235.222 + 30.331 > 250.000. Bu test o ANI
        yeniden kuruyor ve indirimden sonra aynı çağrının kabul edildiğini
        gösteriyor.
        """
        reserve = 30_331  # koşunun reddedilen çağrısının bildirdiği üst sınır
        raw = sum(LIVE_RUN_65a87244.values())
        assert raw + reserve > LIVE_RUN_CEILING  # eski sayaç: RED

        run_id = "live-run-replay"
        monkeypatch.setitem(
            ab._AGENT_PROGRESS,
            run_id,
            {"max_total_tokens": LIVE_RUN_CEILING, **LIVE_RUN_65a87244},
        )
        # Yeni sayaçla aynı çağrı geçer — koşu 3. turda kesilmez.
        ab._admit_llm_budget(run_id, {"total_token_bound": reserve})

        counted = ab._token_usage(LIVE_RUN_65a87244)
        assert counted == pytest.approx(149_951, abs=2)
        assert counted + reserve < LIVE_RUN_CEILING

    def test_improving_the_cache_hit_rate_no_longer_shortens_the_run(self):
        """Asıl sözleşme: cache'i iyileştirmek bütçeyi CEZALANDIRMAMALI.

        Aynı işi daha çok cache okumasıyla yapan bir koşu (yeniden yazım
        yerine okuma), tavana DAHA AZ sayılmalı — eski sayaçta ikisi eşitti.
        """
        wasteful = {"tokens_in": 10_000, "tokens_cache_read": 0}
        cached = {"tokens_in": 0, "tokens_cache_read": 10_000}
        assert ab._token_usage(cached) < ab._token_usage(wasteful)


class TestTheCeilingStillStops:
    """İndirim bütçeyi kaldırmaz — sonsuz fatura koruması yerinde."""

    def test_a_run_over_the_discounted_ceiling_is_still_rejected(self, monkeypatch):
        run_id = "budget-guard-probe"
        monkeypatch.setitem(
            ab._AGENT_PROGRESS,
            run_id,
            {"max_total_tokens": 1_000, "tokens_in": 999_999},
        )
        with pytest.raises(ab.AgentBudgetReached):
            ab._enforce_token_budget(run_id)

    def test_a_pure_cache_read_run_can_still_hit_the_ceiling(self, monkeypatch):
        """0,1 katsayı bir MUAFİYET değil: yeterince okuma yine tavanı doldurur."""
        run_id = "budget-guard-cache-probe"
        monkeypatch.setitem(
            ab._AGENT_PROGRESS,
            run_id,
            {"max_total_tokens": 1_000, "tokens_cache_read": 10_500},
        )
        with pytest.raises(ab.AgentBudgetReached):
            ab._enforce_token_budget(run_id)
