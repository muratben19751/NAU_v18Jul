"""Ortam değişkenlerinin TEK kataloğu — hangi düğmeler var, ne yaparlar, varsayılan ne.

DeepR 2026-08-11 [ORTA]: uygulamanın 77 ortam değişkeni var, beş farklı ön ekle
(``NAU_``, ``NAUTILUS_``, ``AGENT_``, ``STUDIO_``, sağlayıcı adları) ve 18 ayrı
dosyada okunuyorlar. Merkezî bir tanım olmadığı için:

- bir operatörün "bu uygulamayı neyle ayarlayabilirim" sorusunun cevabı yoktu;
  öğrenmenin tek yolu 18 dosyada ``os.environ.get`` aramaktı,
- varsayılanlar okuma yerinde gömülüydü, yani aynı değişken iki yerde farklı
  varsayılanla okunabilirdi (``NAUTILUS_LLM_CALL_TIMEOUT`` gerçekten iki yerde
  okunuyor) ve fark hiçbir yerde görünmezdi,
- ölü düğmeler birikiyordu: adı geçen ama artık hiçbir şeyi etkilemeyen bir
  değişkeni tespit etmenin yolu yoktu.

## Bu modülün duruşu

Okuma yerlerini toptan değiştirmiyoruz. Bunun sebebi kolaycılık değil ölçü:
değişkenlerin çoğu **import anında** modül-global'ine bağlanıyor ve testler tam
olarak o global'leri monkeypatch ediyor; okuma yolunu değiştirmek onlarca testin
sözleşmesini bozardı, karşılığında da davranışsal bir kazanç vermezdi.

Değişen şey KATALOĞUN VAR OLMASI ve **denetleniyor** olması:

- ``ENV_VARS`` her değişkeni adı, tipi, varsayılanı, okuyucusu ve tek cümlelik
  amacıyla listeler,
- ``python -m nau_config`` bunu operatöre basar — yürürlükteki değerlerle,
  sırlar maskeli,
- ``tests/test_env_registry_is_complete.py`` iki yönlü sürüklenmeyi kırmızıya
  çevirir: kodda okunup burada yazılmayan bir değişken de, burada yazılıp kodda
  hiç okunmayan bir değişken de testi düşürür.

Üçüncü madde asıl mekanizma. Bir katalog, güncel tutulması bir SÜREÇ ise çürür;
ihlal edildiğinde kırılan bir test ise durur. Aynı duruş
``tests/test_auto_layer_is_web_free.py`` ve ``tests/test_web_layer_has_no_cycle.py``
içinde katman sınırları için de var.

Wiki References: [[webapp_module_map]], [[surec_yoneticisi_ortami_dondurur]]
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Sırrı olan değişkenler: ``python -m nau_config`` değerlerini maskeler, yalnız
# "ayarlı / ayarlı değil" bilgisini gösterir.
SECRET = "secret"


@dataclass(frozen=True)
class EnvVar:
    name: str
    kind: str  # "str" | "int" | "float" | "bool" | "path" | "secret"
    default: str  # insan-okur varsayılan (kodda yazılı olanla aynı metin)
    read_in: str  # okumanın yaşadığı modül(ler)
    purpose: str  # tek cümle


def _v(name, kind, default, read_in, purpose) -> EnvVar:
    return EnvVar(name, kind, default, read_in, purpose)


# ── Depolama ve dağıtım ──────────────────────────────────────────────────────
_STORAGE = [
    _v(
        "NAU_DATA_DIR",
        "path",
        "~/.cache/nautilus_web_app",
        "app_constants",
        "Kalıcı veri kökü: katalog önbelleği, token defteri, oturum logları.",
    ),
    _v(
        "NAU_STUDIO_DB",
        "path",
        "<repo>/studio.db",
        "app_constants",
        "Strategy Studio'nun SQLite dosyası (varsayılanı kaynak ağacındadır).",
    ),
    _v(
        "NAUTILUS_EXTERNAL_CATALOGS",
        "path",
        "D:/NAU_ev/backend/data/catalog",
        "data",
        "Harici salt-okunur Nautilus katalog kökleri (';' ile ayrılmış).",
    ),
    _v(
        "NAUTILUS_INDEX_ROOT",
        "path",
        "(başka makinenin yolu)",
        "data",
        "US-Index günlük dosya kökü; ayarlı değilse o kaynak kapalıdır.",
    ),
    _v(
        "PM2_HOME",
        "str",
        "(pm2 tarafından set edilir)",
        "server, sandbox",
        "'Gerçekten dağıtıldık' işareti: erişim kapısı dosya yedeği ve pythonw spawn'ı buna bakar.",
    ),
]

# ── Erişim kapısı ve HTTP sertleştirme ───────────────────────────────────────
_HTTP = [
    _v(
        "NAU_ACCESS_TOKEN",
        SECRET,
        "",
        "server",
        "Tek operatörlü giriş kapısının paylaşılan sırrı; boşsa kapı kapalıdır.",
    ),
    _v(
        "NAU_ALLOWED_ORIGINS",
        "str",
        "",
        "server",
        "CSRF origin denetiminde ek olarak kabul edilen host'lar (virgüllü).",
    ),
    _v(
        "NAU_CSRF_REQUIRE_ORIGIN",
        "bool",
        "(kapalı)",
        "server",
        "Origin/Referer başlığı olmayan durum değiştiren istekleri de reddet.",
    ),
    _v(
        "NAU_MAX_REQUEST_BODY_BYTES",
        "int",
        "2097152",
        "server",
        "Uygulama geneli istek gövdesi tavanı (ASGI seviyesinde).",
    ),
]

# ── Kullanıcı kodu izolasyonu ────────────────────────────────────────────────
_SANDBOX = [
    _v(
        "NAU_USER_CODE_TIMEOUT_S",
        "int",
        "10",
        "sandbox",
        "Kullanıcı/LLM bloğunun duvar saati bütçesi (alt süreçte).",
    ),
    _v(
        "NAU_USER_CODE_MEMORY_MB",
        "int",
        "2048",
        "sandbox",
        "Alt sürecin bellek tavanı; ölçüldü: 512 çöküyor, 1024 geçiyor.",
    ),
    _v(
        "NAUTILUS_SPAWN_PYW",
        "bool",
        "1",
        "sandbox",
        "Windows'ta konsol penceresi açılmasın diye pythonw ile spawn et.",
    ),
]

# ── LLM: model seçimi, uç nokta, bütçe ───────────────────────────────────────
_LLM = [
    _v(
        "ANTHROPIC_API_KEY",
        SECRET,
        "",
        "llm_dispatch, strategy_studio.ai",
        "Anthropic API anahtarı.",
    ),
    _v(
        "ANTHROPIC_AUTH_TOKEN",
        SECRET,
        "",
        "llm_client",
        "CLI arka ucuna geçirilen alternatif kimlik.",
    ),
    _v(
        "ANTHROPIC_BASE_URL",
        "str",
        "(resmi API)",
        "llm_dispatch, strategy_studio.ai",
        "Anthropic uç noktası; boşsa resmi API. İki entegrasyon da AYNI değişkeni okur.",
    ),
    _v(
        "NAUTILUS_LLM_BACKEND",
        "str",
        "auto",
        "llm_dispatch",
        "sdk / cli / openrouter / auto.",
    ),
    _v(
        "NAUTILUS_LLM_MODEL",
        "str",
        "claude-fable-5",
        "llm_client",
        "Birincil model kimliği; bütün LLM yolları bunu çözer.",
    ),
    _v(
        "NAUTILUS_LLM_FALLBACK_MODEL",
        "str",
        "claude-opus-4-8",
        "llm_client",
        "Kredi tükenmesi/hata sonrası düşülecek model.",
    ),
    _v(
        "NAUTILUS_LLM_EFFORT",
        "str",
        "",
        "llm_client",
        "Muhakeme eforu ipucu (destekleyen modellerde).",
    ),
    _v(
        "NAUTILUS_MODEL_BY_PURPOSE",
        "str",
        "",
        "llm_client",
        "Amaç-başına model eşlemesi — koşu pinini AMAÇ bazında ezer. Biçim: "
        "'custom_block=claude-fable-5,narrative=or:qwen3.8-27b'. Amaçlar: "
        "composed, idea, custom_block, narrative, lab_idea. Hibrit koşu için: "
        "hacim işini ucuz uca, sözleşmesi katı işi güvenilir uca ver.",
    ),
    _v(
        "NAUTILUS_LLM_CALL_TIMEOUT",
        "int",
        "120",
        "llm_dispatch, openrouter_backend",
        "Tek bir LLM çağrısının duvar saati tavanı (saniye).",
    ),
    _v(
        "NAU_LLM_TRANSCRIPT",
        "bool",
        "1",
        "llm_dispatch",
        "AUTO koşularında prompt/yanıt metnini denetim artifact'ına yaz (0 = kapat).",
    ),
    _v(
        "NAU_LLM_TRANSCRIPT_CHARS",
        "int",
        "20000",
        "llm_dispatch",
        "Kaydedilen prompt/yanıt başına karakter tavanı; aşan metin ortadan kırpılır.",
    ),
    _v(
        "NAUTILUS_CLAUDE_CLI",
        "path",
        "",
        "llm_dispatch",
        "claude CLI ikilisinin yolu (otomatik bulunamazsa).",
    ),
    _v(
        "NAUTILUS_CLI_TIMEOUT",
        "int",
        "300",
        "llm_client",
        "CLI arka ucunun duvar saati tavanı (saniye).",
    ),
    _v(
        "NAUTILUS_STUDIO_LLM_MODEL",
        "str",
        "(ai.py varsayılanı)",
        "strategy_studio.ai",
        "Studio'nun kendi LLM istemcisinin modeli.",
    ),
    _v(
        "NAUTILUS_STUDIO_LLM_MAX_TOKENS",
        "int",
        "(ai.py varsayılanı)",
        "strategy_studio.ai",
        "Studio LLM çağrılarının çıktı tavanı.",
    ),
]

# ── OpenRouter arka ucu ──────────────────────────────────────────────────────
_OPENROUTER = [
    _v(
        "OPENROUTER_API_KEY",
        SECRET,
        "",
        "llm_client, openrouter_backend",
        "OpenRouter anahtarı.",
    ),
    _v(
        "OPENROUTER_BASE_URL",
        "str",
        "https://openrouter.ai/api/v1",
        "openrouter_backend",
        "OpenRouter uç noktası.",
    ),
    _v(
        "OPENROUTER_MODELS_URL",
        "str",
        "https://openrouter.ai/api/v1/models",
        "llm_client",
        "Canlı model kataloğunun adresi.",
    ),
    _v(
        "OPENROUTER_MODELS_TIMEOUT",
        "int",
        "6",
        "llm_client",
        "Model kataloğu çekiminin zaman aşımı (saniye).",
    ),
    _v(
        "OPENROUTER_HTTP_REFERER",
        "str",
        "",
        "openrouter_backend",
        "OpenRouter sıralamasında görünen referer başlığı.",
    ),
    _v(
        "OPENROUTER_X_TITLE",
        "str",
        "",
        "openrouter_backend",
        "OpenRouter sıralamasında görünen uygulama adı.",
    ),
    _v(
        "NAUTILUS_OPENROUTER_MODELS",
        "str",
        "",
        "llm_client",
        "Aday model listesini tamamen elle geçersiz kıl (virgüllü).",
    ),
    _v(
        "NAUTILUS_OPENROUTER_EXTRA_MODELS",
        "str",
        "",
        "llm_client",
        "Keşfedilen listeye eklenecek modeller.",
    ),
    _v(
        "NAUTILUS_OPENROUTER_FREE_ONLY",
        "bool",
        "",
        "llm_client",
        "Yalnız ücretsiz modelleri aday kabul et.",
    ),
    _v(
        "NAUTILUS_OPENROUTER_SDK_RETRIES",
        "int",
        "2",
        "openrouter_backend",
        "SDK seviyesindeki yeniden deneme sayısı.",
    ),
    _v(
        "NAUTILUS_OPENROUTER_429_MAX_WAIT",
        "int",
        "75",
        "openrouter_backend",
        "429 sonrası beklenecek azami süre (saniye).",
    ),
]

# ── AUTO ajanı ───────────────────────────────────────────────────────────────
_AGENT = [
    _v(
        "AGENT_MAX_CONCURRENT_RUNS",
        "int",
        "1",
        "web.routes.agent_backtest",
        "Aynı anda koşabilecek AUTO oturumu sayısı.",
    ),
    _v(
        "AGENT_MIN_TRADES",
        "int",
        "20",
        "web.routes.agent_backtest",
        "Bir sonucun aday sayılması için gereken asgari işlem sayısı.",
    ),
    _v(
        "AGENT_HOLDOUT_MIN_TRADES",
        "int",
        "20",
        "web.routes.agent_backtest",
        "Mühürlü holdout için asgari işlem sayısı.",
    ),
    _v(
        "AGENT_WINLESS_ROUND_LIMIT",
        "int",
        "3",
        "web.routes.agent_backtest",
        "Üst üste kazanansız tur sayısı; aşılınca oturum durur.",
    ),
    _v(
        "NAU_AUTO_HEARTBEAT_SEC",
        "float",
        "30",
        "web.routes.agent_backtest",
        "Canlı nabız aralığı (sn): olay üretilmeyen anlarda da koşunun tam hâli yazılır.",
    ),
    _v(
        "AGENT_DEFAULT_CONTINUOUS_MAX_HOURS",
        "int",
        "4",
        "web.routes.agent_backtest",
        "Sürekli mod için varsayılan saat bütçesi.",
    ),
    _v(
        "AGENT_DEFAULT_CONTINUOUS_MAX_TOKENS",
        "int",
        "250000",
        "web.routes.agent_backtest",
        "Sürekli mod için varsayılan token bütçesi.",
    ),
    _v(
        "AGENT_HARD_MAX_AUTO_HOURS",
        "int",
        "(varsayılan saat bütçesi)",
        "web.routes.agent_backtest",
        "Kullanıcının aşamayacağı saat tavanı.",
    ),
    _v(
        "AGENT_HARD_MAX_AUTO_TOKENS",
        "int",
        "(varsayılan token bütçesi)",
        "web.routes.agent_backtest",
        "Kullanıcının aşamayacağı token tavanı.",
    ),
    _v(
        "AGENT_EQUITY_PCT",
        "int",
        "95",
        "web.routes.agent_backtest",
        "Pozisyon boyutunun sermayeye oranı (%).",
    ),
    _v(
        "AGENT_NORMALIZE_EXTERNAL_EXPOSURE",
        "bool",
        "1",
        "web.routes.agent_backtest",
        "Harici enstrümanlarda maruziyeti normalize et.",
    ),
    _v(
        "AGENT_ALLOW_UNADJUSTED",
        "bool",
        "",
        "web.routes.agent_backtest",
        "Bölünme/temettü düzeltmesi olmayan seriyle koşmaya izin ver.",
    ),
    _v(
        "AGENT_RESEARCH_MODE",
        "bool",
        "",
        "web.routes.agent_backtest",
        "Araştırma modu: kapılar gevşer, sonuçlar dağıtıma uygun sayılmaz.",
    ),
    _v(
        "AGENT_CUSTOM_BLOCK_MAX_TOKENS",
        "int",
        "1800",
        "agent",
        "Custom block kod üretiminin çıktı tavanı.",
    ),
    _v(
        "AGENT_CUSTOM_BLOCK_TIMEOUT",
        "float",
        "75.0",
        "agent",
        "Custom block üretiminin duvar saati tavanı (saniye).",
    ),
    _v(
        "AGENT_CUSTOM_BLOCK_MAX_ATTEMPTS",
        "int",
        "2",
        "agent",
        "Üretim başarısızsa kaç kez yeniden denensin.",
    ),
    _v(
        "AGENT_CUSTOM_BLOCK_TOKEN_LIMIT",
        "int",
        "25000",
        "agent",
        "Custom block üretimine ayrılan toplam token bütçesi.",
    ),
]

# ── Strategy Studio ──────────────────────────────────────────────────────────
_STUDIO = [
    _v(
        "STUDIO_RUNNER",
        "str",
        "",
        "web.routes.strategy_studio",
        "'paper' değilse deployment'lar SİMÜLE edilir, hiçbir emir gönderilmez.",
    ),
    _v(
        "STUDIO_BACKTEST",
        "str",
        "",
        "web.routes.strategy_studio",
        "Studio backtest motorunun seçimi; ayarlı değilse stub motor kullanılır.",
    ),
    _v(
        "STUDIO_BACKTEST_OPT",
        "str",
        "",
        "web.routes.strategy_studio",
        "Optimizasyon koşularının motor seçimi.",
    ),
    _v(
        "STUDIO_OPT_MAX_ENGINE_RUNS",
        "int",
        "200",
        "web.routes.strategy_studio",
        "Tek bir optimizasyon işinin motor koşusu tavanı.",
    ),
]

# ── Backtest / WFO / paralellik ──────────────────────────────────────────────
_ENGINE = [
    _v(
        "NAUTILUS_PARALLEL",
        "bool",
        "1",
        "parallel_exec",
        "Robustness suite'ini çekirdeklere dağıt.",
    ),
    _v(
        "NAUTILUS_PARALLEL_WORKERS",
        "int",
        "(çekirdek sayısından türetilir)",
        "parallel_exec",
        "Robustness işçi süreçlerinin sayısı (bellek tavanıyla birlikte kırpılır).",
    ),
    _v(
        "NAUTILUS_WORKER_MEM_GB",
        "float",
        "1.5",
        "parallel_exec",
        "İşçi başına bellek tavanı; işçi sayısı buna göre kırpılır.",
    ),
    _v(
        "NAUTILUS_WFO_POP_SIZE",
        "int",
        "4",
        "wfo_optimizer",
        "Genetik algoritmanın popülasyon boyutu.",
    ),
    _v("NAUTILUS_WF_FOLDS", "int", "3", "wfo_optimizer", "Walk-forward fold sayısı."),
    _v(
        "NAUTILUS_WFO_TRADE_CONF_K",
        "int",
        "20",
        "wfo_optimizer",
        "Güven sönümleme sabiti: k = n / (n + K).",
    ),
    _v(
        "NAUTILUS_WF_MIN_VALID_FOLDS_FRAC",
        "float",
        "0.6",
        "wfo_optimizer",
        "Bir adayın geçerli sayılması için gereken fold oranı.",
    ),
    _v(
        "NAUTILUS_WF_EMBARGO_DAYS",
        "float",
        "2",
        "backtest_robustness",
        "Fold'lar arası ambargo penceresi (sızıntıyı kesmek için).",
    ),
    _v(
        "NAUTILUS_WFO_BATCH_TIMEOUT_S",
        "float",
        "600.0",
        "backtest_robustness",
        "Bir WFO partisinin duvar saati tavanı.",
    ),
    _v(
        "NAUTILUS_IS_SHARPE_MIN",
        "float",
        "0.05",
        "backtest_robustness",
        "Örneklem-içi asgari Sharpe eşiği.",
    ),
    _v(
        "NAU_BENCHMARK_RT_COST",
        "float",
        "0.0002",
        "app_constants",
        "Benchmark'ın gidiş-dönüş işlem maliyeti (kesir).",
    ),
    _v(
        "NAU_BENCHMARK_DIV_YIELD",
        "float",
        "0.0",
        "app_constants",
        "Benchmark'a eklenen yıllık temettü getirisi.",
    ),
    _v("NAUTILUS_DEBUG_LOG", "bool", "", "backtest", "Motorun ayrıntılı log'unu aç."),
]

# ── /sessions paneli (okuma maliyeti sınırları) ──────────────────────────────
_SESSIONS = [
    _v(
        "NAUTILUS_SESSION_SUMMARY_CONCURRENCY",
        "int",
        "8",
        "web.routes.sessions",
        "Oturum özetlerinin eşzamanlı okuma sayısı.",
    ),
    _v(
        "NAUTILUS_SESSION_SUMMARY_FULL_SCAN_MAX_BYTES",
        "int",
        "8388608",
        "web.routes.sessions",
        "Özet için tam taranacak azami JSONL boyutu.",
    ),
    _v(
        "NAUTILUS_SESSION_DETAIL_FULL_SCAN_MAX_BYTES",
        "int",
        "67108864",
        "web.routes.sessions",
        "Detay için tam taranacak azami JSONL boyutu.",
    ),
    _v(
        "NAUTILUS_SESSION_DETAIL_CACHE_MAX_ENTRIES",
        "int",
        "8",
        "web.routes.sessions",
        "Detay önbelleğinde tutulacak oturum sayısı.",
    ),
    _v(
        "NAUTILUS_SESSION_DETAIL_CACHE_MAX_BYTES",
        "int",
        "25165824",
        "web.routes.sessions",
        "Detay önbelleğinin toplam bellek tavanı.",
    ),
]

# ── Veri indirme (ETL script'leri) ───────────────────────────────────────────
_ETL = [
    _v(
        "MASSIVE_API_KEY",
        SECRET,
        "",
        "download_massive, download_grouped_daily",
        "Polygon/Massive REST anahtarı (POLYGON_API_KEY ile aynı rolü paylaşır).",
    ),
    _v(
        "POLYGON_API_KEY",
        SECRET,
        "",
        "download_massive, download_grouped_daily",
        "MASSIVE_API_KEY yoksa kullanılan alternatif ad.",
    ),
    _v(
        "MASSIVE_S3_KEY",
        SECRET,
        "",
        "download_flatfiles",
        "Flat-file S3 erişim anahtarı.",
    ),
    _v(
        "MASSIVE_S3_SECRET",
        SECRET,
        "",
        "download_flatfiles",
        "Flat-file S3 gizli anahtarı.",
    ),
]

ENV_VARS: tuple[EnvVar, ...] = tuple(
    _STORAGE
    + _HTTP
    + _SANDBOX
    + _LLM
    + _OPENROUTER
    + _AGENT
    + _STUDIO
    + _ENGINE
    + _SESSIONS
    + _ETL
)

BY_NAME: dict[str, EnvVar] = {v.name: v for v in ENV_VARS}

GROUPS: tuple[tuple[str, tuple[EnvVar, ...]], ...] = (
    ("Depolama ve dağıtım", tuple(_STORAGE)),
    ("Erişim kapısı / HTTP", tuple(_HTTP)),
    ("Kullanıcı kodu izolasyonu", tuple(_SANDBOX)),
    ("LLM", tuple(_LLM)),
    ("OpenRouter", tuple(_OPENROUTER)),
    ("AUTO ajanı", tuple(_AGENT)),
    ("Strategy Studio", tuple(_STUDIO)),
    ("Backtest / WFO / paralellik", tuple(_ENGINE)),
    ("/sessions paneli", tuple(_SESSIONS)),
    ("Veri indirme (ETL)", tuple(_ETL)),
)


def effective(name: str, env: dict | None = None) -> str:
    """Yürürlükteki değer — sır ise maskeli, ayarlı değilse boş string."""
    env = env if env is not None else os.environ
    raw = env.get(name)
    if raw is None or raw == "":
        return ""
    if BY_NAME.get(name) and BY_NAME[name].kind == SECRET:
        return f"<ayarlı, {len(raw)} karakter>"
    return raw


def render_table(env: dict | None = None) -> str:
    """Operatöre gösterilecek düz metin tablo."""
    out: list[str] = []
    for title, group in GROUPS:
        out.append(f"\n── {title} " + "─" * max(0, 60 - len(title)))
        for v in group:
            cur = effective(v.name, env)
            mark = "●" if cur else " "
            out.append(f" {mark} {v.name}")
            out.append(f"     {v.purpose}")
            out.append(
                f"     varsayılan: {v.default or '(boş)'}"
                + (f"   ·   yürürlükte: {cur}" if cur else "")
            )
    out.append(
        f"\n{len(ENV_VARS)} değişken · ● = bu süreçte ayarlı. "
        "Çoğu import anında okunur; değiştirdikten sonra sunucuyu yeniden başlatın."
    )
    return "\n".join(out)


if __name__ == "__main__":  # pragma: no cover
    import sys

    # Windows konsolu cp1252/cp1254 varsayar ve tablodaki kutu çizgileri ile
    # Türkçe harflerde UnicodeEncodeError atar. `server.py` açılışta aynı şeyi
    # yapıyor; burada da yapmazsak katalog kendi makinesinde basılamaz.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    print(render_table())
