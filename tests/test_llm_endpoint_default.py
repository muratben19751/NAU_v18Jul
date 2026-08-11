"""LLM ucunun varsayılanı: resmi API, proxy yalnız AÇIKÇA istenince.

DeepR 2026-08-11 [YÜKSEK]: `llm_dispatch._build_client()` varsayılan olarak
`http://localhost:6655` (bir Hyperspace proxy'si) adresine gidiyordu. Yalnız
`ANTHROPIC_API_KEY` veren temiz bir kurulumda -- README'nin "doğrudan API"
dediği durumda -- her çağrı bu makinede koşmayan bir porta gidiyor,
`ConnectionError` ile düşüyor, hata "kredi tükendi" olmadığı için
`_is_credit_exhausted` tetiklenmiyor ve çağıranların graceful fallback'i
koşuyu "Random ... (Claude unavailable)" kompozisyonlarıyla NORMAL gibi
sürdürüyordu. Aynı anahtar `strategy_studio/ai.py` tarafından ise sabit resmi
uca gönderiliyordu: tek env değişkeni, iki farklı uç.

Bu dosya üç şeyi çiviliyor:
1. Uç varsayılanı resmi API (proxy yalnız `ANTHROPIC_BASE_URL` ile).
2. Proxy ayarlıyken ulaşılamıyorsa hata ADIYLA anılır ("proxy yanıt vermiyor:
   <url>"), jenerik bir bağlantı hatası gibi görünmez.
3. Studio'nun AI istemcisi aynı değişkeni okur -- iki entegrasyon aynı uca
   konuşur, bir proxy anahtarı üçüncü tarafa gitmez.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]], [[kesilme_ve_degrade_gorunurlugu]].
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from anthropic import Anthropic, APIConnectionError

import llm_dispatch
from strategy_studio.ai import HttpAnthropicClient, SuggestionFailure

OFFICIAL = "https://api.anthropic.com"


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """`_get_client()` inşa ettiğini saklar; bir testin istemcisi diğerine sızmasın."""
    llm_dispatch._client = None
    yield
    llm_dispatch._client = None


class TestConfiguredBaseUrl:
    def test_unset_means_official_api(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        assert llm_dispatch._configured_base_url() == ""

    def test_whitespace_only_is_treated_as_unset(self, monkeypatch):
        # Boş bir değişken "proxy istiyorum" DEĞİLDİR; `export ANTHROPIC_BASE_URL=`
        # ile temizlemeye çalışan kullanıcıyı yine proxy'ye düşürmemeli.
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "   ")

        assert llm_dispatch._configured_base_url() == ""

    def test_explicit_value_is_returned_trimmed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", " http://localhost:6655 ")

        assert llm_dispatch._configured_base_url() == "http://localhost:6655"


class TestBuildClientEndpoint:
    def test_api_key_alone_goes_to_the_official_endpoint(self, monkeypatch):
        """Denetlenen hatanın tam senaryosu: yalnız anahtar var, proxy yok."""
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        client = llm_dispatch._build_client()

        assert isinstance(client, Anthropic)
        assert "localhost" not in str(client.base_url)
        assert str(client.base_url).rstrip("/") == OFFICIAL

    def test_an_explicit_proxy_is_still_honored(self, monkeypatch):
        monkeypatch.setenv("NAUTILUS_LLM_BACKEND", "api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")

        client = llm_dispatch._build_client()

        assert str(client.base_url).startswith("http://localhost:6655")


class TestEndpointErrorNaming:
    def test_connection_error_names_the_proxy_when_one_is_configured(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")
        exc = APIConnectionError(request=httpx.Request("POST", "http://localhost:6655"))

        out = llm_dispatch._endpoint_error(exc)

        assert isinstance(out, llm_dispatch.LLMEndpointUnreachable)
        assert "proxy yanıt vermiyor" in str(out)
        assert "http://localhost:6655" in str(out)

    def test_connection_error_is_left_alone_on_the_official_endpoint(self, monkeypatch):
        # Resmi uçta bir bağlantı hatası "proxy ölü" değil "ağ yok" demektir;
        # yanlış teşhis koymaktansa hatayı olduğu gibi bırak.
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        exc = APIConnectionError(request=httpx.Request("POST", OFFICIAL))

        assert llm_dispatch._endpoint_error(exc) is exc

    def test_a_non_connection_error_is_never_relabelled(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")
        exc = ValueError("bad request")

        assert llm_dispatch._endpoint_error(exc) is exc

    def test_the_dispatch_choke_point_raises_the_named_error(self, monkeypatch):
        """Uçtan uca: `_create_message_once` üzerinden geçen çağrı."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")
        # Gerçek deftere satır yazmadan: hata yolunda TAHMİNİ girdi kaydedilir.
        monkeypatch.setattr("token_ledger.record", lambda *a, **k: None)

        def _boom(**kwargs):
            raise APIConnectionError(
                request=httpx.Request("POST", "http://localhost:6655")
            )

        client = SimpleNamespace(messages=SimpleNamespace(create=_boom))

        with pytest.raises(llm_dispatch.LLMEndpointUnreachable, match="6655"):
            llm_dispatch._create_message_once(
                client,
                "idea",
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            )

    def test_a_credit_error_is_not_swallowed_by_the_new_branch(self, monkeypatch):
        """Kredi tükenmesi teşhisi bozulmasın — çeviri yalnız bağlantı hatasında."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")

        assert llm_dispatch._is_credit_exhausted(
            llm_dispatch._endpoint_error(Exception("insufficient credit balance"))
        )


class TestStudioAiClientEndpoint:
    def test_default_endpoint_is_the_official_api(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        assert HttpAnthropicClient(api_key="k").base_url == OFFICIAL

    def test_it_reads_the_same_env_var_as_the_agent_path(self, monkeypatch):
        # Denetimin ikinci yarısı: iki entegrasyon aynı anahtarı FARKLI uçlara
        # gönderiyordu. Aynı değişken → aynı uç.
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655/")

        assert HttpAnthropicClient(api_key="k").base_url == "http://localhost:6655"

    def test_the_request_actually_goes_to_the_configured_endpoint(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")
        seen = {}

        def _fake_post(url, **kwargs):
            seen["url"] = url
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", _fake_post)

        assert HttpAnthropicClient(api_key="k").complete("p") == "ok"
        assert seen["url"] == "http://localhost:6655/v1/messages"

    def test_an_unreachable_proxy_is_reported_as_such(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:6655")
        monkeypatch.setattr(HttpAnthropicClient, "_RETRY_WAITS", ())  # bekleme yok

        def _refused(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", _refused)

        with pytest.raises(SuggestionFailure, match="proxy yanıt vermiyor"):
            HttpAnthropicClient(api_key="k").complete("p")

    def test_the_official_endpoint_keeps_the_raw_connection_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.setattr(HttpAnthropicClient, "_RETRY_WAITS", ())

        def _refused(url, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "post", _refused)

        with pytest.raises(httpx.ConnectError):
            HttpAnthropicClient(api_key="k").complete("p")
