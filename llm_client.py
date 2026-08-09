"""LLM client selection: model/effort pins, OpenRouter catalog, control
plane, Claude Code CLI backend.

agent.py decomposition (Adım 2-4, safe-first slice): extracted verbatim from
agent.py's Domain B (model/effort/OpenRouter-catalog selection, Adım 2),
Domain A (cancellation/budget-admission/telemetry/degradation-tagging
control plane, Adım 3, landed after B since _observe_llm/_llm_request_token_bound
call current_model()), and the Claude CLI backend (Adım 4, landed after A
since _ClaudeCLIMessages.create needs _LLM_CONTROL + current_effort()).
`_poll_until_deadline` moved with the CLI backend even though it's ALSO
used by the (not-yet-extracted) OpenRouter multiprocessing backend in
agent.py — it only depends on `_check_llm_cancelled` (already here since
Adım 3) and stdlib, so the OpenRouter caller reaches it through the
re-export with no new coupling either direction.

`model_unavailable_reason` and `_interruptible_sleep` stayed BEHIND in
agent.py rather than moving here with their former neighbors:
`model_unavailable_reason` calls `_get_openrouter_client()`, `_interruptible_sleep`
calls `_sleep` (the patchable `time.sleep` alias used by OpenRouter's 429
backoff) — both still part of agent.py's not-yet-extracted transport/dispatch
layer (a later, separate, more careful session — see the decomposition
plan). Moving either here would have made this module reach back into
agent.py, exactly the reverse-dependency direction the decomposition is
meant to avoid.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]], [[llm_maliyet_kaldiraclari]], [[kesilme_ve_degrade_gorunurlugu]].
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import tempfile
import threading
import time
from typing import Any

from app_constants import NO_WINDOW_FLAGS

MODEL = os.environ.get("NAUTILUS_LLM_MODEL", "claude-fable-5")
# Model to automatically fall back to if Fable credit/quota runs out. Opus 4.8
# offers the same 1M context and API surface (adaptive thinking, no sampling
# params), at a lower per-token price → call bodies run unchanged.
FALLBACK_MODEL = os.environ.get("NAUTILUS_LLM_FALLBACK_MODEL", "claude-opus-4-8")

# Once credit is exhausted, lock to FALLBACK_MODEL for the process lifetime (so we
# don't get a 403 on every call). None = not yet fallen back, MODEL is in use.
_active_model: str | None = None
_model_lock = threading.Lock()

# Per-THREAD model override — the AUTO loop's model picker. Thread-local on
# purpose: a loop run pins only its own worker thread's LLM calls (idea /
# custom blocks / narratives all run there); concurrent surfaces (chat, PRO
# describe) keep the app default. The credit-exhaustion fallback above still
# wins — that is a billing fact, not a preference.
_MODEL_OVERRIDE = threading.local()

SELECTABLE_MODELS = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)

# Ekranlarda model adını yazmak için — id'ler kullanıcıya gösterilecek kadar
# okunur değil ("claude-fable-5" → "Fable 5").
_MODEL_LABELS = {
    "claude-fable-5": "Fable 5",
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5": "Haiku 4.5",
}


def set_thread_model(model: str | None) -> None:
    """Pin THIS thread's LLM calls to ``model``; None/unknown clears the pin.

    ``or:<openrouter-id>`` biçimi de geçerlidir — o thread'in çağrıları
    varsayılan backend yerine OpenRouter'a yönlenir (bkz. _create_message).
    """
    ok = model in SELECTABLE_MODELS or (
        isinstance(model, str) and model.startswith("or:") and len(model) > 3
    )
    _MODEL_OVERRIDE.model = model if ok else None


# ── Effort (düşünme bütçesi) ───────────────────────────────────────────────
# Modelden AYRI bir maliyet kolu: aynı modelde `low` ↔ varsayılan farkı
# sonnet-5'te ölçülen −52% (fable'da −11% — getirisi modele bağlı, ölçmeden
# varsayma). Model pini gibi thread-local: LLM çağrısı koşuyu yürüten worker
# thread'inde yapılır, dolayısıyla pin'i okuyan yer onu yazan yerle aynıdır.
# (Maliyet ROZETİ başka thread'den okunduğu için aynı deseni oraya taşımayın —
# bkz. _llm_cost_usd'nin thread-local tuzağı.)
_EFFORT_OVERRIDE = threading.local()

# Claude CLI'ın `--effort <level>` sözlüğü. "" = bayrağı hiç geçme (CLI kendi
# varsayılanını kullanır) — "varsayılan"ı bir seviye adıyla taklit etmek yanlış
# olurdu, CLI'ın varsayılanı sürümle değişebilir.
SELECTABLE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Süreç geneli varsayılan. Thread pin'i koşuyla birlikte ölür; kalıcı bir
# tercih isteniyorsa kapsam burasıdır (model tarafındaki NAUTILUS_LLM_MODEL'in
# kardeşi).
EFFORT = os.environ.get("NAUTILUS_LLM_EFFORT", "").strip().lower()
if EFFORT and EFFORT not in SELECTABLE_EFFORTS:
    logging.warning("NAUTILUS_LLM_EFFORT=%r geçersiz — yok sayıldı", EFFORT)
    EFFORT = ""


def resolve_effort(effort: str | None) -> str:
    """Bir effort girdisinin GERÇEKTEN uygulanacak hâli ("" = bayrak geçilmez).

    Tek doğruluk kaynağı: hem thread pini hem de kokpitin gösterdiği değer
    buradan geçer. Ayrı ayrı çözülselerdi ikisi ayrışırdı — form "uydurma"
    gönderdiğinde pin "" olur ama ekran "uydurma" yazardı, yani arayüz
    koşmayan bir ayarı koşuyor gibi gösterirdi.

    Tip kontrolü şart: setter koşunun İLK adımında, worker thread'inin içinde
    çağrılıyor; `.strip()`'i korumasız bırakmak form dışından gelen bir int'te
    AttributeError fırlatıp koşuyu daha başlamadan öldürürdü.
    """
    e = effort.strip().lower() if isinstance(effort, str) else ""
    return e if e in SELECTABLE_EFFORTS else ""


def set_thread_effort(effort: str | None) -> None:
    """Pin THIS thread's effort level; geçersiz/str-olmayan pin'i temizler."""
    _EFFORT_OVERRIDE.effort = resolve_effort(effort)


def current_effort() -> str:
    """Bu thread'in effort seviyesi — pin yoksa süreç varsayılanı, o da yoksa ""."""
    return getattr(_EFFORT_OVERRIDE, "effort", "") or EFFORT


# Model seçici seçenekleri. OpenRouter girdileri yalnız OPENROUTER_API_KEY
# ayarlıyken görünür (anahtarsız seçim zaten çalışmaz); liste canlı olarak
# openrouter.ai kataloğundan gelir. Fiyat tablosunda olmayan modellerin
# maliyeti rozet/defterde dürüstçe "?" kalır (uydurma sayı yok).
#
# Varsayılan: yalnız ÜCRETSİZ uçlar listelenir (openrouter.ai fiyatlandırmasında
# girdi ve çıktı 0). NAUTILUS_OPENROUTER_FREE_ONLY=0 tüm kataloğu geri getirir.
_DEFAULT_OPENROUTER_MODELS = (
    "deepseek/deepseek-chat,google/gemini-2.5-flash,openai/gpt-4o-mini"
)
# Katalog çekilemediğinde free-only modun yedeği. Yedek olarak PARALI bir id'ye
# ASLA düşülmez: "ücretsiz" diye seçilen bir model sessizce fatura yazmasın.
_DEFAULT_OPENROUTER_FREE_MODELS = (
    "openrouter/free,openai/gpt-oss-20b:free,google/gemma-4-31b-it:free"
)

# Canlı katalog. Süreç içi cache — bir sayfa render'ı ağ turuna beklememeli.
# Başarısız çekim de (kısa süre) cache'lenir ki kapalı/engelli bir uç nokta her
# render'ı yavaşlatmasın; o durumda liste yukarıdaki statik üçlüye düşer —
# uydurma id'ye ASLA değil.
OPENROUTER_MODELS_URL = os.environ.get(
    "OPENROUTER_MODELS_URL", "https://openrouter.ai/api/v1/models"
)
_OR_CATALOG_TTL = 3600.0
_OR_CATALOG_FAIL_TTL = 60.0
_or_catalog: list[tuple[str, str, bool]] = []
_or_catalog_at = 0.0
_or_catalog_lock = threading.Lock()


def _is_free(pricing: dict) -> bool:
    """openrouter.ai fiyatlandırmasına göre uç ücretsiz mi?

    Fiyatlar string gelir ("0", "0.00000009"). Eksik/ayrıştırılamayan alan
    ücretsiz SAYILMAZ — bilinmeyen fiyat paralı varsayılır ki free-only liste
    yanlışlıkla fatura yazan bir seçenek göstermesin.
    """
    try:
        return all(float(pricing[k]) == 0.0 for k in ("prompt", "completion"))
    except (KeyError, TypeError, ValueError):
        return False


def _fetch_openrouter_catalog() -> list[tuple[str, str, bool]]:
    """openrouter.ai metin modelleri — [(id, görünen ad, ücretsiz mi)].

    Ücretsizlik bayrağı burada, ham fiyattan hesaplanır ve cache'e girer; böylece
    free-only anahtarını çevirmek yeni bir ağ turu gerektirmez.
    """
    import urllib.request

    req = urllib.request.Request(
        OPENROUTER_MODELS_URL, headers={"Accept": "application/json"}
    )
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        # Uç nokta anahtarsız da çalışır; anahtarla hesabın gördüğü liste gelir.
        req.add_header("Authorization", f"Bearer {key}")
    timeout = float(os.environ.get("OPENROUTER_MODELS_TIMEOUT", "6"))
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        payload = json.loads(r.read().decode("utf-8"))

    out: list[tuple[str, str, bool]] = []
    for m in payload.get("data") or []:
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        arch = m.get("architecture") or {}
        # Metin girip metin çıkarabilen uçlar; görüntü/ses-only modeller bu
        # uygulamanın promptlarını çalıştıramaz.
        ins = arch.get("input_modalities") or ["text"]
        outs = arch.get("output_modalities") or ["text"]
        if "text" not in ins or "text" not in outs:
            continue
        out.append(
            (mid, (m.get("name") or mid).strip(), _is_free(m.get("pricing") or {}))
        )
    out.sort(key=lambda t: t[0])
    return out


def openrouter_catalog(force: bool = False) -> list[tuple[str, str, bool]]:
    """Cache'li openrouter.ai kataloğu; çekim başarısızsa boş liste."""
    global _or_catalog, _or_catalog_at
    now = time.monotonic()
    with _or_catalog_lock:
        ttl = _OR_CATALOG_TTL if _or_catalog else _OR_CATALOG_FAIL_TTL
        if not force and _or_catalog_at and (now - _or_catalog_at) < ttl:
            return _or_catalog
        try:
            _or_catalog = _fetch_openrouter_catalog()
            logging.info("OpenRouter kataloğu: %d model", len(_or_catalog))
        except Exception as e:
            logging.warning("OpenRouter kataloğu çekilemedi: %s", e)
            _or_catalog = []
        _or_catalog_at = now
        return _or_catalog


def openrouter_free_only() -> bool:
    """Picker yalnız ücretsiz OpenRouter uçlarını mı listeler? (varsayılan: evet)

    NAUTILUS_OPENROUTER_FREE_ONLY=0 (veya false/no/off) tüm kataloğu açar.
    """
    raw = os.environ.get("NAUTILUS_OPENROUTER_FREE_ONLY", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def openrouter_extra_models() -> list[str]:
    """Ücretsiz filtresine RAĞMEN listelenecek id'ler (NAUTILUS_OPENROUTER_EXTRA_MODELS).

    "Ücretsizler + şu paralı uç" senaryosu için. ``NAUTILUS_OPENROUTER_MODELS``
    pin'inden farkı: pin listenin YERİNE geçer ve ağa çıkmaz; bu ise listeye
    EKLENİR, adını canlı katalogdan alır ve etiketinde paralı olduğu yazar.
    """
    raw = os.environ.get("NAUTILUS_OPENROUTER_EXTRA_MODELS", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def openrouter_paid_extras() -> list[str]:
    """Ücretsiz listeye elle eklenmiş PARALI satırların picker değerleri.

    Arayüz bunları ayrı bir optgroup'ta gösterir: "ücretsiz (17)" başlıklı bir
    grubun içinde paralı bir satır durursa başlık yalan söyler. Tüm-katalog
    modunda ayrım anlamsız (zaten her şey listede) — boş döner.
    """
    if not openrouter_free_only():
        return []
    free_by_id = {mid: free for mid, _, free in openrouter_catalog()}
    return [
        f"or:{m}" for m in openrouter_extra_models() if not free_by_id.get(m, False)
    ]


def _or_row(mid: str, name: str, is_free: bool) -> tuple[str, str]:
    """Bir picker satırı; paralı uç etiketinde bunu SÖYLER.

    Rozet düz `<select>`lerde (optgroup'suz yüzeyler) tek ayrım işareti — ücretsiz
    sanılan bir seçim fatura yazmasın.
    """
    return (f"or:{mid}", f"OR · {name}" + ("" if is_free else " · paralı"))


def _openrouter_options() -> list[tuple[str, str]]:
    """OpenRouter picker satırları — canlı katalog, yoksa statik yedek.

    Varsayılanda yalnız ücretsiz uçlar listelenir (bkz. ``openrouter_free_only``);
    ``NAUTILUS_OPENROUTER_EXTRA_MODELS`` ile açıkça izin verilenler bunlara eklenir.
    NAUTILUS_OPENROUTER_MODELS (virgüllü id listesi) verilmişse liste ona
    sabitlenir, ağa hiç çıkılmaz ve — açık bir tercih olduğu için — ücretsiz
    filtresinden geçmez.
    """
    pinned = os.environ.get("NAUTILUS_OPENROUTER_MODELS", "").strip()
    if pinned:
        rows = [(s.strip(), s.strip()) for s in pinned.split(",") if s.strip()]
        return [(f"or:{mid}", f"OR · {name}") for mid, name in rows]

    free_only = openrouter_free_only()
    catalog = openrouter_catalog()
    rows = [(mid, name, free) for mid, name, free in catalog if free or not free_only]
    if not rows:
        fallback = (
            _DEFAULT_OPENROUTER_FREE_MODELS if free_only else _DEFAULT_OPENROUTER_MODELS
        )
        # Yedek listenin ücretsizliği free-only modda kurgu gereği doğru; tüm-katalog
        # modunda eski üçlü paralıdır ve etiketi bunu yazmalı.
        rows = [(s, s, free_only) for s in fallback.split(",")]

    out = [_or_row(mid, name, free) for mid, name, free in rows]

    # Açıkça izin verilenler. Katalogda yoksa ham id ile yine de gösterilir:
    # sessizce düşürmek, kullanıcının yazdığı bir id'yi yok saymak olurdu.
    listed = {mid for mid, _, _ in rows}
    by_id = {mid: (name, free) for mid, name, free in catalog}
    for mid in openrouter_extra_models():
        if mid in listed:
            continue
        listed.add(mid)
        name, free = by_id.get(mid, (mid, False))
        out.append(_or_row(mid, name, free))
    return out


def selectable_models() -> list[tuple[str, str]]:
    """Model picker options as (form value, label); "" = app default."""
    out = [
        ("", f"{_MODEL_LABELS.get(MODEL, MODEL)} (varsayılan)"),
        ("claude-opus-5", "Opus 5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-haiku-4-5", "Haiku 4.5"),
    ]
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        out.extend(_openrouter_options())
    return out


def model_label(model: str | None = None) -> str:
    """Bir model id'sinin / picker değerinin okunur adı.

    "" veya None = "uygulama varsayılanı": çözülüp gerçek model adı döner, ki
    arayüzdeki rozet "default" gibi bir kelime yerine hep gerçek bir model
    adlandırsın (kredi fallback'i devredeyse onu — bu bir fatura gerçeği).
    """
    m = (model or "").strip() or current_model()
    if m.startswith("or:"):
        # Hangi hesabın faturalandığı adın parçası — OpenRouter ayrı bir hesap.
        return f"OR · {m[3:]}"
    return _MODEL_LABELS.get(m, m)


def model_id(model: str | None = None) -> str:
    """``model_label`` ile aynı çözüm, ama ham id ("or:" öneki korunur)."""
    return (model or "").strip() or current_model()


def current_model() -> str:
    """The model currently in use (FALLBACK_MODEL if fallback has kicked in).

    The credit fallback is a BILLING fact, so it outranks a preference — but
    only inside its own billing domain. `_active_model` is set when *Claude*
    credit runs out; an "or:" pin targets OpenRouter, which is a separate
    account and unaffected by that. Letting the global flag win there broke the
    feature exactly when it is needed ("Claude is out of credit, switch to
    OpenRouter") and sent the call straight back into the same wall.
    """
    pin = getattr(_MODEL_OVERRIDE, "model", None)
    if pin and pin.startswith("or:"):
        return pin
    if _active_model:
        return _active_model
    return pin or MODEL


# ── Control plane: cancellation / telemetry / budget-admission / degradation
# tagging (agent.py decomposition, Adım 3) ──────────────────────────────────


class TerminalLLMError(RuntimeError):
    """Non-retryable provider failure (credit/auth/permission)."""


class LLMCallCancelled(RuntimeError):
    """The owning AUTO run requested stop while an LLM call was in flight."""


class LLMTokenBudgetExceeded(RuntimeError):
    """A local caller budget rejected an LLM provider attempt before billing."""


_LLM_CONTROL = threading.local()

# The Claude CLI deliberately has no ``max_tokens`` flag.  For that backend a
# configured output ceiling is therefore a request *hint*, not a billing cap.
# Keep the largest observed output per (actual model, purpose) and reserve it
# on the next attempt. This is intentionally conservative, but not advertised
# as a hard provider-side limit.
_OUTPUT_RESERVATION_LOCK = threading.Lock()
_OBSERVED_OUTPUT_HIGH_WATER: dict[tuple[str, str], int] = {}


def set_thread_llm_control(cancel_check=None, observer=None, admit_check=None) -> None:
    """Install AUTO-scoped cancellation, telemetry and budget admission hooks."""
    _LLM_CONTROL.cancel_check = cancel_check
    _LLM_CONTROL.observer = observer
    _LLM_CONTROL.admit_check = admit_check


def _check_llm_cancelled() -> None:
    check = getattr(_LLM_CONTROL, "cancel_check", None)
    if callable(check) and check():
        raise LLMCallCancelled("AUTO stop requested")


def _observe_llm(**event) -> None:
    usage = event.get("usage") or {}
    if isinstance(usage, dict):
        try:
            observed_output = max(0, int(usage.get("output_tokens") or 0))
        except (TypeError, ValueError):
            observed_output = 0
        if observed_output:
            key = (
                str(event.get("model") or current_model() or "unknown"),
                str(event.get("purpose") or "llm"),
            )
            with _OUTPUT_RESERVATION_LOCK:
                _OBSERVED_OUTPUT_HIGH_WATER[key] = max(
                    observed_output, _OBSERVED_OUTPUT_HIGH_WATER.get(key, 0)
                )
    observer = getattr(_LLM_CONTROL, "observer", None)
    if callable(observer):
        try:
            observer(event)
        except Exception:
            logging.exception("LLM observer failed")


def _output_cap_telemetry(
    client,
    usage: dict,
    max_tokens: int,
    *,
    provider_enforced: bool = False,
) -> dict[str, int | str | bool]:
    """Describe whether an output ceiling was enforced or merely requested.

    Claude Code's CLI accepts no output-token switch.  Without this explicit
    distinction the session log made a 4k request look like a 4k hard cap even
    after the CLI returned more than 4k tokens.  Keep the proof alongside each
    usage event so budget reviews do not have to infer it from implementation
    details.
    """
    requested = max(0, int(max_tokens or 0))
    output = max(0, int(usage.get("output_tokens") or 0))
    # A thread may be pinned to OpenRouter while the process-default client is
    # still the Claude CLI.  The transport, not that cached default client,
    # determines whether ``max_tokens`` is a hard provider limit.
    is_cli = isinstance(client, _ClaudeCLIClient) and not provider_enforced
    exceeded = bool(is_cli and requested and output > requested)
    return {
        "output_cap_mode": "advisory_cli" if is_cli else "provider_enforced",
        "output_cap_requested": requested,
        "output_cap_exceeded": exceeded,
        "output_cap_excess_tokens": max(0, output - requested) if exceeded else 0,
    }


def _llm_request_token_bound(
    kwargs: dict, *, model: str = "", purpose: str = ""
) -> dict[str, int | str]:
    """Conservative token upper bound used before an LLM request is admitted.

    UTF-8 bytes are a safe upper bound for BPE-style input tokenization and do
    not require a model-specific tokenizer. For API-backed models the output
    side is bounded by ``max_tokens``. Claude CLI has no equivalent switch, so
    its reservation is the configured hint raised to the observed high-water
    mark for this model/purpose. This is a conservative admission estimate,
    not a provider-enforced hard cap.
    """

    payloads: list[str] = []
    system = kwargs.get("system")
    if isinstance(system, str):
        payloads.append(system)
    for message in kwargs.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            payloads.append(content)
    input_bound = sum(len(text.encode("utf-8")) for text in payloads)
    # Account for role/message framing without pretending to know the provider's
    # exact chat template.
    input_bound += 64 * (len(payloads) + 1)
    configured_output = max(0, int(kwargs.get("max_tokens") or 0))
    key = (
        str(model or current_model() or "unknown"),
        str(purpose or "llm"),
    )
    with _OUTPUT_RESERVATION_LOCK:
        observed_output = _OBSERVED_OUTPUT_HIGH_WATER.get(key, 0)
    output_bound = max(configured_output, observed_output)
    return {
        "input_token_bound": input_bound,
        "output_token_bound": output_bound,
        "total_token_bound": input_bound + output_bound,
        "configured_output_tokens": configured_output,
        "observed_output_reserve": observed_output,
        "output_reservation_mode": (
            "observed_high_water"
            if observed_output > configured_output
            else "configured_hint"
        ),
    }


def _admit_llm_request(kwargs: dict, *, model: str = "", purpose: str = "") -> None:
    admit = getattr(_LLM_CONTROL, "admit_check", None)
    if callable(admit):
        admit(_llm_request_token_bound(kwargs, model=model, purpose=purpose))


def _raise_if_llm_control_abort(exc: BaseException) -> None:
    """Never disguise STOP/budget control flow as a successful fallback."""

    if isinstance(exc, LLMCallCancelled) or bool(
        getattr(exc, "llm_control_abort", False)
    ):
        raise exc


def is_terminal_llm_error(exc: BaseException) -> bool:
    """Return True when fallback/retry cannot possibly heal the provider call.

    OpenAI-compatible SDKs do not share one exception class, so inspect the
    stable HTTP status plus a conservative message fallback. Rate limits (429),
    timeouts and 5xx responses remain retryable; credit/auth failures do not.
    """

    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status in (401, 402, 403):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "insufficient credit",
        "insufficient_credit",
        "payment required",
        "invalid api key",
        "authentication failed",
        "not authorized",
        "permission denied",
    )
    return any(marker in text for marker in markers)


def _tag_degraded(fb: dict, e: BaseException) -> dict:
    """Stamp a fallback dict with a machine-readable degradation marker, in place.

    Shared by every "LLM call failed, hand back a fallback instead" path so
    the degraded-fallback contract (which fields exist, truncation length,
    terminal-error flag) is defined once instead of drifting between copies.
    """
    fb["degraded"] = type(e).__name__
    fb["degraded_detail"] = str(e)[:500]
    fb["degraded_terminal"] = is_terminal_llm_error(e)
    return fb


_random_ctx = threading.local()


def set_thread_random_seed(seed: str | int | None) -> None:
    """Pin deterministic fallback exploration to the current AUTO worker."""

    _random_ctx.rng = random.Random(str(seed)) if seed is not None else None


def _fallback_rng():
    return getattr(_random_ctx, "rng", None) or random


# ── Claude Code CLI backend (subscription / OAuth — no ANTHROPIC_API_KEY
# needed; agent.py decomposition, Adım 4) ───────────────────────────────────
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
