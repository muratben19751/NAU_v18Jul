"""Claude Fable 5 strategy-parameter proposer.

Returns a dict:
  {"strategy": "ma_crossover", "params": {"fast": 12, "slow": 34}, "rationale": "..."}

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]] (model seçici, canlı OpenRouter kataloğu,
ücretsiz filtresi, model adının çözülmesi, `model_unavailable_reason` ile kapıda
ret — listelenebilir ≠ çalıştırılabilir), [[llm_maliyet_kaldiraclari]] (defter
denetimi + maliyet kaldıraçları; `_ClaudeCLIMessages.create`'in `max_tokens`'ı
düşürdüğü ve `_purpose`suz çağrıların kör alan oluşturduğu buradan izlenir —
`set_thread_effort`/`current_effort` ile bağlanan `--effort` de orada: modelden
bağımsız ve onunla ÇARPILAN ikinci kol),
[[kesilme_ve_degrade_gorunurlugu]] (`max_tokens` tavanı modelin üslubuna bağlıdır;
`TruncatedResponse` + tek seferlik büyük-tavan denemesi, ve fallback'lerin
`degraded` işaretiyle sayılıp ekrana taşınması; `_LEARNED_MAX_TOKENS_LOCK` +
`max()` yazımı 2026-08-08 DeepR bulgusu).
Geri kalanı app-specific; wiki kapsamı dışında (LLM parametre önericisi, bir
Nautilus kavramı değil).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import llm_client  # noqa: F401  # module-qualified _model_lock/_active_model below


# Adım 3: TerminalLLMError/LLMCallCancelled/LLMTokenBudgetExceeded,
# _LLM_CONTROL/_OUTPUT_RESERVATION_LOCK/_OBSERVED_OUTPUT_HIGH_WATER,
# set_thread_llm_control through _tag_degraded, _random_ctx/
# set_thread_random_seed/_fallback_rng extracted to llm_client.py, imported
# back further down. _interruptible_sleep stays here — it calls _sleep
# (Domain C's patchable time.sleep alias), still not extracted.
def _interruptible_sleep(seconds: float) -> None:
    # Non-AUTO callers have no cancellation source; preserve one sleep call
    # (also keeps deterministic unit tests fast when _sleep is replaced).
    if not callable(getattr(_LLM_CONTROL, "cancel_check", None)):
        _sleep(max(0.0, float(seconds)))
        return
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_llm_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _sleep(min(0.25, remaining))


from anthropic import Anthropic

from app_constants import NO_WINDOW_FLAGS

# Adım 2: MODEL/FALLBACK_MODEL/_active_model/_model_lock and (below)
# _MODEL_OVERRIDE through current_model() extracted to llm_client.py,
# re-exported below. Listed in __all__ (further down) so ruff's F401
# unused-import autofix does not silently delete names that are genuinely
# unused WITHIN this file but still part of agent.<name>'s public surface
# for every external caller (already happened twice while writing this
# step — confirmed via a fresh interpreter, not assumed).
# _client/_client_lock stay here — this module's own not-yet-extracted
# transport-layer state.
from llm_client import (  # noqa: E402
    _LLM_CONTROL,
    _OBSERVED_OUTPUT_HIGH_WATER,
    _OUTPUT_RESERVATION_LOCK,
    EFFORT,
    FALLBACK_MODEL,
    MODEL,
    SELECTABLE_EFFORTS,
    SELECTABLE_MODELS,
    LLMCallCancelled,
    LLMTokenBudgetExceeded,
    TerminalLLMError,
    _admit_llm_request,
    _check_llm_cancelled,
    _fallback_rng,
    _is_free,
    _llm_request_token_bound,
    _observe_llm,
    _output_cap_telemetry,
    _raise_if_llm_control_abort,
    _random_ctx,
    _tag_degraded,
    current_effort,
    current_model,
    is_terminal_llm_error,
    model_id,
    model_label,
    openrouter_catalog,
    openrouter_extra_models,
    openrouter_free_only,
    openrouter_paid_extras,
    resolve_effort,
    selectable_models,
    set_thread_effort,
    set_thread_llm_control,
    set_thread_model,
    set_thread_random_seed,
)
from strategies import STRATEGY_PARAM_SPEC

__all__ = [
    "EFFORT",
    "FALLBACK_MODEL",
    "LLMCallCancelled",
    "LLMTokenBudgetExceeded",
    "MODEL",
    "SELECTABLE_EFFORTS",
    "SELECTABLE_MODELS",
    "TerminalLLMError",
    "_LLM_CONTROL",
    "_OBSERVED_OUTPUT_HIGH_WATER",
    "_OUTPUT_RESERVATION_LOCK",
    "_admit_llm_request",
    "_check_llm_cancelled",
    "_fallback_rng",
    "_is_free",
    "_llm_request_token_bound",
    "_observe_llm",
    "_output_cap_telemetry",
    "_raise_if_llm_control_abort",
    "_random_ctx",
    "_tag_degraded",
    "current_effort",
    "current_model",
    "is_terminal_llm_error",
    "model_id",
    "model_label",
    "openrouter_catalog",
    "openrouter_extra_models",
    "openrouter_free_only",
    "openrouter_paid_extras",
    "resolve_effort",
    "selectable_models",
    "set_thread_effort",
    "set_thread_llm_control",
    "set_thread_model",
    "set_thread_random_seed",
]

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


# 429 geri çekilmesi. Toplam ~65 sn: ücretsiz uçların sınırı DAKİKA başına
# olduğu için bir dakikalık pencereyi kapsamayan bir plan hiçbir işe yaramaz
# (openai SDK'sının varsayılan ~0.5/1 sn'lik iki denemesi tam olarak bu yüzden
# yetersiz kalıyordu).
_OR_RETRY_WAITS = (5.0, 15.0, 45.0)

# Testler beklemeyi burdan yamalar. Global ``time.sleep``'i yamalamak süreçteki
# BAŞKA thread'leri de uykusuz bırakır (arka plan worker'ları) ve zamanlamaya
# duyarlı testleri uzaktan kırar — yama yüzeyi modüle ait olmalı.
_sleep = time.sleep


def _is_rate_limited(exc: Exception) -> bool:
    """429 mı? — `openai` paketi import edilmeden (o bağımlılık opsiyonel)."""
    return (
        getattr(exc, "status_code", None) == 429
        or getattr(exc, "code", None) == 429
        or type(exc).__name__ == "RateLimitError"
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    """Sunucunun söylediği bekleme süresi (``Retry-After``), varsa."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None


def _or_create_with_backoff(or_model: str, **kwargs):
    """OpenRouter çağrısı; 429'da geri çekilip yeniden dener.

    429 kalıcı bir arıza değil, bir **zamanlama** arızasıdır: ücretsiz uçların
    dakika başına istek sınırı, ajan döngüsünün patlama şeklindeki çağrı
    temposuyla uyuşmaz. Yeniden denemeden çağıranın fallback'ine düşmek koşuyu
    rastgele kompozisyona çevirir ve bunu başarı gibi gösterir — yani sonuçları
    sessizce çöpe atar.

    Toplam bekleme ``NAUTILUS_OPENROUTER_429_MAX_WAIT`` ile sınırlı (varsayılan
    75 sn): bir dakikalık pencereyi kapatacak kadar uzun, koşunun STOP'a yanıtını
    kilitlemeyecek kadar kısa. Sunucu ``Retry-After`` söylüyorsa o kullanılır —
    tahminimiz sunucunun bilgisini ezmemeli.
    """
    budget = float(os.environ.get("NAUTILUS_OPENROUTER_429_MAX_WAIT", "75"))
    client = _get_openrouter_client()
    slept = 0.0
    for i, planned in enumerate(_OR_RETRY_WAITS):
        _check_llm_cancelled()
        try:
            return client.messages.create(model=or_model, **kwargs)
        except Exception as e:
            if not _is_rate_limited(e):
                raise
            wait = min(_retry_after_seconds(e) or planned, budget - slept)
            if wait <= 0:  # bütçe bitti — hata çağırana düşsün
                raise
            logging.warning(
                "OpenRouter 429 (%s) — %.0f sn bekleyip yeniden deneniyor (%d/%d)",
                or_model,
                wait,
                i + 1,
                len(_OR_RETRY_WAITS),
            )
            _interruptible_sleep(wait)
            slept += wait
    _check_llm_cancelled()
    return client.messages.create(model=or_model, **kwargs)


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


# ── Web research (DuckDuckGo, no API key required) ─────────────────────────
# Adım 1: extracted to web_research.py. _ddg_search is not re-exported here —
# it had zero consumers outside this module and its own new home; only
# web_research_strategies is imported, for propose_composed_strategy's call
# below.
from web_research import web_research_strategies  # noqa: E402

# ── Claude Code CLI backend (subscription / OAuth — no ANTHROPIC_API_KEY needed) ─
#
# `claude -p` uses Claude Code's existing session (Pro/Max subscription) in
# headless mode. It mimics the minimal messages.create surface the app uses:
# a single user message + optional system prompt → a text-block response.


class _CLITextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _CLIUsage:
    def __init__(self, usage: dict, cost_usd: float | None = None) -> None:
        self.input_tokens = int(usage.get("input_tokens", 0) or 0)
        self.output_tokens = int(usage.get("output_tokens", 0) or 0)
        self.cache_read_input_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        self.cache_creation_input_tokens = int(
            usage.get("cache_creation_input_tokens", 0) or 0
        )
        # 5m/1h TTL split — the CLI writes cache with the 1-HOUR TTL, which
        # prices at 2× input (not the 5-minute 1.25×); the ledger needs the
        # split to bill writes correctly (see token_ledger.cost_usd).
        self.cache_creation = dict(usage.get("cache_creation") or {})
        # Claude CLI's result envelope exposes its own notional charge as
        # ``total_cost_usd``.  Keep that provenance on the normalized response
        # instead of silently recomputing the same call from a local table.
        self.cost_usd = float(cost_usd) if cost_usd is not None else None


class _CLIResponse:
    def __init__(self, text: str, usage: dict, cost_usd: float | None = None) -> None:
        self.content = [_CLITextBlock(text)]
        self.usage = _CLIUsage(usage, cost_usd=cost_usd)
        # CLI'nin max_tokens karşılığı yok (bkz. _ClaudeCLIMessages.create) —
        # bu yolda tavana dayanıp kesilme olamaz.
        self.stop_reason = None


class _CLIError(RuntimeError):
    """claude CLI error; preserves typed fields if a JSON body is present.

    ``message`` (the envelope's ``result``) is kept separate from the raw text:
    the expression that separates a permanent spend limit from a transient
    rate-limit lives there, and it can be lost when the raw text is truncated →
    see _is_credit_exhausted.
    """

    def __init__(self, text: str, status: int | None = None, message: str = "") -> None:
        super().__init__(text)
        self.status = status
        self.message = message


def _poll_until_deadline(
    deadline_s: float, poll_interval: float, step, timeout_msg: str
):
    """Poll ``step(wait_s)`` under a cooperative-cancel + hard-deadline regime.

    Shared skeleton behind the CLI subprocess loop and the OpenRouter
    multiprocessing loop below — both poll a child process for completion
    while checking ``_check_llm_cancelled()`` and a monotonic deadline, just
    with different "is it done yet" primitives (subprocess.communicate vs a
    multiprocessing Pipe), so only the loop math is factored out here; each
    caller still owns its own child-process cleanup around this call.

    ``step`` attempts one bounded wait and returns the result once ready, or
    ``None`` if nothing completed yet (it may itself raise to signal a
    definitive failure, e.g. the child process died). ``timeout_msg`` raises
    ``TimeoutError`` once the deadline is exceeded.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        _check_llm_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(timeout_msg)
        result = step(min(poll_interval, remaining))
        if result is not None:
            return result


class _ClaudeCLIMessages:
    def __init__(self, cli_path: str) -> None:
        self._cli = cli_path

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 0,  # no equivalent in the CLI; prompts already request short answers
        timeout: float | None = None,
        **_ignored: Any,
    ) -> _CLIResponse:
        # Single-turn calls (every existing proposer) join user content verbatim
        # — unchanged behaviour. Multi-turn chat prefixes each turn with a
        # speaker label so the model sees its own prior replies (the CLI runs
        # with --no-session-persistence, so conversation state lives server-side
        # and the full history is re-sent as the prompt each call).
        prompt = "\n\n".join(
            (
                m["content"]
                if len(messages) == 1
                else f"{'Kullanıcı' if m['role'] == 'user' else 'Asistan'}: {m['content']}"
            )
            for m in messages
            if isinstance(m.get("content"), str)
            and m.get("role") in ("user", "assistant")
        )
        cmd = [
            self._cli,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",  # all tools off: a pure LLM call
            "--no-session-persistence",
            "--strict-mcp-config",
        ]

        # Düşünme bütçesi. Bayrağı YALNIZ seçildiğinde geç: "" olduğunda CLI
        # kendi varsayılanını uygular ve komut satırı bugünkü davranışla
        # birebir aynı kalır.
        effort = current_effort()
        if effort:
            cmd += ["--effort", effort]

        # The system prompt is passed via a file: on Windows the command line
        # over the .cmd shim is limited to ~8K chars; prompts containing the
        # catalog can exceed that.
        sys_file: str | None = None
        try:
            if system:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as f:
                    f.write(system)
                    sys_file = f.name
                cmd += ["--system-prompt-file", sys_file]

            # Clear the API key/base URL from the env so the subscription (OAuth)
            # is used; make cwd a neutral directory so the project CLAUDE.md/settings
            # are not loaded.
            env = os.environ.copy()
            for var in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
            ):
                env.pop(var, None)

            cli_ceiling = float(os.environ.get("NAUTILUS_CLI_TIMEOUT", "300"))
            # No caller-supplied timeout → use the CLI's own ceiling outright
            # rather than blending in an unrelated default; a caller that DID
            # pass one still can't exceed the hard ceiling.
            deadline_s = (
                cli_ceiling if timeout is None else min(float(timeout), cli_ceiling)
            )
            if not callable(getattr(_LLM_CONTROL, "cancel_check", None)):
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=tempfile.gettempdir(),
                    timeout=deadline_s,
                    creationflags=NO_WINDOW_FLAGS,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=tempfile.gettempdir(),
                    creationflags=NO_WINDOW_FLAGS,
                )
                first = True

                def _cli_step(wait_s):
                    nonlocal first
                    try:
                        result = proc.communicate(
                            input=prompt if first else None, timeout=wait_s
                        )
                    except subprocess.TimeoutExpired:
                        first = False
                        return None
                    return result

                try:
                    stdout, stderr = _poll_until_deadline(
                        deadline_s,
                        0.25,
                        _cli_step,
                        f"claude CLI exceeded {deadline_s:g}s LLM call timeout",
                    )
                except (LLMCallCancelled, TimeoutError):
                    proc.kill()
                    proc.communicate()
                    raise
                proc.stdout = stdout
                proc.stderr = stderr
        finally:
            if sys_file:
                try:
                    os.unlink(sys_file)
                except OSError:
                    pass

        # The error body also comes back as JSON (even when exit≠0) and the real
        # cause is in its ``result``/``api_error_status`` fields — truncating the
        # raw stdout and embedding it into a string lost this signal.
        #
        # Envelope shape depends on the CLI version: historically one JSON
        # object; newer CLIs emit the whole event stream as one JSON ARRAY
        # (system/assistant/rate_limit_event/... events) whose terminal
        # ``type=="result"`` item is the old envelope. Normalize both — the
        # array shape crashed every LLM call here on 2026-08-01
        # ("'list' object has no attribute 'get'").
        envelope: dict[str, Any] = {}
        try:
            parsed = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            envelope = parsed
        elif isinstance(parsed, list):
            dicts = [x for x in parsed if isinstance(x, dict)]
            envelope = next(
                (x for x in reversed(dicts) if x.get("type") == "result"),
                dicts[-1] if dicts else {},
            )

        if proc.returncode != 0 or envelope.get("is_error"):
            message = str(envelope.get("result") or "")
            status = envelope.get("api_error_status")
            detail = message or (proc.stderr or proc.stdout or "").strip()
            raise _CLIError(
                f"claude CLI exited {proc.returncode}: {detail[:500]}",
                status=status if isinstance(status, int) else None,
                message=message,
            )
        if envelope.get("subtype") != "success":
            # If envelope is empty (stdout not JSON) show the raw output, not "None".
            detail = str(envelope.get("result") or (proc.stdout or "").strip())
            raise _CLIError(
                f"claude CLI error ({envelope.get('subtype')}): {detail[:500]}"
            )
        _record_cli_side_models(envelope, model)
        return _CLIResponse(
            envelope.get("result") or "",
            envelope.get("usage") or {},
            cost_usd=envelope.get("total_cost_usd"),
        )


def _record_cli_side_models(envelope: dict, called_model: str) -> None:
    """Ledger the CLI's INTERNAL side-model calls from ``modelUsage``.

    Each `claude -p` run makes small helper calls on other models (e.g. a
    Haiku topic/safety pass, ~$0.0006/çağrı). The envelope's top-level
    ``usage`` covers only the main model, so without this the ledger silently
    drops them. The main model's own entry is skipped — ``_create_message``
    already records it from ``resp.usage``. Best-effort: never raises.
    """
    try:
        import token_ledger

        for name, u in (envelope.get("modelUsage") or {}).items():
            if not isinstance(u, dict):
                continue
            canon = str(u.get("canonicalModel") or name)
            if name.startswith(called_model) or canon.startswith(called_model):
                continue  # main call — recorded via resp.usage, don't double-count
            token_ledger.record(
                canon,
                {
                    "input_tokens": u.get("inputTokens", 0),
                    "output_tokens": u.get("outputTokens", 0),
                    "cache_read_input_tokens": u.get("cacheReadInputTokens", 0),
                    "cache_creation_input_tokens": u.get("cacheCreationInputTokens", 0),
                },
                "cli_internal",
            )
    except Exception:
        pass


class _ClaudeCLIClient:
    def __init__(self, cli_path: str) -> None:
        self.messages = _ClaudeCLIMessages(cli_path)


class _ORTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ORUsage:
    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        self.input_tokens = int(prompt_tokens or 0)
        self.output_tokens = int(completion_tokens or 0)
        self.cost_usd = float(cost_usd) if cost_usd is not None else None
        # Anthropic yüzeyiyle uyum için mevcut alanlar da sağlanır.
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _ORResponse:
    def __init__(
        self,
        text: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        stop_reason: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.content = [_ORTextBlock(text)]
        self.usage = _ORUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
        # Anthropic adlandırmasına çevrilir ("length" → "max_tokens"), böylece
        # _was_truncated tek bir alan okur; bkz. _create_message.
        self.stop_reason = stop_reason


class _OpenRouterProcessError(RuntimeError):
    """Serializable OpenRouter child-process failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _openrouter_usage_payload(usage: Any) -> dict[str, int | float | None]:
    raw = {}
    if usage is not None and hasattr(usage, "model_dump"):
        try:
            raw = usage.model_dump() or {}
        except Exception:
            raw = {}
    cost = raw.get("cost")
    if cost is None:
        cost = getattr(usage, "cost", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cost_usd": float(cost) if cost is not None else None,
    }


def _openrouter_process_main(conn, config: dict, request: dict) -> None:
    """Execute one provider call in an OS process the AUTO worker can kill."""

    try:
        from openai import OpenAI

        timeout = float(config.get("timeout") or 120.0)
        client = OpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            timeout=timeout,
            max_retries=0,
        )
        req = dict(request)
        req.pop("timeout", None)
        resp = client.chat.completions.create(**req)
        usage = _openrouter_usage_payload(getattr(resp, "usage", None))
        conn.send(
            {
                "ok": True,
                "text": (
                    (resp.choices[0].message.content or "") if resp.choices else ""
                ),
                "finish_reason": (
                    getattr(resp.choices[0], "finish_reason", None)
                    if resp.choices
                    else None
                ),
                **usage,
            }
        )
    except BaseException as exc:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        try:
            conn.send(
                {
                    "ok": False,
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "status_code": status,
                }
            )
        except Exception:
            pass
    finally:
        conn.close()


def _stop_provider_process(proc) -> None:
    if proc.is_alive():
        try:
            proc.kill()
        except AttributeError:
            proc.terminate()
    proc.join(timeout=2.0)


def _run_openrouter_killable(request: dict, config: dict, timeout: float) -> dict:
    """Run a synchronous SDK request behind a hard deadline and kill switch."""

    _check_llm_cancelled()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_openrouter_process_main,
        args=(child, {**config, "timeout": timeout}, request),
        daemon=True,
    )
    proc.start()
    child.close()

    def _or_step(wait_s):
        if parent.poll(wait_s):
            payload = parent.recv()
            if not payload.get("ok"):
                raise _OpenRouterProcessError(
                    f"{payload.get('type', 'OpenRouterError')}: "
                    f"{payload.get('message', 'provider call failed')}",
                    status_code=payload.get("status_code"),
                )
            return payload
        if not proc.is_alive():
            raise _OpenRouterProcessError(
                f"OpenRouter worker exited without a response (exit={proc.exitcode})"
            )
        return None

    try:
        return _poll_until_deadline(
            max(0.1, float(timeout)),
            0.1,
            _or_step,
            f"OpenRouter call exceeded {float(timeout):g}s hard deadline",
        )
    finally:
        _stop_provider_process(proc)
        parent.close()


class _OpenRouterMessages:
    def __init__(
        self,
        client: Any,
        extra_headers: dict[str, str] | None = None,
        process_config: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._extra_headers = extra_headers or {}
        self._process_config = process_config

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 0,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> _ORResponse:
        or_messages: list[dict[str, str]] = []
        if system:
            or_messages.append({"role": "system", "content": system})
        for m in messages:
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            if not isinstance(content, str):
                continue
            if role not in ("user", "assistant", "system"):
                role = "user"
            or_messages.append({"role": role, "content": content})

        req: dict[str, Any] = {
            "model": model,
            "messages": or_messages,
        }
        if max_tokens and max_tokens > 0:
            req["max_tokens"] = int(max_tokens)
        if timeout is not None:
            req["timeout"] = float(timeout)
        # OpenRouter'ın karşılığı `reasoning.effort` ve sözlüğü daha dar
        # (low/medium/high); Claude CLI'a özgü xhigh/max en yakın üst seviyeye
        # düşer. OpenAI SDK'sı bilinmeyen üst-düzey anahtarı reddeder, bu yüzden
        # extra_body ile gider. Akıl yürütmeyen uçlarda OpenRouter alanı yok
        # sayar — o durumda çağrı bugünküyle aynı kalır, hata vermez.
        effort = current_effort()
        if effort:
            or_effort = "high" if effort in ("xhigh", "max") else effort
            req["extra_body"] = {"reasoning": {"effort": or_effort}}
        if self._extra_headers:
            req["extra_headers"] = self._extra_headers

        if self._process_config is not None and callable(
            getattr(_LLM_CONTROL, "cancel_check", None)
        ):
            payload = _run_openrouter_killable(
                req, self._process_config, float(timeout or 120.0)
            )
            text = str(payload.get("text") or "")
            usage_payload = payload
            finish = payload.get("finish_reason")
        else:
            resp = self._client.chat.completions.create(**req)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            usage_payload = _openrouter_usage_payload(getattr(resp, "usage", None))
            finish = (
                getattr(resp.choices[0], "finish_reason", None)
                if resp.choices
                else None
            )
        return _ORResponse(
            text=text,
            prompt_tokens=usage_payload.get("prompt_tokens", 0),
            completion_tokens=usage_payload.get("completion_tokens", 0),
            stop_reason="max_tokens" if finish == "length" else finish,
            cost_usd=usage_payload.get("cost_usd"),
        )


class _OpenRouterClient:
    def __init__(
        self,
        client: Any,
        extra_headers: dict[str, str] | None = None,
        process_config: dict[str, str] | None = None,
    ) -> None:
        self.messages = _OpenRouterMessages(
            client,
            extra_headers=extra_headers,
            process_config=process_config,
        )


def _find_claude_cli() -> str | None:
    override = os.environ.get("NAUTILUS_CLAUDE_CLI", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("claude")


def _build_openrouter_client() -> _OpenRouterClient:
    """OpenRouter istemcisi — OPENROUTER_API_KEY gerekli.

    Hem tüm-uygulama backend'i (NAUTILUS_LLM_BACKEND=openrouter) hem de
    model seçicinin koşu-başına "or:<id>" yolu bu kurucuyu paylaşır.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OpenRouter model seçildi ama OPENROUTER_API_KEY ayarlı değil."
        )

    # openai paketi sadece bu backend kullanılınca gerekir.
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError(
            "OpenRouter backend requires the `openai` package. Install with: pip install openai"
        ) from e

    base_url = os.environ.get(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).strip()
    extra_headers: dict[str, str] = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    if referer:
        extra_headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_X_TITLE", "").strip()
    if title:
        extra_headers["X-Title"] = title

    timeout = float(os.environ.get("NAUTILUS_LLM_CALL_TIMEOUT", "120"))
    # Default matches the OpenAI SDK's own default (2): the synchronous
    # fallback branch in _OpenRouterMessages.create (used by any non-AUTO
    # caller — chat_edit_block, propose_custom_block outside an AUTO run) has
    # no other retry/backoff wrapping it, so zeroing this out by default would
    # turn a transient connection blip or 502/503 into a user-visible failure
    # that the SDK used to absorb silently.
    max_retries = int(os.environ.get("NAUTILUS_OPENROUTER_SDK_RETRIES", "2"))
    logging.info("LLM backend: openrouter (%s)", base_url)
    return _OpenRouterClient(
        OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max(0, max_retries),
        ),
        extra_headers=extra_headers,
        process_config={"base_url": base_url, "api_key": api_key},
    )


_or_client: _OpenRouterClient | None = None
_or_client_lock = threading.Lock()


def _get_openrouter_client() -> _OpenRouterClient:
    """Thread-safe OpenRouter singleton — "or:" model pinlerinin arka ucu."""
    global _or_client
    if _or_client is None:
        with _or_client_lock:
            if _or_client is None:
                _or_client = _build_openrouter_client()
    return _or_client


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


SYSTEM_PROMPT = f"""You are a quantitative trading research agent.

You propose numeric hyperparameters for one of the following pre-implemented strategies. You never write code. You only choose a strategy name and a JSON object of parameters.

Available strategies and parameter specs:
{json.dumps(STRATEGY_PARAM_SPEC, indent=2)}

Constraints:
- For ma_crossover: slow > fast.
- Values must lie within the given ranges.
- Try different parameters and strategies over time — do not repeat past proposals verbatim.
- Use the history of past iterations (win rates, PnL, drawdown) to guide your next proposal.

Return ONLY a JSON object with keys "strategy", "params", "rationale". Nothing else. No markdown, no code fences."""


def _summarize_history(history: list[Any]) -> str:
    if not history:
        return "No prior iterations."
    lines = []
    for r in history[-10:]:
        m = r.metrics if r.error is None else {}
        lines.append(
            f"- id={r.id} strat={r.strategy} params={r.params} "
            f"pnl={m.get('pnl', 'n/a')} sharpe={m.get('sharpe', 'n/a')} "
            f"trades={m.get('n_trades', 'n/a')} err={r.error}"
        )
    return "\n".join(lines)


def _fallback_proposal() -> dict:
    strat = random.choice(list(STRATEGY_PARAM_SPEC.keys()))
    if strat == "ma_crossover":
        fast = random.randint(5, 20)
        slow = random.randint(fast + 5, min(200, fast + 60))
        return {
            "strategy": strat,
            "params": {"fast": fast, "slow": slow},
            "rationale": "fallback random (agent unavailable)",
        }
    else:
        return {
            "strategy": "rsi_mean_reversion",
            "params": {
                "rsi_period": random.randint(7, 21),
                "oversold": round(random.uniform(20.0, 35.0), 1),
                "overbought": round(random.uniform(65.0, 80.0), 1),
            },
            "rationale": "fallback random (agent unavailable)",
        }


def propose_strategy(history: list[Any]) -> dict:
    try:
        client = _get_client()
    except Exception as e:
        logging.warning("propose_strategy: client setup failed: %s", e, exc_info=True)
        fb = _fallback_proposal()
        fb["rationale"] = f"fallback ({type(e).__name__})"
        _tag_degraded(fb, e)
        return fb

    user_msg = f"""Past iterations:
{_summarize_history(history)}

Propose the next strategy + parameters as JSON."""

    try:
        resp = _create_message(
            client,
            max_tokens=MAX_TOKENS_IDEA,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()

        data = json.loads(_extract_json_object(text))
        if "strategy" not in data or "params" not in data:
            raise ValueError("missing keys")
        if data["strategy"] not in STRATEGY_PARAM_SPEC:
            raise ValueError(f"unknown strategy: {data['strategy']}")
        data.setdefault("rationale", "")
        return data
    except Exception as e:
        logging.warning("propose_strategy error: %s", e, exc_info=True)
        fb = _fallback_proposal()
        fb["rationale"] = f"fallback ({type(e).__name__})"
        fb["degraded"] = type(e).__name__
        return fb


# Default market context — if no market parameter is given, the Bybit crypto
# expression is preserved byte-for-byte (existing behavior unchanged).
_DEFAULT_MARKET_CONTEXT = (
    "crypto trading strategies on Bybit (BTCUSDT USDT perp, 1-minute bars)"
)

COMPOSED_SYSTEM_PROMPT = """You are a quantitative trading research agent designing {market_context}.

You must return a JSON object describing a complete "composed strategy" for the Nautilus backtest engine:

{
  "name": "short human-readable name (2-4 words, English or Turkish)",
  "description": "one-sentence description of the trading thesis",
  "blocks": [
    {"type": "<block_type>", "role": "entry" | "exit", "params": { ... }},
    ...
  ],
  "strategy_options": {
    "entry_logic": "OR" | "AND",
    "exit_logic": "OR" | "AND",
    "order_type": "market" | "limit",
    "limit_offset_bps": <float 0..50>,
    "use_bracket": true | false,
    "sl_type": "percent" | "atr",
    "sl_value": <float>,
    "tp_type": "percent" | "atr" | "off",
    "tp_value": <float>,
    "atr_period": <int 5..100>,
    "allow_short": true | false,
    "trade_size_mode": "fixed" | "fixed_usdt" | "percent_equity" | "atr_target",
    "trade_size_percent": <float 0.5..25>,
    "trade_size_atr_risk": <float 0.1..5>
  }
}

RULES:
- The list must contain AT LEAST one entry block and AT LEAST one exit block.
- 2 to 4 blocks total is usually best.
- entry_logic="OR" fires on any entry-block hit; "AND" requires ALL entry blocks to fire the same bar (strict, fewer trades).
- ⚠️ CRITICAL: With 2 or more entry blocks, ALWAYS use entry_logic="OR". Using "AND" across 3 blocks fires on less than 0.5% of bars and almost always produces ZERO trades — the strategy is useless. Only use "AND" with exactly 2 blocks when you have a specific confluence reason. Default is "OR".
- exit_logic works the same.
- order_type="limit" adds a small offset via limit_offset_bps (0-50 bps typical).
- use_bracket=true attaches an atomic SL (+ optional TP) with the entry (submitted as a Nautilus OrderList). sl_type/tp_type "percent" uses % of entry price; "atr" multiplies ATR by sl_value/tp_value.
- allow_short=true lets entry blocks with direction=down (or cross=above / sign=negative / side=upper) open SHORT positions via SELL. The backend switches to MARGIN account when this is enabled.
- trade_size_mode="fixed" uses the blocks-level trade_size (BTC). "percent_equity" sizes as %equity/price. "atr_target" targets a fixed % risk per trade using ATR distance.
- Use ONLY the block types and parameter names defined in the catalog below.
- Every param must be within its declared range. Enum params must use one of the given options.
- For ma_cross / ema_cross / macd_cross: slow > fast strictly.
- atr_stop is EXIT-only.
- Prefer diverse ideas: do NOT copy an existing catalog strategy verbatim; look at history and try something with a plausibly different behavior (different lookbacks, different combinations, mix indicator + non-indicator blocks, try shorts + bracket occasionally).
- IMPORTANT: If recent history shows mostly EMA/RSI-based strategies, deliberately choose a DIFFERENT indicator family this time: Bollinger Bands, Price Breakout, Momentum, MACD, or ATR-based approaches. Rotate through all available block types across iterations.
- Return ONLY the JSON. No markdown, no code fences, no explanation.

BLOCK CATALOG:
{catalog}

Sensible defaults if you have no strong reason to override: entry_logic="OR" (STRONGLY PREFERRED — AND only with exactly 2 blocks and a deliberate confluence reason), exit_logic="OR", order_type="market", use_bracket=false, allow_short=false, trade_size_mode="fixed".
"""


# Katalog özeti TTL memo'su — MALİYET düzeltmesi (2026-08-02): bu özet
# composed-proposer'ın SYSTEM prompt'una gömülür; AUTO loop her iterasyonda
# yeni agnt_* bloğu kaydettiğinden özet her çağrıda değişiyor ve ~30K'lık
# prompt cache'e her seferinde YENİDEN YAZILIYORDU (263 çağrı × ~30K = gecelik
# yazımların %80'i, cache_read=0). 30 dk'lık dondurma prefix'i bayt-sabit
# yapar → yazımlar okumaya döner (×2 → ×0.1). Yeni blokların özete ~30 dk
# gecikmeyle girmesi davranışsal olarak önemsizdir (fikir yolu zaten taze).
_CATALOG_SUMMARY_TTL_S = 1800.0
_catalog_summary_cache: tuple[float, str] | None = None
_catalog_summary_lock = threading.Lock()


def _catalog_summary(*, force: bool = False) -> str:
    global _catalog_summary_cache
    now = time.monotonic()
    with _catalog_summary_lock:
        if (
            not force
            and _catalog_summary_cache is not None
            and now - _catalog_summary_cache[0] < _CATALOG_SUMMARY_TTL_S
        ):
            return _catalog_summary_cache[1]

    from composer import BLOCK_CATALOG

    out = []
    generated = []
    for k, meta in BLOCK_CATALOG.items():
        # Skip lab-generated temporary blocks — they are EMA/RSI variants
        # that Claude itself produced; showing them biases proposals toward
        # the same family of indicators.
        if k.startswith("lab_entry_") or k.startswith("lab_exit_"):
            continue
        # Agent-generated blocks: ONE compact line each (params come from
        # registered defaults; the validator clamps). Full specs for hundreds
        # of self-produced variants both bloated the prompt (~100 token/blok)
        # and biased proposals toward Claude's own past output.
        if k.startswith("agnt_"):
            generated.append(f"- {k}: {meta['label']}")
            continue
        params = []
        for pname, pspec in meta["params"].items():
            if pspec["type"] == "enum":
                params.append(
                    f"{pname}: enum {pspec['options']} (default {pspec['default']})"
                )
            else:
                params.append(
                    f"{pname}: {pspec['type']} [{pspec['min']}..{pspec['max']}] (default {pspec['default']})"
                )
        out.append(f"- {k} ({meta['label']}): {'; '.join(params)}")

    generated.sort()  # deterministik sıra — prefix kararlılığının parçası
    if len(generated) > 80:
        extra = len(generated) - 80
        generated = generated[:80] + [
            f"  (+{extra} more generated blocks omitted — defaults apply)"
        ]
    if generated:
        out.append("Generated blocks (params use registered defaults):")
        out.extend(generated)
    text = "\n".join(out)
    with _catalog_summary_lock:
        _catalog_summary_cache = (now, text)
    return text


def _summarize_composed_history(history: list[Any], catalog: list[Any]) -> str:
    lines = []
    if catalog:
        lines.append("EXISTING SAVED STRATEGIES (avoid duplicating these):")
        for s in catalog[-10:]:
            block_desc = ", ".join(
                f"{b.type}/{b.role}({','.join(f'{k}={v}' for k, v in b.params.items())})"
                for b in s.blocks
            )
            lines.append(f"  · {s.name}: [{block_desc}]")
    if history:
        lines.append("\nRECENT BACKTEST RESULTS (learn from these):")
        for r in history[-8:]:
            m = (r.metrics or {}) if r.error is None else {}
            bi = getattr(r, "bars_info", None) or {}
            # The three fields below were missing and each cost the loop a
            # measurable mistake (audit 2026-08-04, run 1376c812):
            #   tf   — the timeframe is assigned by round-robin AFTER the spec is
            #          written, so a proposal tuned for minutes was scored on
            #          daily bars (one iteration produced a single trade). Without
            #          it in the feedback the model cannot even see the mismatch.
            #   dd   — users ask for "minimum drawdown" and the model was never
            #          shown drawdown; it cannot optimize what it cannot see.
            #   comm — one iteration paid 8,566 USD of commission on 52 USD of
            #          gross profit. Net PnL alone reads as "bad strategy"; with
            #          the commission line it reads as "too many trades".
            tf = bi.get("interval") or bi.get("granularity") or "?"
            lines.append(
                f"  · {r.strategy} tf={tf} pnl={m.get('pnl', 'n/a')} "
                f"sharpe={m.get('sharpe', 'n/a')} "
                f"max_dd={m.get('max_dd', 'n/a')} "
                f"commission={m.get('commission_total', 'n/a')} "
                f"trades={m.get('n_trades', 'n/a')} winrate={m.get('win_rate', 'n/a')} err={r.error}"
            )
        lines.append(
            "  → Read these together: `pnl` is NET of `commission`. If commission "
            "is a large share of the gross result the strategy trades too often "
            "for its timeframe — widen the entry filter or move to a slower tf. "
            "`max_dd` is a fraction and negative (-0.26 = 26% drawdown); a high "
            "pnl with a deep max_dd is not an improvement."
        )
        # Show which block types have already been tried so Claude avoids repeating them
        tried_blocks: set[str] = set()
        for r in history:
            # r.strategy = "composed:Name [block1+block2]" or "composed:Name"
            import re as _re

            m = _re.search(r"\[([^\]]+)\]", r.strategy)
            if m:
                for bt in m.group(1).split("+"):
                    tried_blocks.add(bt.strip())
        if tried_blocks:
            from composer import BLOCK_CATALOG as _BC

            all_blocks = list(_BC.keys())
            untried = [b for b in all_blocks if b not in tried_blocks]
        else:
            untried = []
        lines.append(
            f"\nBLOCK TYPES USED IN THIS SESSION (use DIFFERENT combinations):\n"
            f"  Already used: {', '.join(sorted(tried_blocks)) or 'none yet'}\n"
            f"  Not yet tried: {', '.join(sorted(untried)) or 'all tried'}\n"
            "  → Prefer block types from 'Not yet tried'. Vary the indicator family: "
            "if recent runs used EMA/RSI, try Bollinger, price_breakout, momentum, "
            "macd_cross, or atr combinations instead."
        )
    if not lines:
        return "No prior context — first strategy proposal."
    return "\n".join(lines)


def _fallback_composed() -> dict:
    from composer import BLOCK_CATALOG

    rng = _fallback_rng()

    # Exclude exit-only blocks (e.g. atr_stop) from entry selection to avoid
    # _validate_composed forcing role="exit" → zero entry blocks → ValueError.
    exit_only = {"atr_stop"}
    all_types = list(BLOCK_CATALOG.keys())
    entry_types = [t for t in all_types if t not in exit_only]
    entry_type = rng.choice(entry_types)
    exit_type = rng.choice([t for t in all_types if t != entry_type] or all_types)

    def _rand_params(btype: str) -> dict:
        p = {}
        for pname, pspec in BLOCK_CATALOG[btype]["params"].items():
            if pspec["type"] == "int":
                p[pname] = rng.randint(pspec["min"], pspec["max"])
            elif pspec["type"] == "float":
                p[pname] = round(rng.uniform(pspec["min"], pspec["max"]), 1)
            else:
                p[pname] = rng.choice(pspec["options"])
        return p

    def _fix_fast_slow(btype: str, params: dict) -> dict:
        """slow <= fast → swap to valid range for any crossover block."""
        if btype in ("ma_cross", "ema_cross", "macd_cross"):
            if params.get("slow", 0) <= params.get("fast", 0):
                params["fast"], params["slow"] = 10, 40
        return params

    e_params = _fix_fast_slow(entry_type, _rand_params(entry_type))
    x_params = _fix_fast_slow(exit_type, _rand_params(exit_type))

    result = {
        "name": f"Random {entry_type}/{exit_type}",
        "description": "Fallback random composition (Claude unavailable).",
        "blocks": [
            {"type": entry_type, "role": "entry", "params": e_params},
            {"type": exit_type, "role": "exit", "params": x_params},
        ],
        "strategy_options": dict(_STRATEGY_OPTION_DEFAULTS),
    }
    # Run _validate_composed — fix the role of exit-only blocks like atr_stop
    return _validate_composed(result)


_STRATEGY_OPTION_DEFAULTS: dict = {
    "entry_logic": "OR",
    "exit_logic": "OR",
    "order_type": "market",
    "limit_offset_bps": 0.0,
    "use_bracket": False,
    "sl_type": "percent",
    "sl_value": 2.0,
    "tp_type": "off",
    "tp_value": 4.0,
    "atr_period": 14,
    "allow_short": False,
    "trade_size_mode": "fixed",
    "trade_size_percent": 5.0,
    "trade_size_atr_risk": 1.0,
    "trade_size_usdt": 1000.0,
}


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return max(lo, min(hi, f))
    except (TypeError, ValueError):
        return default


def _validate_strategy_options(raw: dict) -> dict:
    """Clamp / default strategy_options into safe values."""
    if not isinstance(raw, dict):
        raw = {}
    opts = dict(_STRATEGY_OPTION_DEFAULTS)

    def pick_enum(key, options):
        v = raw.get(key, opts[key])
        return v if v in options else opts[key]

    opts["entry_logic"] = pick_enum("entry_logic", ["OR", "AND"])
    opts["exit_logic"] = pick_enum("exit_logic", ["OR", "AND"])
    opts["order_type"] = pick_enum("order_type", ["market", "limit"])
    opts["limit_offset_bps"] = _clamp(raw.get("limit_offset_bps", 0.0), 0.0, 100.0, 0.0)
    opts["use_bracket"] = bool(raw.get("use_bracket", False))
    opts["sl_type"] = pick_enum("sl_type", ["percent", "atr"])
    opts["sl_value"] = _clamp(raw.get("sl_value", 2.0), 0.1, 50.0, 2.0)
    opts["tp_type"] = pick_enum("tp_type", ["percent", "atr", "off"])
    opts["tp_value"] = _clamp(raw.get("tp_value", 4.0), 0.1, 100.0, 4.0)
    opts["atr_period"] = int(_clamp(raw.get("atr_period", 14), 5, 100, 14))
    opts["allow_short"] = bool(raw.get("allow_short", False))
    opts["trade_size_mode"] = pick_enum(
        "trade_size_mode", ["fixed", "fixed_usdt", "percent_equity", "atr_target"]
    )
    opts["trade_size_percent"] = _clamp(
        raw.get("trade_size_percent", 5.0), 0.1, 50.0, 5.0
    )
    opts["trade_size_atr_risk"] = _clamp(
        raw.get("trade_size_atr_risk", 1.0), 0.05, 20.0, 1.0
    )
    opts["trade_size_usdt"] = _clamp(
        raw.get("trade_size_usdt", 1000.0), 1.0, 10_000_000.0, 1000.0
    )
    return opts


def _validate_composed(data: dict) -> dict:
    """Clamp params to catalog ranges and drop invalid blocks; raise on hopeless."""
    from composer import BLOCK_CATALOG

    if not isinstance(data, dict) or "blocks" not in data:
        raise ValueError("missing 'blocks'")

    clean_blocks = []
    for b in data["blocks"]:
        btype = b.get("type")
        role = b.get("role")
        if btype not in BLOCK_CATALOG:
            continue
        if role not in ("entry", "exit"):
            continue
        meta = BLOCK_CATALOG[btype]
        declared_role = meta.get("role")
        if declared_role in ("entry", "exit") and role != declared_role:
            # Do not silently coerce a custom block into the opposite role.
            # Dropping it lets the existing missing-entry/missing-exit logic
            # reject or repair the proposal without changing signal semantics.
            continue
        params = {}
        for pname, pspec in meta["params"].items():
            raw = (b.get("params") or {}).get(pname, pspec["default"])
            try:
                if pspec["type"] == "int":
                    v = int(raw)
                    v = max(pspec["min"], min(pspec["max"], v))
                elif pspec["type"] == "float":
                    v = float(raw)
                    v = max(pspec["min"], min(pspec["max"], v))
                else:
                    v = raw if raw in pspec["options"] else pspec["default"]
            except (TypeError, ValueError):
                v = pspec["default"]
            params[pname] = v
        # Enforce cross fast<slow for cross-family blocks.
        if btype in ("ma_cross", "ema_cross", "macd_cross") and params.get(
            "slow", 0
        ) <= params.get("fast", 0):
            params["fast"], params["slow"] = 10, max(params.get("slow", 30), 30)
        # atr_stop is exit-only.
        if btype == "atr_stop" and role != "exit":
            role = "exit"
        clean_blocks.append({"type": btype, "role": role, "params": params})

    if not clean_blocks or not any(b["role"] == "entry" for b in clean_blocks):
        raise ValueError("proposal missing entry block after cleanup")

    # Add one of several exit options if no exit block exists (not always atr_stop)
    if not any(b["role"] == "exit" for b in clean_blocks):
        from composer import BLOCK_CATALOG

        # Choose a suitable exit based on the entry block
        entry_types = {b["type"] for b in clean_blocks if b["role"] == "entry"}
        # Preference order: block giving the opposite signal to entry → atr_stop last resort
        _exit_candidates = [
            "momentum",
            "rsi_threshold",
            "bollinger_break",
            "macd_cross",
            "atr_stop",
        ]
        # Exclude types already used
        _exit_candidates = [t for t in _exit_candidates if t not in entry_types]
        fallback_exit_type = _exit_candidates[0] if _exit_candidates else "atr_stop"
        exit_meta = BLOCK_CATALOG.get(fallback_exit_type, {}).get("params", {})
        clean_blocks.append(
            {
                "type": fallback_exit_type,
                "role": "exit",
                "params": {k: v["default"] for k, v in exit_meta.items()},
            }
        )

    opts = _validate_strategy_options(data.get("strategy_options") or {})

    return {
        "name": str(data.get("name") or "Claude Suggestion")[:60].strip(),
        "description": str(data.get("description") or "")[:300].strip(),
        "blocks": clean_blocks,
        "strategy_options": opts,
    }


_TF_ABBREV_TO_SPELLED = {
    "1m": "1-minute",
    "5m": "5-minute",
    "15m": "15-minute",
    "30m": "30-minute",
    "1h": "1-hour",
    "4h": "4-hour",
    "12h": "12-hour",
    "1d": "daily",
}


def _build_tf_human() -> dict[str, str]:
    """Bybit-code / external-DSL → human phrase for LLM prompts.

    Derived from data.py's canonical interval tables (BYBIT_ALL_INTERVALS,
    EXTERNAL_GRAN_BY_BYBIT_CODE) instead of a third hand-maintained copy of
    the same code set — data.py already has two (BYBIT_ALL_INTERVALS and
    web/mission.py's ``_tf_short``); a new interval added there is now picked
    up here automatically instead of silently degrading to the raw code in
    the LLM prompt.
    """
    from data import BYBIT_ALL_INTERVALS, EXTERNAL_GRAN_BY_BYBIT_CODE

    out: dict[str, str] = {}
    for code, abbrev in BYBIT_ALL_INTERVALS:
        spelled = _TF_ABBREV_TO_SPELLED.get(abbrev, abbrev)
        out[code] = spelled
        dsl = EXTERNAL_GRAN_BY_BYBIT_CODE.get(code)
        if dsl:
            out[dsl] = spelled
    # 30m has no external-catalog counterpart (EXTERNAL_GRAN_BY_BYBIT_CODE
    # excludes it) but the DSL spelling is kept for callers that still pass it.
    out.setdefault("30-MINUTE", _TF_ABBREV_TO_SPELLED["30m"])
    return out


_TF_HUMAN = _build_tf_human()


def _timeframe_line(timeframe: str) -> str:
    """Prompt line naming the bar this spec will be scored on.

    The AUTO loop picks the timeframe round-robin and it used to do so AFTER the
    spec came back, so the model wrote indicator periods blind. On daily bars a
    minute-scale filter fires almost never (audit run 1376c812: a daily
    iteration opened exactly ONE trade and was disqualified for having <20 —
    the idea was never tested, only the mismatch was).
    """
    tf = (timeframe or "").strip()
    if not tf:
        return ""
    human = _TF_HUMAN.get(tf, tf)
    return (
        f"\nTARGET TIMEFRAME: this strategy will be backtested on {human} bars. "
        "Size every indicator period, threshold and stop for that bar — a filter "
        "tuned for minutes fires almost never on daily bars, and one tuned for "
        "days fires on every minute bar and pays commission on each."
    )


def propose_composed_strategy(
    history: list[Any],
    catalog: list[Any],
    hint: str = "",
    web_research: bool = False,
    market: str | None = None,
    timeframe: str = "",
) -> tuple[dict, dict | None]:
    """Ask Claude to design a full composed strategy.
    Returns (strategy_dict, usage_dict | None).
    usage_dict has keys: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens.

    ``market`` — optional market context (e.g. "US equity QQQ.NASDAQ (1-DAY
    bars, USD cash account)"). If None the Bybit BTCUSDT expression is preserved as-is.

    ``timeframe`` — the bar interval this spec will ACTUALLY be scored on. The
    AUTO loop assigns timeframes round-robin, so the caller knows the target
    before it asks for the spec; passing it lets the model size periods and
    thresholds for that bar. Empty = don't mention it (prompt unchanged).
    """
    try:
        client = _get_client()
    except Exception as e:
        logging.warning(
            "propose_composed_strategy: client setup failed: %s", e, exc_info=True
        )
        fb = _fallback_composed()
        fb["description"] = (
            fb.get("description", "") + f" · fallback ({type(e).__name__})"
        ).strip()
        _tag_degraded(fb, e)
        return fb, None

    market_context = (
        f"trading strategies for {market} — a US stock from a historical Nautilus "
        "data catalog (2003→present). The account is a long-only USD CASH account: "
        "prefer allow_short=false, and trade_size is in whole SHARES (integer >= 1)"
        if market
        else _DEFAULT_MARKET_CONTEXT
    )
    system = COMPOSED_SYSTEM_PROMPT.replace("{market_context}", market_context).replace(
        "{catalog}", _catalog_summary()
    )
    hint_line = (
        f"\nUser hint (incorporate this into the strategy concept): {hint.strip()}"
        if hint.strip()
        else ""
    )

    web_section = ""
    if web_research:
        web_text = web_research_strategies(hint, market=market)
        if web_text:
            web_section = f"\n\n{web_text}"

    market_target = market or "BTCUSDT Bybit"
    tf_line = _timeframe_line(timeframe)
    user = f"""Context:
{_summarize_composed_history(history, catalog)}{hint_line}{tf_line}{web_section}

Design a new {market_target} composed strategy as specified. Return JSON only."""

    try:
        resp = _create_message(
            client,
            _purpose="composed",
            max_tokens=MAX_TOKENS_COMPOSED,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        data = json.loads(_extract_json_object(text))
        return _validate_composed(data), usage
    except Exception as e:
        _raise_if_llm_control_abort(e)
        fb = _fallback_composed()
        fb["description"] = (
            fb.get("description", "") + f" · fallback ({type(e).__name__})"
        ).strip()
        # Makine-okunur bozulma işareti. Açıklamanın kuyruğundaki metni ayrıştırmak
        # kırılgan; koşu bu alanı sayar ve fazı "degraded" kapatır — fallback bir
        # koşuyu başarı gibi göstermesin.
        _tag_degraded(fb, e)
        return fb, None


# ============================================================================


# Nodes explicitly ALLOWED. Anything else is rejected.
# AST security gate. The validator + whitelists live in codegate.py so the
# on-disk load path (composer) can re-validate stored blocks WITHOUT importing
# this heavy (anthropic) module. Imported here for the generation path.
from codegate import (  # noqa: E402
    _ALLOWED_BUILTINS,
    GeneratedCodeError,
)
from codegate import safe_builtins as _safe_builtins  # noqa: E402
from codegate import (
    validate_generated_code as _validate_generated_code,
)


def _find_evaluate_def(tree):
    """Find the top-level ``def evaluate(...)`` in a parsed module, if any.

    Shared by the AST role-contract checks below, which each independently
    searched for this same node with different empty-case handling (fallback
    to the whole tree vs. ``None``) — a predicate duplicated with different
    syntax makes it easy for the two checks to diverge over time even though
    they're meant to enforce the same rule.
    """
    import ast

    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "evaluate"
        ),
        None,
    )


def _test_execute_generated(
    src: str,
    meta: dict | None = None,
    require_max_lookback: bool = False,
    role_hint: str = "entry",
) -> None:
    """Compile + execute the module in an isolated namespace, then invoke
    evaluate() once with harmless inputs to catch runtime errors (NameError,
    KeyError on missing param, etc.). Raises GeneratedCodeError on failure.

    `meta` — if provided, `block.params` is pre-populated with declared defaults
    so the smoke call matches real runtime shape.

    `require_max_lookback` — M16: for NEWLY generated blocks the max_lookback
    export is required (if not declared, the window was clipped to 50+5 and
    long-period indicators were silently computed wrong). False for old disk
    blocks (backward compatibility).

    Environment parity (H8): the injection set here (math/statistics/ind) and
    the loop-budgeted compilation are IDENTICAL to composer._load_module_from_path —
    a block that passes smoke finds the same environment at runtime.
    """
    # A single smoke input cannot exercise every conditional branch.  The live
    # AUTO review found exit blocks whose smoke returned None while a later
    # branch returned "long"/"short".  Validate every literal return in the
    # syntax tree before execution so dead/rare branches cannot evade the role
    # contract.  Dynamic expressions remain covered by the runtime check below.
    import ast

    try:
        tree = ast.parse(src, filename="<custom_block>")
    except SyntaxError as exc:
        raise GeneratedCodeError(f"invalid Python syntax: {exc}") from exc
    allowed_literals = {"long", "short"} if role_hint == "entry" else {"exit"}
    evaluate_def = _find_evaluate_def(tree)
    for node in ast.walk(evaluate_def if evaluate_def is not None else tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if not isinstance(node.value, ast.Constant):
            raise GeneratedCodeError(
                f"evaluate() role return at line {getattr(node, 'lineno', '?')} "
                "must be a literal signal or None; dynamic returns are not auditable"
            )
        value = node.value.value
        if value is not None and value not in allowed_literals:
            expected = "'long'/'short'/None" if role_hint == "entry" else "'exit'/None"
            raise GeneratedCodeError(
                f"evaluate() violated the {role_hint} role contract at line "
                f"{getattr(node, 'lineno', '?')}: returned {value!r}; expected {expected}"
            )

    import math as _math
    import statistics as _stats

    import indicators as _ind_mod
    from codegate import compile_with_loop_budget, safe_module_proxy

    safe_globals = {
        # Single source of truth shared with composer._load_module_from_path so
        # smoke and runtime resolve the SAME restricted builtins (incl. the
        # RuntimeError the injected loop-budget guard raises).
        "__builtins__": _safe_builtins(),
        # Read-only proxies, not the live modules: this exec runs inside the web
        # server process, so a write through an injected module would poison every
        # later strategy in the process.
        "math": safe_module_proxy(_math, "math"),
        "statistics": safe_module_proxy(_stats, "statistics"),
        "ind": safe_module_proxy(_ind_mod, "ind"),
    }
    ns: dict = {}
    try:
        # M25: loop-budgeted compilation — `while True: pass` now raises a
        # RuntimeError on budget overrun instead of hitting the 2s thread
        # timeout and LEAKING a daemon thread (this closes the L4 core-leak).
        exec(compile_with_loop_budget(src, "<custom_block>"), safe_globals, ns)
    except Exception as e:
        raise GeneratedCodeError(f"module init failed: {type(e).__name__}: {e}") from e

    if require_max_lookback and not callable(ns.get("max_lookback")):
        raise GeneratedCodeError(
            "max_lookback(params) function is required (M16): it must return the "
            "number of bars the block needs, otherwise the window is clipped to 55 bars"
        )

    # Make helpers defined in the module visible when evaluate() runs.
    # Python exec with separate globals/locals means name lookups inside
    # function bodies go through globals (safe_globals), not locals (ns).
    # Merge all callable names from ns into safe_globals so helpers resolve.
    # Guard: do NOT overwrite whitelisted builtins (e.g. str, int, list) —
    # a helper named "str" would shadow the builtin for the smoke call.
    _protected = set(_ALLOWED_BUILTINS) | {"math", "statistics", "ind", "__builtins__"}
    for k, v in ns.items():
        if callable(v) and not k.startswith("_") and k not in _protected:
            safe_globals[k] = v
    # M1084: the budget preamble (__budget/__budget_tick) lands in ns inside
    # exec's separate globals/locals; the injected functions look these up in
    # GLOBALS — move them so they are reachable in smoke too (the loader uses a
    # single namespace, no problem there).
    for _bk in ("__budget", "__budget_tick"):
        if _bk in ns:
            safe_globals[_bk] = ns[_bk]

    ev = ns.get("evaluate")
    if not callable(ev):
        raise GeneratedCodeError("evaluate is not callable after exec")

    # Build defaults from meta.params, matching how the composer populates
    # block.params from BLOCK_CATALOG specs at add-block time.
    defaults: dict = {}
    if isinstance(meta, dict):
        for pname, pspec in (meta.get("params") or {}).items():
            if isinstance(pspec, dict) and "default" in pspec:
                defaults[pname] = pspec["default"]

    class _Block:
        def __init__(self, params):
            self.params = params
            self.role = role_hint
            self.type = "custom"

    class _Portfolio:
        def is_net_long(self, _):
            return False

        def is_net_short(self, _):
            return False

        def is_flat(self, _):
            return True

    # Give the block a decently long price series so most lookbacks don't
    # underrun and skip execution entirely.
    closes = [100.0 + i * 0.1 for i in range(300)]
    # Volume + high/low series are also provided via indicators just like at
    # runtime — blocks reading OHLC (ADX/ATR/Stochastic/Donchian) should really
    # run in smoke-exec (not fall into the None-guard). The high > close > low
    # ordering is preserved so True-Range etc. logic sees sensible values.
    indicators = {
        "volumes": [1000.0 + (i % 7) * 150.0 for i in range(300)],
        "highs": [100.0 + i * 0.1 + 0.5 for i in range(300)],
        "lows": [100.0 + i * 0.1 - 0.5 for i in range(300)],
    }

    # Run evaluate() in a daemon thread with a 2s timeout to guard against
    # infinite loops (e.g. `while True: pass`) that pass the AST whitelist.
    result_holder: list = []
    error_holder: list = []

    def _run():
        try:
            result_holder.append(
                ev({}, _Block(dict(defaults)), closes, indicators, _Portfolio())
            )
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=2.0)
    if t.is_alive():
        raise GeneratedCodeError(
            "evaluate() timed out after 2s (possible infinite loop)"
        )
    if error_holder:
        raise GeneratedCodeError(
            f"evaluate() raised on smoke input: {type(error_holder[0]).__name__}: {error_holder[0]}"
        ) from error_holder[0]
    out = result_holder[0] if result_holder else None

    allowed = {None, "long", "short"} if role_hint == "entry" else {None, "exit"}
    if out not in allowed:
        expected = "None/'long'/'short'" if role_hint == "entry" else "None/'exit'"
        raise GeneratedCodeError(
            f"evaluate() violated the {role_hint} role contract: returned {out!r}; "
            f"expected {expected}"
        )


CUSTOM_BLOCK_SYSTEM_PROMPT = """You are a Python code generator for a trading strategy composer.

The user describes a signal condition in natural language. You output a JSON
object describing a new "signal block" that plugs into an existing framework.

STRICT OUTPUT SCHEMA (return this and NOTHING else — no markdown, no fences):
{
  "name": "lowercase_snake_case (2-40 chars, letters/digits/underscore, starts with a letter)",
  "meta": {
    "label": "Human-readable Turkish or English label (max 40 chars)",
    "params": {
      "<param_name>": {"type": "int"|"float"|"enum", ...spec...},
      ...
    },
    "help": "One or two sentences explaining what the block does (Turkish OK)."
  },
  "code": "def evaluate(state, block, closes, indicators, portfolio):\\n    ...\\n"
}

param spec rules (mirror the built-ins):
- {"type": "int",   "min": <int>,   "max": <int>,   "default": <int>}
- {"type": "float", "min": <float>, "max": <float>, "default": <float>}
- {"type": "enum",  "options": ["a","b"], "default": "a"}

`code` rules:
- MUST define `evaluate(state, block, closes, indicators, portfolio)` — this is the main function.
- MAY also define helper functions at module level (e.g. `def ema_series(closes, period):`) BEFORE `evaluate`. Helper functions may call each other and be called from `evaluate`.
- `evaluate`:
- Return "long" or "short" for an entry-fire, "exit" for an exit-fire, or None.
- `closes` is a list of float bar closes (oldest first, newest last).
- `block.params` is a dict of the current param values (already coerced to declared types).
- `state` is a mutable dict — persistent across bars, scoped to this block. Use it for prev-value tracking.
- `indicators` is a dict with three aligned bar series (each a list[float], oldest-first, same length as `closes`): `indicators.get("volumes")`, `indicators.get("highs")`, `indicators.get("lows")`. No other keys — do NOT rely on anything else in it.
- `portfolio` exposes `is_net_long(...)`, `is_net_short(...)`, `is_flat(...)`. Do not import anything.

DATA AVAILABLE — FULL OHLCV:
- `closes` (bar closes) + `indicators.get("highs")` (bar highs) + `indicators.get("lows")` (bar lows) + `indicators.get("volumes")` (bar volumes). All four are aligned float lists, oldest-first. (Open is not exposed; use `closes[i-1]` as the prior close where an OHLC formula needs "previous close".) Always guard length: `hi = indicators.get("highs") or []; if len(hi) < n: return None`.
- Because highs/lows ARE available, indicators that need OHLC CAN be computed for real: **ATR**, **ADX / DMI**, **Stochastic**, **Donchian / Keltner channels**, **WaveTrend**, **SuperTrend** (ATR bands). Volume logic (volume spike, OBV, volume-weighted momentum) is also supported. Builtin blocks already cover RSI, EMA/MACD cross, Bollinger, ATR-stop, volume_spike, ADX (adx_threshold), StochRSI (stoch_rsi_cross), WaveTrend (wave_trend_cross), Donchian (donchian_channel) — write a custom block only for something NOT in that set.
- ⭐ INDICATOR LIBRARY `ind` (M27/M33 — USE IT, do NOT hand-roll the math): a vetted NAU-parity library is pre-available as `ind` (no import needed). Prefer these over reimplementing formulas — hand-rolled indicator math drifts and breaks parity:
  * `ind.calc_rsi(closes, period)` → float; `ind.calc_rsi_series(closes, period)` → list
  * `ind.sma(values, period)` / `ind.ema(values, period)` → list (aligned tails)
  * `ind.calc_atr(highs, lows, closes, period)` → float | None
  * `ind.calc_adx(highs, lows, closes, period)` → {"adx", "plusDI", "minusDI"} | None
  * `ind.calc_stoch_rsi(closes, rsi_period, stoch_period)` → {"k", "d"}
  * `ind.calc_wave_trend(highs, lows, closes, channel_len, avg_len)` → {"wt1", "wt2"} | None
  * `ind.calc_volume_change(volumes, lookback)` → float
  Example: `adx = ind.calc_adx(indicators.get("highs") or [], indicators.get("lows") or [], closes, 14); if adx is None or adx.get("adx", 0) < 20: return None`. Only hand-roll math for exotic indicators NOT in this list.
- MULTI-indicator confluence IS allowed. If the user asks for "RSI AND ADX AND ATR" (or any multi-indicator combo), you MAY AND all of them together — implement the full confluence the user requested, don't collapse it. The ONLY cost is signal frequency (see below): each extra AND cuts firing, so you MUST compensate with very loose thresholds so the combined block still fires enough. When the user did NOT ask for a confluence, keep it simple (one condition).

⚠️ SIGNAL FREQUENCY — the real constraint (not a condition-count cap):
A block that almost never fires produces too few trades and gets filtered out downstream (runs with fewer than ~20 trades are discarded). So there is no hard limit on the NUMBER of AND conditions — but every AND you add sharply cuts how often the block fires, and you must offset that with LOOSE thresholds. A GOOD entry block fires ~100-500 times on 50,000 bars (0.2-1%).

RULES FOR ADEQUATE SIGNAL FREQUENCY:
- AND as many conditions as the user's confluence requires, but LOOSEN each threshold hard so the combination still fires (e.g. a 4-way confluence needs each gate wide open). Err toward too-loose, never too-strict — a noisy block still beats a 0-trade one.
- PREFER simple threshold or crossover per condition: "if rsi < 40, fire" or "if ema5 crosses ema20, fire"
- AVOID multi-stage state machines like: "was_below AND now_above AND momentum > 0" — this fires too rarely
- For crossover detection: ONE state variable is enough. Store prev value, compare to current.
- Default parameter values MUST produce frequent signals. Use LOOSE thresholds:
  * RSI: oversold threshold default=40 (not 25), overbought default=60 (not 75)
  * Std deviations for Bollinger-style: default=1.5 (not 2.0 or higher)
  * Lookback periods: default=10-14 (not 20+)
  * Momentum bars: default=3 (not 5+)
- Provide wide param ranges so the backtest engine can optimize: e.g. RSI period min=5, max=30

SIGNAL FREQUENCY SELF-CHECK (mental simulation):
Before writing the code, ask: "On 1000 consecutive bars of BTC price data, how many times does this fire?"
- If answer < 5: your thresholds are too strict. Loosen them (do NOT drop conditions the user asked for).
- If answer > 200: might be too noisy, but better than 0.
- Target: 10-100 fires per 1000 bars.

AVOID (proven to produce 0 trades unless thresholds are loosened hard):
- Bollinger band crossing with std_dev>=1.5 as the entry trigger: fires <0.1% of bars, almost always 0 trades. If you use Bollinger, loosen std_dev to <=1.5 and don't rely on a clean band-cross.
- Multiple TIGHT AND-gates stacked (e.g. "was_below AND now_above AND momentum>0" with strict values): a many-gate state machine with tight thresholds fires ~never. Multi-AND is fine — TIGHT multi-AND is the trap. Loosen every gate.
- VWAP approximation using rolling std bands with tight multipliers (<=1.0): fires too rarely.

PREFERRED CONCEPTS (proven to produce trades):
- RSI threshold crossover (single condition: rsi < 35 → long)
- EMA or SMA crossover (fast crosses slow from below)
- Donchian channel breakout (close > max of last N bars)
- MACD histogram sign change (prev < 0, current >= 0)
- Rate-of-change threshold (ROC > X%)
- Hull MA crossover (use ~0.5*period weighted MA trick)


- No `import` statements. `math`, `statistics` and `ind` (indicator library, see above) are pre-available (no import needed).
- MUST also define `max_lookback(params)` at module level returning the number of bars the block needs (e.g. `def max_lookback(params): return int(params.get("period", 14)) * 2 + 10`). Without it the price window is silently clipped to 55 bars and long-period indicators miscompute (M16).
- No try/except, no with, no async, no lambda, no yield, no global/nonlocal, no delete.
- No dunder access (anything starting with `_`) — not on attributes, not on names.
- Only these built-ins may be called: abs, min, max, sum, len, round, sorted, range, int, float, bool, str, list, tuple, dict, set, any, all, enumerate, zip, reversed, isinstance.
- Helper functions defined in the same `code` string may call each other — that is allowed.
- Only these attributes may be accessed: .params, .role, .type, .get, .keys, .values, .items, .value, .upper, .lower, .middle, .initialized, .is_net_long, .is_net_short, .is_flat, math/statistics module functions.

STYLE:
- Keep each function short (helper 3-15 lines, evaluate 5-20 lines).
- Guard against short `closes` lists (return None when `len(closes) < required`).
- Use `state.get('prev', ...)` / `state['prev'] = ...` pattern for cross-detection.
- Prefer clear code over cleverness — the framework runs one bar at a time.

Return ONLY the JSON object. No prose, no explanation, no code fences."""


def _summarize_role_hint(role_hint: str) -> str:
    if role_hint == "entry":
        return "This block is meant for ENTRY: evaluate should return 'long' or 'short' (or None). Do not return 'exit'."
    if role_hint == "exit":
        return "This block is meant for EXIT: evaluate should return 'exit' (or None). Do not return 'long'/'short'."
    return "This block may be used as either entry or exit; check `block.role` and act accordingly."


def _extract_json_object(text: str) -> str:
    """Return the first balanced {...} block found in text. Handles preambles,
    trailing commentary, and code fences. Respects string literals."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    if start < 0:
        return s  # let json.loads produce the error
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s[start:]  # unbalanced — let json.loads raise


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


def _custom_block_system_prompt(role_hint: str) -> str:
    """Return a role-locked system prompt for one custom-block call.

    The generic prompt documents both return vocabularies.  In live AUTO runs
    Fable consequently generated entry signals (``long``/``short``) for three
    exit blocks even though the user prompt named the role.  Repeating the
    contract at system priority removes that ambiguity.
    """

    if role_hint == "exit":
        lock = (
            "\n\nROLE LOCK — EXIT ONLY:\n"
            "This call generates an EXIT block. Every firing branch in evaluate() "
            'must return the literal "exit". Non-firing branches return None. '
            'The strings "long" and "short" are forbidden anywhere in a '
            "return statement. Do not infer direction; closing either open side is "
            "the engine's responsibility."
        )
    else:
        lock = (
            "\n\nROLE LOCK — ENTRY ONLY:\n"
            "This call generates an ENTRY block. Every firing branch in evaluate() "
            'must return the literal "long" or "short". Non-firing branches '
            'return None. The string "exit" is forbidden in return statements.'
        )
    # Custom generation was the dominant AUTO token consumer in live runs:
    # one idea plus entry/exit code could spend several 4k+ CLI responses.
    # This is an instruction-level budget for the CLI path (which has no hard
    # output switch) and also keeps API output concise without weakening the
    # role/sandbox contract above.
    compact = (
        "\n\nCOMPACT OUTPUT BUDGET:\n"
        "Think silently. Return only the schema JSON, with no rationale beyond "
        "meta.help. Use at most 3 parameters and no helper unless essential; "
        "keep code at most 60 lines and the entire response under 1,800 tokens."
    )
    return CUSTOM_BLOCK_SYSTEM_PROMPT + lock + compact


def _env_bounded(name: str, default, *, lo=None, hi=None, cast=int):
    """Parse a numeric env var with clamping and a safe fallback on a bad value.

    Shared by the custom-block tunables below, which each independently
    hand-rolled the same try/except-ValueError + clamp shape.
    """
    try:
        v = cast(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return cast(default)
    if lo is not None:
        v = max(cast(lo), v)
    if hi is not None:
        v = min(cast(hi), v)
    return v


def _call_claude_for_block(
    user_prompt: str,
    *,
    role_hint: str = "entry",
    token_limit: int | None = None,
    tokens_spent: int = 0,
) -> tuple[dict, dict]:
    """Returns (parsed_json, usage) — M1583: count custom-block tokens too."""
    client = _get_client()
    # Compact JSON-only custom blocks do not need the generic 4k ceiling.
    # Keeping the limit below the prompt's 1,800-token contract reduces
    # OpenRouter latency and prevents oversized retry spend.
    custom_max_tokens = _env_bounded(
        "AGENT_CUSTOM_BLOCK_MAX_TOKENS", 1_800, lo=512, hi=1_800
    )
    # A custom-block timeout should be shorter than the generic research
    # call deadline; the composer can safely select a builtin fallback.
    custom_timeout = _env_bounded(
        "AGENT_CUSTOM_BLOCK_TIMEOUT", 75.0, lo=15.0, hi=120.0, cast=float
    )
    request = {
        "_purpose": "custom_block",
        "max_tokens": custom_max_tokens,
        "timeout": custom_timeout,
        "system": _custom_block_system_prompt(role_hint),
        "messages": [{"role": "user", "content": user_prompt}],
    }

    # A custom block's budget has to apply to *provider attempts*, not merely
    # to the final response. `_create_message` may retry a truncated 4k answer
    # at 16k, and counting only after it returned made the advertised 25k cap
    # an overshoot detector rather than a spending guard. Temporarily compose a
    # local admission hook with AUTO's run-wide hook; this also keeps STOP and
    # the global 250k ceiling intact.
    if token_limit is None:
        resp = _create_message(client, **request)
        usage = _usage_dict(resp)
    else:
        limit = max(0, int(token_limit))
        spent_before = max(0, int(tokens_spent))
        observed: dict[str, int] = {}
        previous_admit = getattr(_LLM_CONTROL, "admit_check", None)
        previous_observer = getattr(_LLM_CONTROL, "observer", None)

        def _observed_total() -> int:
            return sum(
                int(observed.get(k) or 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )

        def _local_admit(bound: dict) -> None:
            reserve = max(0, int(bound.get("total_token_bound") or 0))
            projected = spent_before + _observed_total() + reserve
            if projected > limit:
                raise LLMTokenBudgetExceeded(
                    "custom_block token cap cannot admit provider attempt: "
                    f"{spent_before + _observed_total():,}/{limit:,} spent, "
                    f"{reserve:,} reserved"
                )
            if callable(previous_admit):
                previous_admit(bound)

        def _local_observer(event: dict) -> None:
            payload = event.get("usage") or {}
            if isinstance(payload, dict):
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ):
                    observed[key] = observed.get(key, 0) + int(payload.get(key) or 0)
            if callable(previous_observer):
                previous_observer(event)

        _LLM_CONTROL.admit_check = _local_admit
        _LLM_CONTROL.observer = _local_observer
        try:
            resp = _create_message(client, **request)
        finally:
            _LLM_CONTROL.admit_check = previous_admit
            _LLM_CONTROL.observer = previous_observer
        # Includes a discarded truncated response, if any. The caller's retry
        # counter and telemetry now agree with actual provider attempts.
        usage = observed or _usage_dict(resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    payload = _extract_json_object(text)
    try:
        return json.loads(payload), usage
    except json.JSONDecodeError as e:
        snippet = (payload[:400] if payload else text[:400]).replace("\n", "\\n")
        raise json.JSONDecodeError(
            f"{e.msg} — response snippet: {snippet!r}", e.doc or "", e.pos
        ) from None


def _repair_exit_return_literals(src: str) -> str | None:
    """Safely map literal entry signals to ``exit`` in an exit-only block.

    This is deliberately narrow: only direct literal returns inside
    ``evaluate`` are changed. Dynamic expressions, helper returns, syntax
    errors, and entry blocks still go through the normal retry/fallback path.
    The repaired source is re-run through the full AST and runtime gates by the
    caller before it can be persisted.
    """

    import ast

    try:
        tree = ast.parse(src, filename="<custom_block_repair>")
    except SyntaxError:
        return None
    evaluate = _find_evaluate_def(tree)
    if evaluate is None:
        return None
    changed = False
    for node in ast.walk(evaluate):
        if (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and node.value.value in {"long", "short"}
        ):
            node.value = ast.copy_location(ast.Constant(value="exit"), node.value)
            changed = True
    if not changed:
        return None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


_BREAKDOWN_SYSTEM_PROMPT = """You split a trading-strategy description into DISTINCT
signal conditions, one per role, so each becomes a separate editable block that a
strategy engine combines with block-level OR/AND.

Return JSON ONLY (no markdown, no fences):
{
  "label": "short strategy name (<=40 chars, Turkish or English)",
  "entry_logic": "OR" | "AND",
  "exit_logic":  "OR" | "AND",
  "conditions": [
    {"role": "entry", "label": "<=40 char human label",
     "desc": "ONE self-contained signal condition, in the user's language, "
             "detailed enough to become a standalone block (name the indicator, "
             "period, threshold, and when it fires)"},
    ...
  ]
}

RULES:
- Split the description into its SEPARATE conditions — one condition per array item.
  "RSI<30 AND volume>2x average, exit on ATR stop or RSI>70" → two entry items
  (RSI-oversold, volume-spike) + two exit items (ATR-stop, RSI-overbought).
- Each `desc` must stand ALONE: a code generator will read it with no other context.
- AT LEAST one entry and AT LEAST one exit condition. If the user gave no explicit
  exit, add a single sensible exit (e.g. ATR trailing stop).
- entry_logic="OR" fires the entry when ANY entry condition hits; "AND" requires ALL
  the same bar. Default OR. Use AND only for a genuine confluence the user asked for,
  and NEVER AND across 3+ conditions (fires on <0.5% of bars → zero trades).
- exit_logic works the same; exits are almost always OR.
- Keep it to 2-4 conditions total unless the user clearly described more.
Return the JSON only."""


def propose_condition_breakdown(description: str) -> dict:
    """Split a natural-language description into SEPARATE signal conditions (each
    becomes its own block).

    Returns: {label, entry_logic, exit_logic, conditions:[{role,label,desc}], usage}.
    Conditions include at least 1 entry + 1 exit; otherwise ValueError (the caller
    falls back to the single-block path). LLM/parse errors are also raised.
    """
    client = _get_client()
    resp = _create_message(
        client,
        max_tokens=1500,
        system=_BREAKDOWN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description.strip()}],
    )
    usage = _usage_dict(resp)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(_extract_json_object(text))

    conds = data.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise ValueError("breakdown: no/empty 'conditions' list")
    clean: list[dict] = []
    for c in conds:
        if not isinstance(c, dict):
            continue
        role = c.get("role")
        desc = str(c.get("desc") or "").strip()
        if role in ("entry", "exit") and desc:
            clean.append(
                {
                    "role": role,
                    "label": str(c.get("label") or role).strip()[:40] or role,
                    "desc": desc,
                }
            )
    n_entry = sum(1 for c in clean if c["role"] == "entry")
    n_exit = sum(1 for c in clean if c["role"] == "exit")
    if n_entry < 1 or n_exit < 1:
        raise ValueError(
            f"breakdown: at least 1 entry + 1 exit required (entry={n_entry}, exit={n_exit})"
        )

    def _logic(v):
        return "AND" if str(v).upper() == "AND" else "OR"

    return {
        "label": str(data.get("label") or "").strip()[:40] or "Described strategy",
        "entry_logic": _logic(data.get("entry_logic")),
        "exit_logic": _logic(data.get("exit_logic")),
        "conditions": clean,
        "usage": usage,
    }


_REFINE_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi editörüsün.
Kullanıcı, doğal dilde kabaca bir strateji tarifi yazdı. TÜM çıktıların TÜRKÇE olmalı.

Görevlerin:
1. Tarifi tek, net, kesin ve test edilebilir bir kural setine dönüştür — dilbilgisini
   düzelt, eksik çıkış (exit) koşulu varsa ekle, belirsizliği gider, kısa tut (en fazla
   2-4 cümle). Kullanıcının anlattığı çekirdek mantığı DEĞİŞTİRME; yalnızca netleştir.
2. Eğer backtest metrikleri verilmişse, zayıf noktalara (düşük Sharpe, yüksek DD,
   az işlem, düşük win rate) odaklan ve tarifi bu sorunları giderecek şekilde güçlendir.
3. Stratejiyi güçlendirebilecek 2-4 SOMUT ilave ÖNERİ ver (ör. bir trend filtresi,
   hacim teyidi, uygun stop/take-profit, parametre aralığı). Her öneri kısa ve
   uygulanabilir olsun.
4. Olası TUZAKLARA/RİSKLERE dair uyarılar ver (ör. çok sıkı AND koşulu → az işlem,
   Bollinger tam kesişimi → 0 işlem, aşırı optimizasyon riski, düşük likidite).
   Uyarı yoksa boş liste döndür.

Yalnızca JSON döndür:
{"refined": "<iyileştirilmiş tarif>",
 "notes": "<tek kısa cümle: neyi değiştirdin>",
 "suggestions": [{"kind": "oneri|uyari", "text": "<kısa madde>"}]}
Girdi zaten net görünse bile her zaman en az 2 somut öneri ver; notes alanına kısa bir iyileştirme gerekçesi yaz.
"""


def _format_metrics_block(
    raw_description: str,
    backtest_metrics: dict | None = None,
    robustness: dict | None = None,
) -> str:
    """Compose the user-prompt text: strategy description + optional backtest &
    robustness metrics. Shared by propose_refined_description (single-shot) and
    chat_refine (multi-turn) so both feed the model the same context format.

    If backtest_metrics is empty/None the raw description is returned as-is.
    """
    user_content = raw_description.strip()
    if not backtest_metrics:
        return user_content
    parts = ["Strateji tarifi:\n" + user_content, "\nSon backtest sonuçları:"]
    if backtest_metrics.get("spec_name"):
        parts.append(f"  Strateji: {backtest_metrics['spec_name']}")
    if backtest_metrics.get("best_tf"):
        parts.append(f"  En iyi TF: {backtest_metrics['best_tf']}")
    for key, label in [
        ("pnl_pct", "PnL %"),
        ("sharpe", "Sharpe"),
        ("max_dd", "Max DD %"),
        ("n_trades", "İşlem sayısı"),
        ("win_rate", "Kazanç %"),
    ]:
        val = backtest_metrics.get(key)
        if val not in (None, ""):
            parts.append(f"  {label}: {val}")
    # Robustness özeti — overfitting-farkındalıklı öneri için.
    if robustness and any(v not in (None, "") for v in robustness.values()):
        parts.append("\nRobustness analizi:")
        for key, label in [
            (
                "overfitting_score",
                "Overfitting skoru (≥0.7 sağlam · <0.4 aşırı-uyum)",
            ),
            ("verdict", "Verdict"),
            ("wfo_efficiency", "WFO verimliliği (OOS/in-sample)"),
            ("oos_sharpe", "OOS Sharpe"),
            ("stability", "Parametre kararlılığı"),
        ]:
            val = robustness.get(key)
            if val not in (None, ""):
                parts.append(f"  {label}: {val}")
        parts.append(
            "\nBu sonuçlara göre stratejiyi iyileştir. Overfitting skoru düşük "
            "(<0.4) ya da OOS Sharpe zayıfsa: parametreleri sadeleştir, aşırı "
            "optimizasyondan kaçın, daha genel kurallar öner. Robustsa (≥0.7): "
            "güçlendirme/pozisyon ölçekleme önerebilirsin."
        )
    else:
        parts.append("\nBu sonuçlara göre stratejiyi iyileştir.")
    return "\n".join(parts)


def propose_refined_description(
    raw_description: str,
    backtest_metrics: dict | None = None,
    robustness: dict | None = None,
) -> dict:
    """Rewrite the user's raw strategy description into a cleaner, more precise version.

    If backtest_metrics is provided (pnl_pct, sharpe, max_dd, n_trades, win_rate,
    spec_name, best_tf), the AI uses them to suggest targeted improvements.
    If robustness is provided (overfitting_score, verdict, wfo_efficiency,
    oos_sharpe, stability), the AI weighs overfitting risk: low score / poor OOS
    → simplify & de-tune; robust → allowed to strengthen.
    Returns: {refined: str, notes: str, suggestions: list[{kind, text}]}.
    Falls back to original text (empty suggestions) on any error.
    """
    client = _get_client()
    try:
        user_content = _format_metrics_block(
            raw_description, backtest_metrics, robustness
        )
        resp = _create_message(
            client,
            max_tokens=700,
            system=_REFINE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(_extract_json_object(text))
        refined = str(data.get("refined") or "").strip()
        notes = str(data.get("notes") or "").strip()
        # Normalize suggestions: keep only {kind in oneri|uyari, non-empty text}.
        suggestions: list[dict] = []
        for s in data.get("suggestions") or []:
            if not isinstance(s, dict):
                continue
            stext = str(s.get("text") or "").strip()
            if not stext:
                continue
            kind = str(s.get("kind") or "oneri").strip().lower()
            if kind not in ("oneri", "uyari"):
                kind = "oneri"
            suggestions.append({"kind": kind, "text": stext})
        if not refined:
            raise ValueError("empty refined")
        return {"refined": refined, "notes": notes, "suggestions": suggestions}
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.refine").warning(
            "propose_refined_description failed: %r", _e
        )
        return {"refined": raw_description.strip(), "notes": "", "suggestions": []}


_CHAT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi danışmanısın.
Kullanıcı seninle SOHBET ederek bir strateji tarifini birlikte netleştiriyor.
TÜM yanıtların TÜRKÇE, kısa ve net olsun (gereksiz uzatma).

Davranış:
- Kullanıcının sorusuna/isteğine DOĞRUDAN cevap ver; önceki mesajlara atıfta bulunabilirsin.
- Stratejinin çekirdek mantığını DEĞİŞTİRME; yalnızca netleştir, güçlendir, tuzakları
  (aşırı-uyum, çok sıkı AND koşulu → az işlem, eksik çıkış koşulu, düşük likidite) uyar.
- Backtest/robustness metrikleri verildiyse zayıf noktalara odaklan (düşük Sharpe,
  yüksek DD, az işlem). Overfitting skoru düşükse sadeleştirmeyi öner.
- Somut ve uygulanabilir ol; istendiğinde parametre aralığı/filtre öner.

Her yanıtının EN SONUNA, o ana dek üzerinde uzlaşılan güncel ve net strateji tarifini
TEK SATIRLIK şu işaretli biçimde ekle (2-4 cümlelik özet, test edilebilir kurallar):
[NET_TARİF]: <buraya güncel net tarif>
Bu satır dışındaki metin kullanıcıyla serbest sohbetindir.
"""


def chat_refine(conversation_messages: list[dict], context: dict | None = None) -> dict:
    """Multi-turn strateji-iyileştirme sohbeti.

    conversation_messages: Anthropic messages[] — {"role": "user"|"assistant",
      "content": str} turn'leri. Metrik bağlamı (varsa) çağıran taraf, ilk user
      turn'üne ``_format_metrics_block`` ile gömer; bu fonksiyon listeyi olduğu
      gibi modele iletir. ``context`` şu an yalnızca ileriye dönük imza uyumu
      için tutulur (gömme çağıran tarafta yapılır).
    Döndürür: {"text": "<asistan sohbet yanıtı>", "refined": "<güncel net tarif>|''"}.
    Herhangi bir hatada nazik Türkçe özür + boş ``refined`` döner.
    """
    client = _get_client()
    try:
        import re

        resp = _create_message(
            client,
            max_tokens=1000,
            system=_CHAT_SYSTEM_PROMPT,
            messages=conversation_messages,
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        # Sondaki [NET_TARİF]: satırını ayıkla; kalanı sohbet metni olarak bırak.
        refined = ""
        m = re.search(r"\[NET_TAR[İI]F\]\s*:\s*(.+)", text)
        if m:
            refined = m.group(1).strip()
            text = text[: m.start()].rstrip()
        if not text:
            text = "Anladım. Devam edebilmem için biraz daha detay verir misin?"
        return {"text": text, "refined": refined}
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_refine failed: %r", _e)
        return {
            "text": (
                "Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin? "
                "İstersen tarifini elle düzenleyip 'Blokları Oluştur'a devam edebilirsin."
            ),
            "refined": "",
        }


def propose_custom_block(
    label: str, description: str, role_hint: str = "entry"
) -> dict:
    """Ask Claude to design a new custom signal block from a natural-language description.

    Returns {name, meta, code} on success. Raises GeneratedCodeError with a
    friendly message on repeated validation failure.
    """
    role_line = _summarize_role_hint(role_hint)
    role_contract = (
        'This is an ENTRY block: every firing branch MUST return exactly "long" or "short"; '
        'never return "exit".'
        if role_hint == "entry"
        else 'This is an EXIT block: every firing branch MUST return exactly "exit"; '
        'never return "long" or "short".'
    )
    user_prompt = f"""Design a new signal block.

Label (user's short name for it): {label}
Role hint: {role_hint}
{role_line}
ROLE CONTRACT (applies to EVERY conditional branch): {role_contract}

Description (user's words — infer parameters, thresholds, logic):
\"\"\"{description.strip()}\"\"\"

Return the JSON only."""

    last_error = None
    _acc_usage: dict = {}
    _attempt_limit = _env_bounded("AGENT_CUSTOM_BLOCK_MAX_ATTEMPTS", 2, lo=1, hi=2)
    _token_limit = _env_bounded("AGENT_CUSTOM_BLOCK_TOKEN_LIMIT", 25_000, lo=4_000)

    def _spent_tokens() -> int:
        return sum(
            int(_acc_usage.get(k) or 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )

    def _retry_allowed() -> bool:
        nonlocal last_error
        spent = _spent_tokens()
        if spent >= _token_limit:
            last_error = (
                f"{last_error}; custom_block token limit reached "
                f"({spent:,}/{_token_limit:,})"
            )
            return False
        return True

    def _retry_with(next_prompt: str) -> bool:
        """Set the next retry prompt; report whether another attempt is allowed.

        Every failure branch below ends with "set the next prompt, then break
        if the retry budget is spent" — this collapses that pair to one call.
        """
        nonlocal user_prompt
        user_prompt = next_prompt
        return _retry_allowed()

    for attempt in range(_attempt_limit):
        try:
            data, _u = _call_claude_for_block(
                user_prompt,
                role_hint=role_hint,
                token_limit=_token_limit,
                tokens_spent=_spent_tokens(),
            )
            for k, v in _u.items():
                _acc_usage[k] = _acc_usage.get(k, 0) + v
        except LLMTokenBudgetExceeded as e:
            # Local cap refusal is intentional and cannot be healed by another
            # paid retry. Return a normal generated-code failure to the caller,
            # which can select the role-safe builtin candidate instead.
            last_error = str(e)
            break
        except Exception as e:
            _raise_if_llm_control_abort(e)
            if is_terminal_llm_error(e):
                raise TerminalLLMError(
                    f"terminal LLM provider failure: {type(e).__name__}: {e}"
                ) from e
            last_error = f"Claude request failed: {type(e).__name__}: {e}"
            # Repeating a hard provider timeout doubles wall-clock delay while
            # producing no usable block.  Bubble a normal generated-code
            # failure so AUTO's existing role-safe builtin fallback is used.
            if isinstance(e, TimeoutError):
                break
            if not _retry_with(
                f"Previous request failed ({type(e).__name__}). {user_prompt}"
            ):
                break
            continue

        if (
            not isinstance(data, dict)
            or "name" not in data
            or "meta" not in data
            or "code" not in data
        ):
            last_error = f"schema mismatch: missing keys in {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
            if not _retry_with(
                f"Your last output was invalid: {last_error}. Return JSON with keys name/meta/code."
            ):
                break
            continue

        name = str(data["name"]).strip()
        meta = data["meta"]
        code = data["code"]

        # Basic name validation happens later in the store; validate meta shape here.
        if not isinstance(meta, dict) or "label" not in meta or "params" not in meta:
            last_error = "meta must have label and params"
            if not _retry_with(
                f"Your last output was invalid: {last_error}. Fix and return valid JSON."
            ):
                break
            continue

        # Persist the provenance contract with the block. The composer validates
        # this metadata whenever a spec assigns the block to a role.
        meta = dict(meta)
        meta["role"] = role_hint

        try:
            _validate_generated_code(code)
            _test_execute_generated(
                code,
                meta=meta,
                require_max_lookback=True,
                role_hint=role_hint,
            )
        except GeneratedCodeError as e:
            last_error = str(e)
            # Live Fable runs repeatedly produced otherwise-valid exit code
            # whose firing branches returned the entry vocabulary.  For direct
            # literal returns the semantics are unambiguous: an exit condition
            # firing means ``exit``. Repair locally and re-run every safety gate
            # instead of paying for another full generation call.
            if role_hint == "exit" and "role contract" in last_error:
                repaired = _repair_exit_return_literals(code)
                if repaired is not None:
                    try:
                        _validate_generated_code(repaired)
                        _test_execute_generated(
                            repaired,
                            meta=meta,
                            require_max_lookback=True,
                            role_hint=role_hint,
                        )
                    except GeneratedCodeError as repair_error:
                        last_error = f"{last_error}; safe repair failed: {repair_error}"
                    else:
                        return {
                            "name": name,
                            "meta": meta,
                            "code": repaired,
                            "usage": _acc_usage,
                            "repair": "exit_literal_normalized",
                        }
            if not _retry_with(
                f"Your last code was REJECTED with this error:\n\n{last_error}\n\n"
                f"ROLE CONTRACT (still mandatory): {role_contract}\n"
                "Fix the code and return the same JSON schema. Remember: no imports, "
                "no leading underscore names, no try/with/lambda/global/nonlocal, only whitelisted "
                "attributes (.params/.role/.value/.upper/.lower/.middle/.initialized/.get/.keys/"
                ".values/.items) and only whitelisted builtins."
            ):
                break
            continue

        return {"name": name, "meta": meta, "code": code, "usage": _acc_usage}

    raise GeneratedCodeError(
        f"Claude could not produce valid code after {_attempt_limit} attempts. "
        f"Last error: {last_error}"
    )


# ── "Blok kodunu AI ile düzenle" — çok-turlu sohbet ─────────────────────────
# propose_custom_block (yukarı) tek-atış ÜRETİR; bu ise MEVCUT bir custom block'un
# kodunu kullanıcıyla SOHBET ederek düzenler. Sohbet danışman tonu _CHAT_SYSTEM_PROMPT
# (chat_refine) deseninden; kod/güvenlik kuralları CUSTOM_BLOCK_SYSTEM_PROMPT'tan gelir.
# Protokol: asistan serbest TR sohbet metni verir; kodu DEĞİŞTİRDİĞİNDE yanıtın EN SONUNA
# tek satır [NET_KOD]: işareti + tek bir JSON {"meta":..., "code":...} bloğu ekler.
_BLOCK_EDIT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) sinyal BLOĞU (Python kodu) düzenleme asistanısın.
Kullanıcı MEVCUT bir bloğun kodunu seninle SOHBET ederek düzenliyor.
TÜM sohbet yanıtların TÜRKÇE, kısa ve net olsun.

Davranış:
- Kullanıcının isteğine DOĞRUDAN cevap ver; ne değiştirdiğini bir-iki cümleyle açıkla.
- Bloğun ADINI (name) ASLA değiştirme — sadece meta (label/params/help) ve code üzerinde çalış.
- Kullanıcı yalnızca SORU sorduysa (değişiklik istemediyse) kodu OLDUĞU GİBİ bırak; [NET_KOD] EKLEME.
- Mevcut kodu koru; yalnızca istenen değişikliği uygula, gereksiz yere baştan yazma.

Kod kuralları (ihlal edilirse kod reddedilir):
- `evaluate(state, block, closes, indicators, portfolio)` tanımlı olmalı; "long"/"short"/"exit"/None döner.
- `max_lookback(params)` modül seviyesinde tanımlı olmalı (gerekli bar sayısı).
- import YOK; `math`, `statistics`, `ind` (NAU-parite indikatör kütüphanesi) hazır — import gerekmez.
  Indikatör matematiğini elle yazma; `ind.calc_rsi/calc_atr/calc_adx/...` kullan.
- try/except, with, async, lambda, yield, global/nonlocal, delete YOK.
- `_` ile başlayan ad/attribute (dunder) YOK.
- İzinli built-in'ler: abs, min, max, sum, len, round, sorted, range, int, float, bool, str,
  list, tuple, dict, set, any, all, enumerate, zip, reversed, isinstance.
- Sinyal frekansı: blok çok seyrek ateşlerse işlem az olur ve elenir; eşikleri gevşek tut.

Kodu DEĞİŞTİRDİĞİNDE yanıtının EN SONUNA — sohbet metninden sonra — tam olarak şu biçimi ekle:
[NET_KOD]:
{"meta": {"label": "...", "params": {...}, "help": "..."}, "code": "def evaluate(...):\\n    ...\\n"}
Bu JSON tek satır veya çok satır olabilir ama GEÇERLİ JSON olmalı (code alanı \\n kaçışlı string).
Bu blok dışındaki metin kullanıcıyla serbest sohbetindir. Kodu değiştirmediysen bu bloğu HİÇ ekleme.
"""


def chat_edit_block(
    name: str,
    existing_meta: dict,
    existing_code: str,
    conversation_messages: list[dict],
) -> dict:
    """Mevcut bir custom block'un kodunu çok-turlu sohbetle düzenle.

    ``conversation_messages``: Anthropic messages[] turn'leri. İlk user turn'üne
    çağıran taraf mevcut kod+meta'yı gömer; bu fonksiyon listeyi olduğu gibi iletir.
    Döndürür: {"text", "meta", "code", "changed", "error", "usage"}.
      - ``changed`` True ise ``meta``/``code`` yeni (doğrulanmış) değerlerdir.
      - ``changed`` False ise kod DEĞİŞMEDİ (kullanıcı soru sormuş) — çağıran eski
        kod/meta'yı korur; ``error`` doluysa kod önerildi ama doğrulama başarısız.
    Herhangi bir LLM/parse hatasında nazik TR metin + ``changed=False`` döner.
    """
    client = _get_client()
    try:
        import re

        resp = _create_message(
            client,
            max_tokens=4000,
            system=_BLOCK_EDIT_SYSTEM_PROMPT,
            messages=conversation_messages,
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()

        # [NET_KOD]: işaretini ara; sonrasındaki JSON'u ayıkla (chat_refine deseni).
        m = re.search(r"\[NET_KOD\]\s*:", text)
        if not m:
            # Kod değişmedi — kullanıcı soru sormuş olabilir.
            if not text:
                text = (
                    "Anladım. Blokta ne değiştirmemi istediğini biraz daha açar mısın?"
                )
            return {
                "text": text,
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": "",
                "usage": usage,
            }

        chat_text = text[: m.start()].rstrip()
        payload = _extract_json_object(text[m.end() :])
        try:
            data = json.loads(payload)
            new_meta = data["meta"]
            new_code = data["code"]
            if (
                not isinstance(new_meta, dict)
                or "label" not in new_meta
                or "params" not in new_meta
            ):
                raise ValueError("meta must have label and params")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return {
                "text": chat_text
                or "Kodu güncellemeye çalıştım ama çıktı bozuktu. Tekrar dener misin?",
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": f"model output parse error: {e}",
                "usage": usage,
            }

        # Güvenlik + smoke: propose_custom_block:1554-1566 ile birebir defense-in-depth.
        try:
            _validate_generated_code(new_code)
            edit_role = str(
                existing_meta.get("role") or new_meta.get("role") or "entry"
            )
            new_meta = dict(new_meta)
            new_meta["role"] = edit_role
            _test_execute_generated(
                new_code,
                meta=new_meta,
                require_max_lookback=True,
                role_hint=edit_role,
            )
        except GeneratedCodeError as e:
            # Kod reddedildi — eski geçerli kodu KORU, hatayı kullanıcıya göster
            # (otomatik iç-tur yok; kullanıcı sohbetle düzelttirir).
            return {
                "text": chat_text,
                "meta": existing_meta,
                "code": existing_code,
                "changed": False,
                "error": str(e),
                "usage": usage,
            }

        if not chat_text:
            chat_text = "Kodu güncelledim. Önizlemeyi kontrol edip kaydedebilirsin."
        return {
            "text": chat_text,
            "meta": new_meta,
            "code": new_code,
            "changed": True,
            "error": "",
            "usage": usage,
        }
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_edit_block failed: %r", _e)
        return {
            "text": ("Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin?"),
            "meta": existing_meta,
            "code": existing_code,
            "changed": False,
            "error": "",
            "usage": {},
        }


# ── "Blok listesini AI ile düzenle" — çok-turlu sohbet ──────────────────────
# Strateji taslağındaki (draft) SignalBlock listesini kullanıcıyla sohbet ederek
# düzenler. propose_composed_strategy'nin katalog-gömme (_catalog_summary) ve
# param-clamp (_coerce_catalog_blocks) mantığını yeniden kullanır; halüsinasyon
# blok tipi üretmeyi ENGELLER (yalnızca BLOCK_CATALOG'daki tipler geçer).
_BLOCKS_EDIT_SYSTEM_PROMPT = """Sen bir alım-satım (trading) stratejisi TASLAĞINI düzenleyen bir asistansın.
Kullanıcı, stratejinin BLOK LİSTESİNİ (giriş/çıkış sinyalleri) seninle SOHBET ederek düzenliyor.
TÜM sohbet yanıtların TÜRKÇE, kısa ve net olsun.

Kullanabileceğin blok tipleri YALNIZCA aşağıdaki katalogdur — BAŞKA tip UYDURMA:
{catalog}

Kurallar:
- Kullanıcının isteğine DOĞRUDAN cevap ver; ne değiştirdiğini bir-iki cümleyle açıkla.
- Her blok: type (yukarıdaki katalogdan), role ("entry" veya "exit"), params (kataloğun
  o tip için tanımladığı parametreler; verilmeyenler varsayılana düşer).
- Yalnızca SORU sorulduysa listeyi OLDUĞU GİBİ bırak; [NET_BLOKLAR] EKLEME.
- Mevcut listeyi koru; sadece istenen değişikliği uygula (blok ekle/çıkar/parametre değiştir).

Listeyi DEĞİŞTİRDİĞİNDE yanıtının EN SONUNA — sohbet metninden sonra — tam olarak şu biçimi ekle:
[NET_BLOKLAR]:
{"blocks": [{"type": "rsi_threshold", "role": "entry", "params": {"period": 14, "threshold": 30}}, ...]}
Bu GEÇERLİ bir JSON olmalı. Bu blok dışındaki metin kullanıcıyla serbest sohbetindir.
Listeyi değiştirmediysen bu bloğu HİÇ ekleme.
"""


def _coerce_catalog_blocks(raw_blocks: list) -> list[dict]:
    """Katalog-dışı tipleri düşür, paramları katalog aralığına clamp'le.

    _validate_composed'daki (agent.py:725) coercion mantığıyla aynı; ancak
    entry/exit ZORUNLULUĞU ve fallback-exit EKLEMEZ — draft düzenlemede liste
    yarı-tamamlanmış olabilir. Dönen her öğe {type, role, params}.
    """
    from composer import BLOCK_CATALOG

    out: list[dict] = []
    for b in raw_blocks or []:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        role = b.get("role")
        if btype not in BLOCK_CATALOG or role not in ("entry", "exit"):
            continue
        meta = BLOCK_CATALOG[btype]
        params: dict = {}
        for pname, pspec in meta["params"].items():
            raw = (b.get("params") or {}).get(pname, pspec["default"])
            try:
                if pspec["type"] == "int":
                    params[pname] = max(pspec["min"], min(pspec["max"], int(raw)))
                elif pspec["type"] == "float":
                    params[pname] = max(pspec["min"], min(pspec["max"], float(raw)))
                else:
                    params[pname] = raw if raw in pspec["options"] else pspec["default"]
            except (TypeError, ValueError):
                params[pname] = pspec["default"]
        # cross-family: slow>fast; atr_stop exit-only (mirror _validate_composed).
        if btype in ("ma_cross", "ema_cross", "macd_cross") and params.get(
            "slow", 0
        ) <= params.get("fast", 0):
            params["fast"], params["slow"] = 10, max(params.get("slow", 30), 30)
        if btype == "atr_stop":
            role = "exit"
        out.append({"type": btype, "role": role, "params": params})
    return out


def chat_edit_blocks(
    existing_blocks: list[dict],
    conversation_messages: list[dict],
) -> dict:
    """Strateji taslağının blok listesini çok-turlu sohbetle düzenle.

    ``existing_blocks``: [{type, role, params}, ...]. Döndürür:
      {"text", "blocks", "changed", "error", "usage"}.
      - ``changed`` True ise ``blocks`` yeni (katalog-doğrulanmış) listedir.
      - ``changed`` False ise liste DEĞİŞMEDİ; ``error`` doluysa öneri geçersizdi.
    LLM/parse hatasında nazik TR metin + ``changed=False`` döner.
    """
    client = _get_client()
    try:
        import re

        system = _BLOCKS_EDIT_SYSTEM_PROMPT.replace("{catalog}", _catalog_summary())
        resp = _create_message(
            client,
            max_tokens=1500,
            system=system,
            messages=conversation_messages,
        )
        usage = _usage_dict(resp)
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()

        m = re.search(r"\[NET_BLOKLAR\]\s*:", text)
        if not m:
            if not text:
                text = "Anladım. Blok listesinde ne değiştirmemi istediğini biraz açar mısın?"
            return {
                "text": text,
                "blocks": existing_blocks,
                "changed": False,
                "error": "",
                "usage": usage,
            }

        chat_text = text[: m.start()].rstrip()
        payload = _extract_json_object(text[m.end() :])
        try:
            data = json.loads(payload)
            raw_blocks = data["blocks"]
            if not isinstance(raw_blocks, list):
                raise ValueError("blocks must be a list")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return {
                "text": chat_text
                or "Listeyi güncellemeye çalıştım ama çıktı bozuktu. Tekrar dener misin?",
                "blocks": existing_blocks,
                "changed": False,
                "error": f"model output parse error: {e}",
                "usage": usage,
            }

        clean = _coerce_catalog_blocks(raw_blocks)
        if not clean:
            # Model yalnızca geçersiz/katalog-dışı tipler önerdi — eski listeyi koru.
            return {
                "text": chat_text,
                "blocks": existing_blocks,
                "changed": False,
                "error": "önerilen bloklar geçersiz (katalog-dışı tip veya boş liste)",
                "usage": usage,
            }

        if not chat_text:
            chat_text = (
                "Blok listesini güncelledim. Önizlemeyi kontrol edip onaylayabilirsin."
            )
        return {
            "text": chat_text,
            "blocks": clean,
            "changed": True,
            "error": "",
            "usage": usage,
        }
    except Exception as _e:
        import logging as _logging

        _logging.getLogger("agent.chat").warning("chat_edit_blocks failed: %r", _e)
        return {
            "text": ("Şu an AI danışmana ulaşılamadı. Biraz sonra tekrar dener misin?"),
            "blocks": existing_blocks,
            "changed": False,
            "error": "",
            "usage": {},
        }


_AGENT_IDEA_PROMPT = """\
You are a {market_tr} research agent.{market_note}

{exploration_directive}

⚠️ FORBIDDEN: Bollinger Band crossing — always produces 0 trades. NEVER select this.
⚠️ FORBIDDEN: VWAP deviation + momentum AND combination — produces 0 trades.

Past strategies and their RESULTS (0 trades = failed, change it!):
{history}

Concepts previously generated as custom blocks (DO NOT REGENERATE):
{used_concepts}

User hint:
{hint}

Rule: Usable data = FULL OHLCV. closes (close) + indicators["highs"] (high)
+ indicators["lows"] (low) + indicators["volumes"] (volume) — all four are float lists,
aligned with closes, oldest to newest. Since high/low are AVAILABLE, real OHLC indicators
can be computed: ATR, ADX/DMI, Stochastic, Donchian/Keltner channel, WaveTrend, SuperTrend.
Volume-based ideas (volume spike, OBV, volume confirmation) are also valid. If the user hint
contains one of these indicators, use its REAL formula (do not fall back to a crude proxy).
If the user asked for multi-indicator confluence (e.g. "RSI AND ADX AND ATR"), produce an idea
that uses them ALL TOGETHER — do not trim. There is no upper limit on AND condition count; the
only cost is signal frequency, so compensate with loose thresholds (low-trade runs are already
filtered out later). If the user did not ask for confluence, a single simple condition is enough.

Return in this JSON format (write nothing else):
{{
  "name": "short strategy name (2-4 words)",
  "description": "1-sentence trading thesis",
  "entry_label": "short name for the entry block",
  "entry_desc": "describe how to compute the entry signal (over closes/highs/lows/volumes series — ATR/ADX/Stochastic requiring high/low can also be used)",
  "exit_label": "short name for the exit block",
  "exit_desc": "describe the exit signal"
}}
"""


# Indicators recognized in hints: canonical name → search patterns. Short acronyms
# (rsi/adx/atr…) match on word boundaries, long distinctive names (bollinger/stochastic…)
# match as substrings — so that e.g. "ma" inside "smart" is not a false positive.
_HINT_INDICATORS: dict[str, list[str]] = {
    "RSI": [r"\brsi\b"],
    "ADX/DMI": [r"\badx\b", r"\bdmi\b", r"\bdx\b"],
    "ATR": [r"\batr\b"],
    "MACD": [r"\bmacd\b"],
    "Stochastic": ["stochastic", "stokastik", r"\bstoch\b"],
    "Bollinger": ["bollinger", r"\bbband"],
    "EMA": [r"\bema\b"],
    "SMA/MA": [r"\bsma\b", r"\bwma\b", "hareketli ortalama", r"\bmoving average\b"],
    "WaveTrend": ["wavetrend", "wave trend", r"\bwt\b"],
    "Donchian": ["donchian"],
    "Keltner": ["keltner"],
    "CCI": [r"\bcci\b"],
    "Williams %R": ["williams", "%r"],
    "OBV": [r"\bobv\b"],
    "SuperTrend": ["supertrend", "super trend"],
    "Momentum/ROC": ["momentum", r"\broc\b", "rate of change"],
    "Ichimoku": ["ichimoku"],
    "Volume": ["hacim", r"\bvolume\b"],
}


def _hint_indicators(hint: str) -> list[str]:
    """Canonical names of recognized indicators mentioned in the hint (ordered, deduplicated)."""
    import re

    low = (hint or "").lower()
    found = []
    for canon, patterns in _HINT_INDICATORS.items():
        if any(re.search(p, low) for p in patterns):
            found.append(canon)
    return found


def _exploration_directive(hint: str) -> str:
    """If the hint contains explicit indicators, returns a 'stay in this set +
    scan variations' directive, otherwise a 'pick a different indicator family'
    directive.

    Goal: when the user gives 'RSI+ADX+ATR', the agent should scan the
    combination/parameter space of this set instead of drifting to a different
    indicator family every round.
    """
    inds = _hint_indicators(hint)
    if inds:
        names = ", ".join(inds)
        return (
            f"Task: The user requested these indicators: {names}. These are the "
            "CORE of the strategy — use them in every idea (alone, in pairs, or all "
            "together with AND; each round try a DIFFERENT combination/subset + "
            "DIFFERENT parameters/thresholds/logic, so you systematically scan the "
            "space of this set). ALSO be CREATIVE by adding to each idea a "
            "COMPLEMENTARY indicator (an extra filter, confirmation, or a better "
            "exit) that you think COULD INCREASE PROFIT — do not abandon the "
            "requested set, but do not stay limited to them alone either; build on "
            "top. Do NOT REPEAT the combinations tried in the history below; produce "
            "a NEW variation each round. (STAY IN THIS SET.)"
        )
    return (
        "Task: Looking at the past results below, produce a NEW and COMPLETELY "
        "DIFFERENT strategy idea. pick a DIFFERENT indicator family from the "
        "existing history (e.g. Donchian channel, Hull MA, Williams %R, Keltner "
        "channel, DEMA/TEMA, rate-of-change threshold, CCI, WaveTrend, MACD "
        "histogram sign change)."
    )


def _propose_agent_strategy_idea(
    hint: str,
    history: list,
    used_concepts: list | None = None,
    market: str | None = None,
    timeframe: str = "",
) -> dict:
    """Ask Claude for a novel strategy idea (labels + descriptions only, no code).

    Returns dict with keys: name, description, entry_label, entry_desc,
    exit_label, exit_desc. Falls back to a hardcoded idea on any failure.

    ``market`` — optional market context; if None the crypto phrasing is kept.
    ``timeframe`` — the bar this idea will be scored on (see _timeframe_line);
    the custom-block path needs it as much as the builtin one.
    """
    history_summary = ""
    if history:
        tried_with_outcomes = []
        for r in history[-8:]:
            n_trades = (r.metrics or {}).get("n_trades", 0) if not r.error else 0
            if n_trades == 0:
                outcome = "❌ 0 TRADES — NEVER RAN"
            else:
                sh = (r.metrics or {}).get("sharpe", 0) or 0
                outcome = f"✓ {n_trades} trade, sharpe={sh:.1f}"
            name = r.strategy.split(":")[-1].strip()
            tried_with_outcomes.append(f"  {name}: {outcome}")
        history_summary = "Previously tried and their results:\n" + "\n".join(
            tried_with_outcomes
        )

        # Highlight zero-trade failures explicitly
        zero_names = [
            r.strategy.split(":")[-1].strip()
            for r in history
            if not r.error and (r.metrics or {}).get("n_trades", 0) == 0
        ]
        if zero_names:
            history_summary += (
                "\n\n⛔ CONCEPTS THAT PRODUCED ZERO TRADES — ALWAYS SKIP THESE: "
                + ", ".join(zero_names[-8:])
            )

    concepts_str = "None (first round)"
    if used_concepts:
        concepts_str = ", ".join(used_concepts[-12:])  # last 12 concepts

    if market:
        market_tr = "US equity trading"
        market_note = (
            f"\nInstrument: {market}. Not crypto — produce ideas suited to equity "
            "dynamics and the bar interval (on daily bars use swing logic instead "
            "of 'intraday')."
        )
    else:
        market_tr = "crypto trading"
        market_note = ""

    market_note += _timeframe_line(timeframe)

    prompt = _AGENT_IDEA_PROMPT.format(
        market_tr=market_tr,
        market_note=market_note,
        exploration_directive=_exploration_directive(hint),
        history=history_summary or "No history yet.",
        used_concepts=concepts_str,
        hint=hint.strip() or "None (fully autonomous)",
    )

    try:
        client = _get_client()
        resp = _create_message(
            client,
            _purpose="idea",
            max_tokens=MAX_TOKENS_IDEA,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        idea = json.loads(_extract_json_object(text))
        idea["usage"] = _usage_dict(resp)  # M1583: count idea tokens too
        return idea
    except Exception as e:
        _raise_if_llm_control_abort(e)
        logging.warning("_propose_agent_strategy_idea failed: %s", e, exc_info=True)
        # Fallback: pick from a variety of concepts (avoid always returning Bollinger)
        _FALLBACK_IDEAS = [
            {
                "name": "RSI Oversold Reversal",
                "description": "Enter when RSI turns up from below 30, exit above 70.",
                "entry_label": "RSI Oversold Entry",
                "entry_desc": "RSI 14 period; if the previous bar was below 30 and the current bar crosses above 30, long signal.",
                "exit_label": "RSI Overbought Exit",
                "exit_desc": "Produce an exit signal when RSI rises above 70.",
            },
            {
                "name": "MACD Zero Line Cross",
                "description": "Momentum begins when the MACD histogram crosses zero upward.",
                "entry_label": "MACD Zero Cross Entry",
                "entry_desc": "When the MACD histogram (12-26 EMA difference) crosses from below zero to above, long signal.",
                "exit_label": "MACD Negative Exit",
                "exit_desc": "Produce an exit signal when the MACD histogram turns negative.",
            },
            {
                "name": "EMA Ribbon Breakout",
                "description": "Trend starts when the short EMA crosses above the long EMA.",
                "entry_label": "EMA Ribbon Entry",
                "entry_desc": "If the 5-period EMA is below the 20-period EMA and crosses above it, long signal.",
                "exit_label": "EMA Ribbon Exit",
                "exit_desc": "Produce an exit signal when the 5-period EMA falls below the 20-period EMA.",
            },
            {
                "name": "Stochastic Reversal",
                "description": "Enter as Stochastic turns up from the oversold zone.",
                "entry_label": "Stoch Reversal Entry",
                "entry_desc": "If Stochastic K (14,3) turns up from below 20, long signal. K = (close-min14)/(max14-min14)*100.",
                "exit_label": "Stoch Overbought Exit",
                "exit_desc": "Produce an exit signal when Stochastic K rises above 80.",
            },
            {
                "name": "Donchian Channel Breakout",
                "description": "Breakout entry when price breaks the N-period high.",
                "entry_label": "Donchian Breakout Entry",
                "entry_desc": "When close breaks the maximum of the last 20 bars (Donchian upper channel), long signal.",
                "exit_label": "Donchian Lower Exit",
                "exit_desc": "Produce an exit signal when close falls below the minimum of the last 10 bars.",
            },
        ]
        # pick the least-used fallback according to used_concepts
        idx = 0
        if used_concepts:
            used_str = " ".join(used_concepts).lower()
            # compute a usage score for each fallback
            scores = []
            for idea in _FALLBACK_IDEAS:
                score = sum(
                    1
                    for kw in [idea["entry_label"].lower(), idea["name"].lower()]
                    if any(w in used_str for w in kw.split()[:2])
                )
                scores.append(score)
            idx = scores.index(min(scores))
        # Bozulma işareti fikirle birlikte taşınır: bu strateji modelin önerisi
        # DEĞİL, kaynak kodda sabit duran bir dizge. İşaretsiz bırakılırsa makul
        # bir yedek (ör. "RSI Oversold Reversal") sıralamada gerçek önerilerle
        # yarışır ve "kazanan" ilan edilir.
        fb = dict(_FALLBACK_IDEAS[idx])
        _tag_degraded(fb, e)
        return fb


if __name__ == "__main__":
    print(json.dumps(propose_strategy([]), indent=2))
    print("---")
    print(json.dumps(propose_composed_strategy([], []), indent=2))
