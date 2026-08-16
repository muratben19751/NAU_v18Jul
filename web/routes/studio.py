"""Unified Strategy Studio — merges the Composer (/strategy) and Backtest
(/backtest) pages into one tabbed page at ``/studio``.

Wiki References
---------------
See: [[strategy_and_actor]], [[backtesting_guide]], [[webapp_module_map]],
[[auto_mission_control]] (AUTO sekmesinin kokpit yüzeyi),
[[model_secici_ve_gorunurluk]] (model picker + rozet)

The merged Compose + Backtest surface. This module owns only the ``GET /studio``
entry point that renders ``studio.html`` with the UNION of both pages' contexts;
every HTMX action still posts to the existing ``/strategy/*`` and ``/backtest/*``
endpoints (unchanged). The single durable link between the two halves is
``strategy_catalog.json`` keyed by ``spec_id`` — the catalog picker in the
Backtest tab finally makes the Composer→Backtest handoff live (the old
``preferred_spec_id`` deep-link was computed but never rendered).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from composer import BLOCK_CATALOG, load_catalog
from data import BYBIT_ALL_INTERVALS, BYBIT_CATEGORIES, BYBIT_SYMBOLS
from web.shared import session_id, set_session_cookie
from web.templating import get_market_info, templates

router = APIRouter(prefix="/studio")

# ── AUTO brief'inin açılış varsayılanı ────────────────────────────────────
# Kokpit brief'i boş bir formla değil, kullanıcının fiilen koşturduğu ayarlarla
# açılır. Sabit alanların varsayılanı studio.html'de (formun kendi
# `selected`/`value`'ları); model ve sembol burada durur çünkü ikisinin de
# seçenek listesi çalışma anında oluşuyor ve seçili değerin listede OLDUĞU
# doğrulanmalı.
AUTO_DEFAULT_MODEL = "or:qwen3.8-27b"

# Aynı brief'in enstrümanı. Noktalı id = dış katalog; liste diskten CANLI
# tarandığı için (bkz. _mc_external_symbols) katalog başka bir makinede/boşsa
# seçenek hiç render edilmez — o durumda ilk statik satıra (BTCUSDT) düşeriz,
# yoksa hiçbir seçeneği işaretli olmayan bir kutu kalırdı.
AUTO_DEFAULT_SYMBOL = "QQQC.NASDAQ"

# Aynı brief'in düşünme bütçesi. "" = ucun kendi varsayılanı; seviye listesi
# çalışma anında oluşmadığı için (sabit sözlük) burada doğrulamaya gerek yok.
# Varsayılanı "" bırakıyoruz: effort'un getirisi modele bağlı (sonnet-5'te
# −52%, fable'da −11%) ve seçilmemiş bir kutu sessizce ucuzlatma yapmamalı.
AUTO_DEFAULT_EFFORT = ""

# ── Simple-mode wizard: ready-made strategy templates ──────────────────────
# Each template's ``description`` is a natural-language brief that feeds the
# existing ``POST /backtest/describe`` flow (Claude turns it into signal
# blocks). ``blurb`` is the plain-English card text shown to the user.
STRATEGY_TEMPLATES: list[dict] = [
    {
        "id": "trend_following",
        "icon": "📈",
        "title": "Trend Following",
        "blurb": (
            "Rides established trends: buys when a fast average crosses above "
            "a slow one, exits when the trend bends back. Works best in "
            "trending markets; struggles in sideways chop."
        ),
        "description": (
            "BUY when the EMA(20) crosses above the EMA(50) and the close is "
            "above the SMA(200). EXIT the position when the EMA(20) crosses "
            "back below the EMA(50). Use an ATR(14) based stop loss of 3x ATR."
        ),
    },
    {
        "id": "mean_reversion",
        "icon": "🔄",
        "title": "Mean Reversion (RSI)",
        "blurb": (
            "Buys short-term dips and sells the bounce using RSI oversold/"
            "overbought levels. Works best in ranging markets; risky in "
            "strong downtrends."
        ),
        "description": (
            "BUY when RSI(14) drops below 30 and then crosses back above 30. "
            "EXIT when RSI(14) rises above 70 and then crosses back below 70. "
            "Only take BUY signals while the close is above the SMA(200)."
        ),
    },
    {
        "id": "breakout",
        "icon": "🚀",
        "title": "Breakout",
        "blurb": (
            "Buys when price breaks above its recent high with strong volume "
            "— aiming to catch the start of a new move. Prone to false "
            "breakouts in quiet markets."
        ),
        "description": (
            "BUY when the close breaks above the highest close of the last 20 "
            "bars and the volume is at least 1.5x the 20-bar average volume. "
            "EXIT when the close drops below the lowest close of the last 10 "
            "bars. Use an ATR(14) based stop loss of 2x ATR."
        ),
    },
    {
        "id": "momentum",
        "icon": "⚡",
        "title": "Momentum",
        "blurb": (
            "Buys when recent returns are strongly positive and momentum "
            "indicators agree. Simple and robust, but gives back profit at "
            "sharp turning points."
        ),
        "description": (
            "BUY when the 10-bar return is positive and the MACD(12,26,9) "
            "line crosses above its signal line. EXIT when the MACD line "
            "crosses back below the signal line or the 10-bar return turns "
            "negative."
        ),
    },
    {
        "id": "volatility",
        "icon": "🌊",
        "title": "Volatility Squeeze",
        "blurb": (
            "Waits for unusually quiet periods (tight Bollinger Bands), then "
            "trades the expansion when price escapes the range. Patient — "
            "few but potentially large trades."
        ),
        "description": (
            "BUY when the close breaks above the upper Bollinger Band(20, 2) "
            "after the band width has been narrowing for at least 10 bars. "
            "EXIT when the close falls back below the middle Bollinger Band. "
            "Use an ATR(14) based stop loss of 2.5x ATR."
        ),
    },
]


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    # Sync handler → FastAPI runs it in a threadpool, so the blocking disk I/O
    # (load_catalog + custom-block listing + read_wiki_page + recent_runs)
    # doesn't stall the event loop.
    import custom_block_store as cbs

    # Import backtest/strategy-side helpers lazily to avoid import-time
    # coupling; these are the same functions the standalone /backtest and
    # /strategy pages use (DeepR 2026-08-09 [YÜKSEK]: previously reached
    # into as `_`-prefixed private names — see nau_deepr_ucuncu_tur_2026_08_09.md).
    from web.routes.backtest import (
        catalog_index_symbols,
        last_result_get,
        recent_runs,
        result_viewmodel,
    )
    from web.routes.strategy import session_drafts, wiki_html_for

    sid = session_id(request)
    catalog = load_catalog()
    default_type = next(iter(BLOCK_CATALOG.keys()))
    wiki_active, wiki_html = wiki_html_for(default_type)

    # Autonomous mode: bind the studio cockpit to the newest still-running agent
    # run (System B), so switching to OTONOM after a restart/refresh reconnects
    # to a live loop even if it was started elsewhere (the /agent page or API).
    # Same accessor web.routes.agent_backtest.page's own AUTO panel uses (DeepR
    # 2026-08-09 [YÜKSEK] — this used to import and scan the raw
    # _AGENT_LOCK/_AGENT_PROGRESS itself, a second unmonitored acquirer of a
    # lock with a documented deadlock incident; see the accessor's docstring).
    try:
        from web.routes.agent_backtest import newest_active_run_id

        active_run_id = newest_active_run_id()
    except Exception:
        active_run_id = None

    # Model listesi CANLI (OpenRouter kataloğu + anahtar durumu) — bir kez çöz,
    # hem picker'ı hem varsayılan seçimi aynı listeden türet.
    _models = _llm_models()

    # Sembol satırları (disk taraması) bir kez okunur: hem picker listesi hem
    # de sihirbazın sembol→kategori haritası aynı görüntüden türesin.
    _symbol_rows = _bybit_symbols()

    # Dış katalog da ÖNBELLEKSİZ bir dizin taraması: picker listesi ile brief'in
    # varsayılan seçimi aynı görüntüden türesin, tarama sayfa başına iki kez
    # koşmasın (varsayılanı ayrı çözerken sessizce ikinci taramaya dönmüştü).
    _ext_symbols = _mc_external_symbols()

    # Backtest tab: session-scoped last result (Faz 3).
    slot = last_result_get(sid)
    last_row = None
    if slot["r"] is not None:
        last_row = result_viewmodel(
            slot["r"],
            slot["spec_name"],
            slot.get("narrative", ""),
            dict(slot.get("bars_info", {})),
        )

    ctx = {
        "active": "studio",
        "page_title": "Strategy Studio",
        "market": get_market_info(),
        # ── Autonomous cockpit: newest running agent run (System B), or None ──
        "active_run_id": active_run_id,
        # ── Compose tab context ──
        "block_catalog": BLOCK_CATALOG,
        "default_type": default_type,
        "drafts": session_drafts(sid),
        "wiki_active": wiki_active,
        "wiki_html": wiki_html,
        "custom_blocks": cbs.list_custom(include_ephemeral=False),
        "options": {
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
            "trade_size_mode": "fixed_usdt",
            "trade_size_percent": 5.0,
            "trade_size_atr_risk": 1.0,
            "trade_size_usdt": 1000.0,
            "trade_size_btc": 0.1,
            "emulate": False,
            "trend_filter": False,
            "trend_interval": "60",
            "trend_ema_period": 50,
        },
        # ── Shared: the catalog. Compose lists newest-first; Backtest picker
        # iterates it too — pass newest-first (compose_body reverses visually
        # via its own include, backtest_body renders the picker in list order).
        "catalog": list(reversed(catalog)),
        # ── Backtest tab context ──
        "last": last_row,
        "preferred_spec_id": request.query_params.get("spec_id", ""),
        "recent_runs": recent_runs(6),
        "bybit_symbols": _symbol_rows,
        "bybit_categories": BYBIT_CATEGORIES,
        "bybit_intervals": BYBIT_ALL_INTERVALS,
        "index_symbols": catalog_index_symbols(),
        # ── Simple-mode wizard context ──
        "strategy_templates": STRATEGY_TEMPLATES,
        # Sihirbazın hızlı coin butonları hangi kategoride koşacağını buradan
        # öğrenir (bkz. _bybit_symbol_category_map).
        "bybit_symbol_categories": _bybit_symbol_category_map(_symbol_rows),
        # ── AUTO cockpit: model picker (Claude + varsa OpenRouter) ──
        "llm_models": _models,
        "llm_or_free_only": _llm_or_free_only(),
        "llm_or_paid_extras": _llm_or_paid_extras(),
        # Brief açılışta hangi ucu seçili göstersin (liste canlı → fallback var).
        "mc_default_model": _mc_default_model(_models),
        # AUTO brief SYMBOL seçicisi: noktalı id'ler dış katalogdan CANLI gelir
        # (elle yazılı tek satır QQQ.NASDAQ, ingest edilen diğer hisseleri
        # görünmez kılıyordu).
        "mc_external_symbols": _ext_symbols,
        "mc_default_symbol": _mc_default_symbol(_ext_symbols),
        # ── AUTO kokpiti: effort (düşünme bütçesi) seçici ──
        "llm_efforts": _llm_efforts(),
        "mc_default_effort": AUTO_DEFAULT_EFFORT,
        # ── Model rozeti: SIMPLE/PRO uygulama varsayılanını kullanır, AUTO'da
        #    bu yalnız başlangıç değeri (picker/koşu üzerine yazar) ──
        **llm_badge(),
    }
    # Preserve strategy.py's manual render + cookie set (drafts session depends
    # on nautlab_sid; session_id above mints it).
    #
    # KAYAN SÜRE (DeepR 2026-08-11 [ORTA]): çerez eskiden yalnız YOKSA
    # yazılıyordu, yani sayfayı yenilemek ömrü uzatmıyordu — bu ekranda saatlerce
    # çalışan kullanıcının son sonuç paneli, taslakları ve tek-aktif-koşu
    # koruması, hepsi sid'e bağlı olduğu için bir anda sıfırlanıyordu. Her
    # yanıtta yeniden yaz: aktif kullanım oturumu ayakta tutsun.
    html = templates.get_template("studio.html").render(request=request, **ctx)
    resp = HTMLResponse(html)
    set_session_cookie(resp, sid)
    return resp


def _llm_models():
    """AUTO kokpiti model seçici seçenekleri (Claude + varsa OpenRouter)."""
    from agent import selectable_models

    return selectable_models()


def _llm_or_free_only() -> bool:
    """OpenRouter grubu ücretsizlerle mi sınırlı — başlık bunu dürüstçe yazar."""
    from agent import openrouter_free_only

    return openrouter_free_only()


def _llm_or_paid_extras() -> set[str]:
    """Ücretsiz listeye elle eklenmiş paralı satırlar — kendi optgroup'unda."""
    from agent import openrouter_paid_extras

    return set(openrouter_paid_extras())


def _llm_efforts() -> list[tuple[str, str]]:
    """Effort seçici seçenekleri — [(değer, etiket)].

    İlk satır "" = bayrağı hiç geçme. Bunu bir seviye adıyla ("medium") taklit
    etmiyoruz: ucun kendi varsayılanı sürümle değişebilir ve kullanıcıya yanlış
    bir söz vermiş oluruz.
    """
    from agent import SELECTABLE_EFFORTS

    return [("", "varsayılan (uç belirler)")] + [(e, e) for e in SELECTABLE_EFFORTS]


def _mc_default_model(models: list) -> str:
    """AUTO brief'i açılırken MODEL seçicisinde seçili gelecek uç.

    Picker'ın içeriği CANLI: OpenRouter satırları anahtara, ücretsiz filtresine
    ve NAUTILUS_OPENROUTER_EXTRA_MODELS'e bağlı. İstenen uç o an listede yoksa
    uygulama varsayılanına ("" = Claude) düşeriz — aksi halde hiçbir seçeneği
    işaretlenmemiş bir kutu kalır ve START, kullanıcının görmediği bir modelle
    koşardı.
    """
    return AUTO_DEFAULT_MODEL if any(v == AUTO_DEFAULT_MODEL for v, _ in models) else ""


def llm_badge() -> dict[str, str]:
    """Ekranlarda "hangi model" rozetinin context'i.

    Uygulama varsayılanını (kredi fallback'i devredeyse onu) çözer; SIMPLE/PRO
    yüzeyleri her zaman bunu kullanır. Şablonlarda ``llm_model_label`` /
    ``llm_model_id`` / ``llm_model_hybrid``.

    ``llm_model_hybrid`` amaç-başına eşleme devredeyken dolu gelir: tek bir model
    adı yazmak o durumda yanlış olurdu (bkz. ``llm_client.hybrid_note``).
    """
    from agent import hybrid_note, model_id, model_label

    return {
        "llm_model_label": model_label(),
        "llm_model_id": model_id(),
        "llm_model_hybrid": hybrid_note(),
    }


def _mc_default_symbol(symbols: list[str]) -> str:
    """AUTO brief'i açılırken SYMBOL kutusunda seçili gelecek enstrüman.

    ``_mc_default_model`` ile aynı gerekçe: liste canlı, istenen id o an
    taranan katalogda yoksa formun kendi ilk satırına (BTCUSDT) düşülür.
    Liste PARAMETRE olarak alınır — picker'ın gördüğü görüntünün AYNISI
    olduğu garanti olsun ve disk taraması sayfa başına bir kez koşsun.
    """
    return AUTO_DEFAULT_SYMBOL if AUTO_DEFAULT_SYMBOL in symbols else "BTCUSDT"


def _mc_external_symbols() -> list[str]:
    """AUTO brief'inin SYMBOL kutusundaki noktalı (dış katalog) id'leri.

    Dizin taraması; katalog kökü yoksa boş liste döner — bir seçici listesi
    asla traceback etmeye değmez.
    """
    from data import list_external_instruments

    try:
        return [r["instrument_id"] for r in list_external_instruments()]
    except Exception:
        return []


def _bybit_symbols():
    """Symbol list for the Backtest instrument picker — catalog symbols if
    present, else the static BYBIT_SYMBOLS fallback (same as backtest.page)."""
    from data import list_catalog_bybit_symbols

    return list_catalog_bybit_symbols() or [
        {"symbol": s, "category": "linear"} for s in BYBIT_SYMBOLS
    ]


# Aynı sembol birden çok pazarda önbellekte olabilir. Öncelik sırası: linear
# (uygulamanın varsayılanı, en derin geçmiş) → spot → inverse.
_CATEGORY_PRIORITY = ("linear", "spot", "inverse")


def _bybit_symbol_category_map(rows) -> dict[str, str]:
    """``{symbol: category}`` — hızlı coin butonu hangi pazarda koşacak.

    SIMPLE sihirbazı butonlarını ``list_catalog_bybit_symbols()``'un
    ``{symbol, category}`` çiftlerinden üretiyor ama kategoriyi ATIP her yere
    sabit ``"linear"`` gönderiyordu (DeepR 2026-08-11 [ORTA]). Sonuç: yalnız
    ``spot`` altında önbellekte olan bir sembolü seçen kullanıcıya MAX 404 →
    "veri yok" yazıyor (veri aslında var), Run backtest de indirilmiş spot
    verisi yerine linear'ı arıyordu. Listelenen şey ile koşulan şey aynı
    olsun diye kategori sembolle birlikte taşınır.
    """
    order = {c: i for i, c in enumerate(_CATEGORY_PRIORITY)}
    best: dict[str, str] = {}
    for row in rows or []:
        sym = row.get("symbol")
        cat = row.get("category") or "linear"
        if not sym:
            continue
        cur = best.get(sym)
        if cur is None or order.get(cat, 99) < order.get(cur, 99):
            best[sym] = cat
    return best
