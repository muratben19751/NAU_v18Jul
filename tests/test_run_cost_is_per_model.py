"""Hibritte maliyet, harcayan modele yazılsın.

Amaç-başına model eşlemesi (`NAUTILUS_MODEL_BY_PURPOSE`) bir koşuda birden fazla
modelin para harcamasına izin veriyor. Maliyet satırı bunu bilmiyordu: toplam
token'lar TEK bir modelle fiyatlanıp koşunun pinlenmiş modeline yazılıyordu.

Ölçülen vaka (koşu 14ff96e7): 1,02 USD tamamen Claude'un 7 `custom_block`
çağrısının bedeliydi ama `pricing_model: 'or:qwen3.8-27b'` yazıyordu — yani
ekranda "yerel model 1 dolar yaktı" gibi görünüyordu. Sayı doğru, etiket yanlış;
ve yanlış etiket tam da "yerel model bedava" olan kararı çürütür gibi duruyordu.

Wiki References
---------------
See: [[llm_maliyet_kaldiraclari]], [[model_secici_ve_gorunurluk]].
"""

from __future__ import annotations

from web.routes.agent_backtest import _run_cost


def _slot(calls=1, inp=0, out=0, cr=0, cw=0, cost=None):
    s = {
        "calls": calls,
        "input": inp,
        "output": out,
        "cache_read": cr,
        "cache_write": cw,
        "provider_cost_usd": 0.0,
    }
    if cost is not None:
        s["provider_cost_usd"] = cost
    return s


class TestHybridAttribution:
    def test_the_spender_is_named_not_the_run_pin(self):
        """Gerçek vakanın küçültülmüş hâli: yerel bedava, Claude harcadı."""
        state = {
            "by_model": {
                "qwen3.8-27b": _slot(calls=12, inp=40000, out=36000),  # yerel, bedava
                "claude-fable-5": _slot(calls=7, inp=22000, out=14050, cost=1.019011),
            }
        }

        got = _run_cost(state, fallback_model="or:qwen3.8-27b")

        # Tek harcayan var → adı yazılır; koşunun pini DEĞİL.
        assert got["pricing_model"] == "claude-fable-5"
        assert abs(got["cost_usd"] - 1.019011) < 1e-6
        assert got["cost_source"] == "provider_reported"

    def test_breakdown_keeps_the_free_model_visible(self):
        """Bedava model kırılımda görünmeli — 0 maliyet de bir bilgidir."""
        state = {
            "by_model": {
                "qwen3.8-27b": _slot(calls=12, inp=40000, out=36000),
                "claude-fable-5": _slot(calls=7, out=14050, cost=1.019011),
            }
        }

        by = _run_cost(state, fallback_model="or:qwen3.8-27b")["by_model"]

        assert set(by) == {"qwen3.8-27b", "claude-fable-5"}
        assert by["qwen3.8-27b"]["calls"] == 12
        assert by["claude-fable-5"]["cost_usd"] == 1.019011

    def test_two_spenders_are_not_collapsed_into_one_name(self):
        state = {
            "by_model": {
                "claude-fable-5": _slot(out=1000, cost=0.5),
                "claude-opus-5": _slot(out=1000, cost=0.75),
            }
        }

        got = _run_cost(state, fallback_model="claude-fable-5")

        assert got["pricing_model"] == "hibrit (2 model)"
        assert abs(got["cost_usd"] - 1.25) < 1e-9


class TestBackwardCompatibility:
    def test_without_a_breakdown_the_old_single_model_path_is_used(self):
        """Eski koşu kayıtları ve kırılım biriktirmemiş durumlar bozulmasın."""
        state = {
            "tokens_in": 1000,
            "tokens_out": 500,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "provider_cost_usd": 2.5,
        }

        got = _run_cost(state, fallback_model="claude-fable-5")

        assert got["cost_usd"] == 2.5
        assert got["cost_source"] == "provider_reported"
        assert got["by_model"] == {}

    def test_all_free_still_names_a_model_rather_than_going_blank(self):
        state = {"by_model": {"qwen3.8-27b": _slot(calls=5, inp=100, out=100)}}

        got = _run_cost(state, fallback_model="or:qwen3.8-27b")

        assert got["pricing_model"] == "qwen3.8-27b"
        assert got["by_model"]["qwen3.8-27b"]["calls"] == 5
