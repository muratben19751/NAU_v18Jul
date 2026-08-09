"""LLM client selection: model/effort pins, OpenRouter catalog.

agent.py decomposition (Adım 2, safe-first slice): extracted verbatim from
agent.py's Domain B (model/effort/OpenRouter-catalog selection). Self-
contained — nothing here depends on anything else in agent.py.

`model_unavailable_reason` stayed BEHIND in agent.py rather than moving here
with its former neighbors: it calls `_get_openrouter_client()`, which is
still part of agent.py's not-yet-extracted transport/dispatch layer (a
later, separate, more careful session — see the decomposition plan). Moving
it here would have made this module reach back into agent.py, exactly the
reverse-dependency direction the decomposition is meant to avoid.

Wiki References
---------------
See: [[model_secici_ve_gorunurluk]], [[llm_maliyet_kaldiraclari]].
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

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
