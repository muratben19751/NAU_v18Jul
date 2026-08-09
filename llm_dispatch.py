"""LLM dispatch core: client construction/selection, the credit-exhaustion
fallback, the truncation-retry + learned-ceiling mechanism, and the single
choke point (`_create_message`/`_create_message_once`) through which every
app LLM call passes.

agent.py decomposition (Adım 9 -- the dispatch-core session, after Adım 2-4/6
moved model/effort selection, the control plane, and the two provider
backends to llm_client.py/openrouter_backend.py). Extracted verbatim from
agent.py's dispatch span (`_client`/`_client_lock` through `_get_client`),
plus `_usage_dict` -- physically stranded deep inside agent.py's Domain C
(LLM proposal/backtest orchestration) even though it is a pure function this
module's own `_create_message_once` also needs, so it moved here too; its
Domain C call sites keep working via agent.py's re-export.

`_client`/`_client_lock` and `_ledger_record` are deliberately NOT re-exported
back into agent.py:

- `_client`/`_client_lock`: a mutable singleton, this module's own
  transport-layer state -- matches openrouter_backend.py's `_or_client`/
  `_or_client_lock`, which were never re-exported into agent.py either
  (Adım 6). A test that needs to reset the cached client between cases
  imports this module directly (`llm_dispatch._client = None`), same as
  openrouter_backend.py's own singleton would be reset.
- `_ledger_record`: re-exporting it would invite `monkeypatch.setattr(agent,
  "_ledger_record", ...)` to silently stop working once a caller (like
  `_create_message_once`) also lives here and reads it as a bare module
  global -- the patch would land on agent's copy of the name while the real
  call site reads this module's own. The documented-safe fix is to patch
  `token_ledger.record` directly (works regardless of which module calls
  `_ledger_record`); see tests/test_regression_anchors.py's
  `_no_real_ledger_writes` fixture.

`from web_research import web_research_strategies` stayed BEHIND in agent.py
even though it sat mid-span here -- it is Domain C's dependency
(`propose_composed_strategy`), stranded next to this code by accident, not
part of the dispatch core.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]] (model seçici, canlı OpenRouter kataloğu,
`model_unavailable_reason` ile kapıda ret), [[llm_maliyet_kaldiraclari]]
(defter denetimi + maliyet kaldıraçları -- `_ledger_record`, `max_tokens`
tavanları), [[kesilme_ve_degrade_gorunurlugu]] (`TruncatedResponse` + tek
seferlik büyük-tavan denemesi, `_LEARNED_MAX_TOKENS_LOCK` + `max()` yazımı).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

from anthropic import Anthropic

import llm_client  # module-qualified _model_lock/_active_model mutation below
from llm_client import (
    FALLBACK_MODEL,
    LLMCallCancelled,
    _admit_llm_request,
    _check_llm_cancelled,
    _ClaudeCLIClient,
    _observe_llm,
    _output_cap_telemetry,
    current_model,
)
from openrouter_backend import (
    _build_openrouter_client,
    _get_openrouter_client,
    _OpenRouterClient,
    _or_create_with_backoff,
)

_client: Anthropic | _ClaudeCLIClient | _OpenRouterClient | None = None
_client_lock = threading.Lock()


def model_unavailable_reason(model: str | None) -> str:
    """Bu picker değeri neden KOŞAMAZ — koşabiliyorsa "".

    Ağa çıkmaz; yalnız yapılandırmayı yoklar (anahtar var mı, istemci
    kurulabiliyor mu). Koşu BAŞLAMADAN çağrılır: eksik yapılandırma yüzünden
    LLM'e hiç ulaşamayan bir AUTO turu, seçilen modelin önerileri yerine
    "Random … (Claude unavailable)" kompozisyonları üretir ve bunu normal bir
    koşu gibi gösterir — sessiz bozulma yerine kapıda ret.

    Yalnız "or:" için anlamlı: Claude yolunun ön koşulu (abonelik CLI'ı ya da
    ANTHROPIC_API_KEY) uygulamanın varsayılan yolu, ayrıca yoklanmaz.
    """
    m = (model or "").strip()
    if not m.startswith("or:"):
        return ""
    try:
        _get_openrouter_client()
    except Exception as e:  # eksik anahtar / eksik `openai` paketi
        return str(e)
    return ""


_CREDIT_EXHAUSTED_SIGNALS = (
    "credit balance is too low",  # API: 400
    "billing_error",
    "insufficient credit",
    "monthly spend limit",  # claude-cli: 429 but PERMANENT
)


def _is_credit_exhausted(exc: Exception) -> bool:
    """Is this credit/quota exhaustion? (NOT a rate-limit or transient error)

    The two backends emit two distinct signals:

    - API: HTTP 403 + ``error.type == "billing_error"``. The SDK's `.type` field
      separates billing_error from permission_error (both are 403), so a typed
      field is used instead of string matching.
    - claude-cli (subscription): does not produce a typed exception and reports
      the spend limit with the SAME 429 code as a transient rate-limit → the
      distinction can only be made from the message text ("monthly spend limit",
      which advises switching models as the remedy).

    That's why a bare 429 is DELIBERATELY excluded: it is transient, retried with
    backoff — permanently switching the model would be wrong. Only the permanent
    expressions above match.
    """
    if getattr(exc, "type", None) == "billing_error":
        return True
    msg = f"{getattr(exc, 'message', '')} {exc}".lower()
    return any(s in msg for s in _CREDIT_EXHAUSTED_SIGNALS)


def _ledger_record(resp, called_model: str, purpose: str) -> None:
    """Append this call's token usage to the persistent per-model ledger.

    Best-effort: a ledger I/O error must never break an LLM call. The model
    recorded is the one the API actually answered with (``resp.model``) so the
    Fable→Opus fallback is attributed correctly; the CLI response carries no
    ``.model``, so we fall back to the model we called with. See
    ``token_ledger`` + [[nau_token_tuketim_izleme]].
    """
    try:
        import token_ledger

        actual = getattr(resp, "model", None) or called_model
        token_ledger.record(actual, getattr(resp, "usage", None), purpose)
    except Exception:
        pass


class TruncatedResponse(RuntimeError):
    """Yanıt ``max_tokens`` tavanına dayanıp kesildi — model kusuru değil, bütçe.

    Kesilme kendi adıyla gelmezse aşağıda bir ``JSONDecodeError`` olarak görünür
    ve suçu **modele** atar ("geçerli JSON üretemedi"); oysa cevabı kesen biziz.
    Bu tip, o yanlış-atfı kapatmak için var: çağıranların fallback açıklamasında
    ``fallback (TruncatedResponse)`` yazar ve sebep okunabilir kalır.
    """


# Sağlayıcıların "yer kalmadı" cevabı: Anthropic ``max_tokens``, OpenAI-uyumlu
# uçlar ``length`` der. _ORResponse ikincisini birincisine çevirir.
_TRUNCATION_STOP_REASONS = ("max_tokens", "length")
# Kesilmede tavan bir kez bu katsayıyla büyütülüp yeniden denenir. Kesilme kalıcı
# bir arıza değil bütçe arızasıdır (429 gibi); fallback'e düşmek koşuyu rastgele
# kompozisyona çevirir. Ödenen bedel bir fazladan çağrı.
_TRUNCATION_RETRY_SCALE = 4
_TRUNCATION_RETRY_CAP = 16000

# JSON döndüren üretim çağrılarının tavanları. Eski değerler (composed 900,
# idea 400) Claude'un kısa JSON'una göre ayarlanmıştı ve önce düşünüp sonra yazan
# bir modelde (ölçüm: moonshotai/kimi-k3) çıktının TAMAMI tavana dayanıyordu —
# 5 çağrının 5'i kesildi, 5'i de fallback'e düştü. Tavan, promptun değil ÜRETENİN
# özelliğine göre kalibre olduğu için bolluk tarafında hata yapılır: kullanılmayan
# tavan bedava, yarım JSON toptan kayıp.
MAX_TOKENS_COMPOSED = 4000
MAX_TOKENS_IDEA = 1500

# Ceilings LEARNED at runtime, keyed by (model, purpose). The retry below is
# correct but had no memory: measured on run 3467219a, EVERY `idea` and
# `custom_block` call to moonshotai/kimi-k3 hit the default ceiling and was
# retried at 4× — 9 and 10 times respectively, a 100% rate. So each generation
# cost two calls, the first one's output discarded in full.
#
# A static default cannot fix this: the right ceiling is a property of the
# ENDPOINT (how much a model thinks before it writes), and the endpoint is
# picked per run from a live list. Raising the constants would overshoot for
# terse models and still undershoot the next verbose one. Remembering the
# escalation instead costs one truncated call per (model, purpose) per process
# and nothing after that.
# DeepR 2026-08-08 [DÜŞÜK]: unlocked, unlike every other shared mutable
# dict in this app (state.py's threading.Lock, token_ledger's _LOCK,
# custom_block_store's _STORE_LOCK). AUTO can run more than one worker
# thread; two racing to grow the SAME (model, purpose) ceiling could clobber
# each other's write. Not a crash (dict ops are GIL-safe), just a silent
# loss of the learned ceiling — the next call for that key falsely
# truncates again and re-pays the two-call cost the whole mechanism exists
# to avoid.
_LEARNED_MAX_TOKENS_LOCK = threading.Lock()
_LEARNED_MAX_TOKENS: dict[tuple[str, str], int] = {}


def _was_truncated(resp) -> bool:
    """Yanıt tavana dayanıp kesildi mi? (bilgi yoksa hayır)"""
    return (
        str(getattr(resp, "stop_reason", "") or "").lower() in _TRUNCATION_STOP_REASONS
    )


def _create_message(client, _purpose: str = "", **kwargs):
    """``_create_message_once`` + kesilmede tek seferlik büyük-tavan denemesi.

    ``max_tokens`` yapılandırılmış çıktı isteyen bir çağrıda maliyet freni değil
    **doğruluk önkoşuludur**: yarım JSON hiçbir işe yaramaz. Tavanlar bir kez ve
    o günkü modelin üslubuna göre ayarlanır; önce düşünüp sonra yazan bir modelde
    aynı tavan sığmaz. Bu yüzden kesilme burada yakalanır, tavan büyütülüp bir kez
    yeniden denenir, yine sığmazsa ``TruncatedResponse`` fırlatılır — çağıranın
    fallback'i devreye girer ama sebep artık okunabilir.

    Başarılı büyütme ``_LEARNED_MAX_TOKENS``'a yazılır: aynı (model, amaç) çifti
    bir daha ilk çağrıyı çöpe atmaz. Ölçüm: kimi-k3'te her `idea`/`custom_block`
    çağrısı tavana dayanıyordu, yani her üretim iki çağrı ediyordu.
    """
    _check_llm_cancelled()
    base = int(kwargs.get("max_tokens") or 0)
    key = (current_model(), _purpose or "llm")
    with _LEARNED_MAX_TOKENS_LOCK:
        learned = _LEARNED_MAX_TOKENS.get(key, 0)
    if learned > base:
        # This endpoint has already proven it needs the bigger budget. Unused
        # ceiling is free; a truncated response is a wasted call in full.
        base = learned
        kwargs = {**kwargs, "max_tokens": base}

    resp = _create_message_once(client, _purpose, **kwargs)
    if not _was_truncated(resp):
        return resp

    bigger = min(base * _TRUNCATION_RETRY_SCALE, _TRUNCATION_RETRY_CAP)
    if base <= 0 or bigger <= base:
        raise TruncatedResponse(
            f"{_purpose or 'llm'}: yanıt max_tokens={base} tavanında kesildi"
        )

    logging.warning(
        "%s: yanıt max_tokens=%d tavanında kesildi — %d ile yeniden deneniyor "
        "(bu uç için öğrenildi, sonraki çağrılar %d ile başlayacak)",
        _purpose or "llm",
        base,
        bigger,
        bigger,
    )
    _check_llm_cancelled()
    resp = _create_message_once(client, _purpose, **{**kwargs, "max_tokens": bigger})
    if _was_truncated(resp):
        # Do NOT learn a ceiling that did not work either — the next call would
        # then pay the big budget AND still truncate.
        raise TruncatedResponse(
            f"{_purpose or 'llm'}: yanıt max_tokens={bigger} ile de kesildi"
        )
    with _LEARNED_MAX_TOKENS_LOCK:
        # max(): a concurrent writer for the same key may have already
        # learned a larger ceiling — never regress it under a race.
        _LEARNED_MAX_TOKENS[key] = max(bigger, _LEARNED_MAX_TOKENS.get(key, 0))
    return resp


def _create_message_once(client, _purpose: str = "", **kwargs):
    """messages.create + automatic Fable→Opus fallback on credit exhaustion.

    The model kwarg is added HERE; callers do not pass a model. On a credit
    error the active model is permanently switched to FALLBACK_MODEL and the
    request is retried once (the request body is passed as-is — both models
    share the same API surface).

    Every successful call is recorded to the persistent per-model token ledger
    (``token_ledger``); ``_purpose`` tags the call site (best-effort, "" is
    fine). This is the single choke point through which all app LLM calls pass.
    """
    _check_llm_cancelled()
    kwargs = dict(kwargs)
    if not isinstance(client, _ClaudeCLIClient):
        # The CLI backend has its own ceiling (NAUTILUS_CLI_TIMEOUT, default
        # 300s — a subprocess with real thinking time, not a bare HTTP call)
        # applied in _ClaudeCLIMessages.create. Injecting this shorter
        # cross-backend safety net into its kwargs too would silently cap
        # slow high-effort CLI calls that used to fit comfortably under 300s.
        kwargs.setdefault(
            "timeout", float(os.environ.get("NAUTILUS_LLM_CALL_TIMEOUT", "120"))
        )
    model = current_model()

    requested_max_tokens = int(kwargs.get("max_tokens") or 0)

    def _call(called_model: str, fn):
        # Admission happens for every real provider attempt, including a
        # truncation retry or model fallback.  The AUTO hook can therefore
        # reject the request before any prompt/output tokens are spent.
        _admit_llm_request(kwargs, model=called_model, purpose=_purpose or "llm")
        started = time.monotonic()
        try:
            response = fn()
        except Exception as exc:
            _observe_llm(
                model=called_model,
                purpose=_purpose or "llm",
                usage=None,
                duration_s=round(time.monotonic() - started, 3),
                max_tokens=requested_max_tokens,
                status="cancelled" if isinstance(exc, LLMCallCancelled) else "error",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            raise
        usage = _usage_dict(response)
        _observe_llm(
            model=called_model,
            purpose=_purpose or "llm",
            usage=usage,
            duration_s=round(time.monotonic() - started, 3),
            max_tokens=requested_max_tokens,
            status="truncated" if _was_truncated(response) else "ok",
            **_output_cap_telemetry(
                client,
                usage,
                requested_max_tokens,
                provider_enforced=model.startswith("or:"),
            ),
        )
        _check_llm_cancelled()
        return response

    if model.startswith("or:"):
        # Koşu-başına OpenRouter yolu (thread pin "or:<id>"): varsayılan
        # backend'e dokunmadan bu çağrı OpenRouter'a gider. Fable→Opus kredi
        # fallback'i Claude'a özgüdür — burada uygulanmaz; 429 dışındaki hata
        # çağırana düşer (çağıranların kendi graceful fallback'leri var).
        or_model = model[3:]
        resp = _call(or_model, lambda: _or_create_with_backoff(or_model, **kwargs))
        _ledger_record(resp, or_model, _purpose)
        return resp
    try:
        resp = _call(model, lambda: client.messages.create(model=model, **kwargs))
        _ledger_record(resp, model, _purpose)
        return resp
    except Exception as e:
        if model == FALLBACK_MODEL or not _is_credit_exhausted(e):
            raise
        with llm_client._model_lock:
            llm_client._active_model = FALLBACK_MODEL
        logging.warning(
            "%s credit exhausted (%s) — permanently falling back to %s",
            model,
            type(e).__name__,
            FALLBACK_MODEL,
        )
        resp = _call(
            FALLBACK_MODEL,
            lambda: client.messages.create(model=FALLBACK_MODEL, **kwargs),
        )
        _ledger_record(resp, FALLBACK_MODEL, _purpose)
        return resp


def _find_claude_cli() -> str | None:
    override = os.environ.get("NAUTILUS_CLAUDE_CLI", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("claude")


def _build_client() -> Anthropic | _ClaudeCLIClient | _OpenRouterClient:
    """Backend selection (NAUTILUS_LLM_BACKEND env var):

    - "api":        anthropic SDK — ANTHROPIC_API_KEY / ~/.nautilus_proxy_key required
    - "claude-cli": Claude Code CLI (`claude -p`) — subscription (OAuth), no key needed
    - "openrouter": OpenRouter (OpenAI-compatible API) — OPENROUTER_API_KEY required
    - "auto" (default): API if a key exists, otherwise the claude CLI
    """
    backend = os.environ.get("NAUTILUS_LLM_BACKEND", "auto").strip().lower()

    if backend == "openrouter":
        return _build_openrouter_client()

    # Hyperspace AI proxy takes priority; falls back to direct Anthropic.
    # The proxy key must be set via ANTHROPIC_API_KEY env var or
    # ~/.nautilus_proxy_key file — never hardcoded.
    proxy_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not proxy_key:
        key_file = Path.home() / ".nautilus_proxy_key"
        if key_file.exists():
            proxy_key = key_file.read_text().strip()

    if backend in ("api", "auto") and proxy_key:
        proxy_url = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:6655")
        logging.info("LLM backend: anthropic SDK (%s)", proxy_url)
        return Anthropic(base_url=proxy_url, api_key=proxy_key)
    if backend == "api":
        raise RuntimeError(
            "NAUTILUS_LLM_BACKEND=api but ANTHROPIC_API_KEY is not set. "
            "Set it as an environment variable or write it to ~/.nautilus_proxy_key"
        )

    cli = _find_claude_cli()
    if cli:
        logging.info("LLM backend: claude CLI / subscription (%s)", cli)
        return _ClaudeCLIClient(cli)
    if backend == "claude-cli":
        raise RuntimeError(
            "NAUTILUS_LLM_BACKEND=claude-cli but the `claude` CLI was not found on PATH. "
            "Install Claude Code and sign in (subscription), or set NAUTILUS_CLAUDE_CLI."
        )
    raise RuntimeError(
        "No LLM access: ANTHROPIC_API_KEY is not set and the `claude` CLI was not found. "
        "Either set an API key (env var or ~/.nautilus_proxy_key) or install Claude Code "
        "and sign in with your subscription."
    )


def _get_client() -> Anthropic | _ClaudeCLIClient | _OpenRouterClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


def _usage_dict(resp) -> dict:
    """resp.usage → normalize token dict (M1583: counted on every LLM call)."""
    u = getattr(resp, "usage", None)
    usage = {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0)
        or 0,
    }
    cost = getattr(u, "cost_usd", None)
    if cost is not None:
        usage["cost_usd"] = float(cost)
    return usage
