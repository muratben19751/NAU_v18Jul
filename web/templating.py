"""Jinja ortamı + süreç-ömürlü piyasa bağlamı — web katmanının yaprak tabanı.

DeepR 2026-08-11 [ORTA]: `server.py` ile `web/routes` arasında ÇİFT YÖNLÜ bir
bağımlılık vardı. `server.py` her router'ı import ediyor, router'lar da
`templates` (52 çağrı yeri), `get_market_info` (13) ve `get_bars` (1) için geri
`server`'a bakıyordu. Döngü olduğu için bu import'lar modül seviyesine
konulamamış, 54 tanesi FONKSİYON İÇİNE saklanmıştı — yanına da
``# local import: avoid circular`` notu düşülmüştü, yani smell biliniyordu.

Fonksiyon-içi import'un bedeli teorik değil:

- her istekte `sys.modules` araması + attribute erişimi (sıcak yolda 52 kopya),
- statik analiz `templates`in nereden geldiğini göremiyor; ruff/IDE "tanımsız"
  demediği için yanlış yazımlar çalışma zamanına ertelenir,
- ve en önemlisi: döngü GİZLENİYOR ama KALKMIYOR. Bir route modülünü tek başına
  import etmek (test, script, sandbox child'ı) hâlâ tüm `server.py`'yi —
  FastAPI app'i, middleware'leri, erişim kapısını, startup lifespan'ini —
  ayağa kaldırıyordu.

Çözüm `auto/robustness.py` ile aynı: paylaşılan şeyi ALTA al, ok tek yöne
baksın. `server.py` de, route'lar da buradan import eder; bu modül ne
`server`'ı ne de `web.routes`'u tanır. Kural `tests/test_web_layer_has_no_cycle.py`
içinde AST ile denetleniyor — yorumla korunan katman sınırı çürür.

`server.py` geriye dönük uyumluluk için adları yeniden dışa veriyor
(`from server import templates` çalışmaya devam eder), ama yeni kod buradan
almalı.

Wiki References
---------------
Bkz: [[webapp_module_map]], [[import_aninda_yakalanan_referans]]
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR = BASE_DIR / "web" / "static"

# Sunucu açılışında bir kez doldurulan varsayılan piyasa penceresi. Dashboard'ın
# sparkline'ı ve "market info" şeridi bunu okur; `server.lifespan` yazar.
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_CATEGORY = "linear"
DEFAULT_INTERVAL = "1"  # 1-minute bars


def static_version(base_dir: Path | None = None) -> str:
    """Cache-busting hash based on chart.js + app.css + app.js content.

    The vendored third-party bundles (htmx, Chart.js — pulled off the CDN and
    into web/static/ by the DeepR 2026-08-11 [ORTA] fix) are hashed too. They
    only change when someone deliberately bumps a version, but when that day
    comes the browser must not keep serving the old bundle from cache: the
    tag's `?v=` would otherwise be identical across the bump.

    ``base_dir`` çağrı anında çözülür (varsayılan: bu modülün ``BASE_DIR``'i)
    — HEAD'deki eski sözleşme buydu: yol import anında dondurulan bir sabitten
    değil, çağrı sırasındaki modül global'inden türetilir; testler kökü
    monkeypatch'leyerek sahte bir ``web/static`` ağacını hash'letebilir.
    """
    try:
        root = (base_dir if base_dir is not None else BASE_DIR) / "web" / "static"
        h = hashlib.md5()
        for name in (
            "chart.js",
            "app.css",
            "app.js",
            "htmx.min.js",
            "chartjs.umd.min.js",
        ):
            p = root / name
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()[:8]
    except Exception:
        return "0"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["static_version"] = static_version()


def _loop_running() -> bool:
    """Live loop status for the sidebar Engine card + Dashboard nav dot."""
    try:
        from state import get_state

        _, _, running, _ = get_state().snapshot()
        return bool(running)
    except Exception:
        return False


templates.env.globals["loop_running"] = _loop_running


def _engine_model_label() -> str:
    """Sidebar ENGINE card's model line. Was a hardcoded string ('Claude Fable 5')
    regardless of which model AUTO/SIMPLE/PRO actually ran with — this resolves
    the same way the AUTO cockpit's own model slot does (agent.model_label),
    so switching models (or the credit-exhaustion fallback kicking in) is
    reflected sitewide instead of only where llm_badge() happened to be wired.

    Amaç-başına eşleme (`NAUTILUS_MODEL_BY_PURPOSE`) devredeyse tek bir ad
    yazmak yine yanlış olurdu — aynı sebeple ` +hibrit` eklenir; ayrıntı
    kokpit/badge title'larında.
    """
    try:
        from agent import hybrid_note, model_label

        return model_label() + (" +hibrit" if hybrid_note() else "")
    except Exception:
        return "Claude"


templates.env.globals["engine_model_label"] = _engine_model_label


def _first_studio_strategy_id() -> str | None:
    """Nav link target for 'Strategy Builder' — the most recently updated
    strategy, so a fresh install (no seed_studio.py run) doesn't 404 on a
    hardcoded demo id."""
    try:
        from strategy_studio.store import StrategyStore

        meta = StrategyStore().list_meta()
        return meta[0].strategy_id if meta else None
    except Exception:
        return None


templates.env.globals["first_studio_strategy_id"] = _first_studio_strategy_id


def _catalog_issues() -> dict | None:
    """Bozuk (yüklenemeyen) strateji kayıtlarının özeti — yoksa None.

    DeepR 2026-08-11 [YÜKSEK]: `composer.load_catalog` bozuk kayıtları sessizce
    düşürüyordu; kullanıcı stratejisinin listeden kaybolduğunu görüyor ama
    sebebini asla öğrenemiyordu. Bunu bir Jinja global'i yapıyoruz çünkü
    katalog listesini render eden üç ayrı yüzey var (`compose_body`,
    `save_result` OOB tazelemesi, tekil `/strategy` listesi) ve üçü de farklı
    route'lardan besleniyor — bandı tek yerden almalarının yolu bu.
    """
    try:
        from composer import last_catalog_load_issues

        return last_catalog_load_issues()
    except Exception:
        return None


templates.env.globals["catalog_issues"] = _catalog_issues


def _datetimefmt(unix_ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=UTC)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(unix_ts)


templates.env.filters["datetimefmt"] = _datetimefmt


def _nl2br(value) -> Markup:
    return Markup(str(escape(value)).replace("\n", "<br>"))


templates.env.filters["nl2br"] = _nl2br


# Varsayılan sembolün (BTCUSDT) gerçekte hangi Bybit pazarında önbellekte
# olduğu. Sabit değil, KATALOGDAN türetilir.
#
# DeepR 2026-08-11 [DÜŞÜK]: aynı BTCUSDT için üç yüzey üç farklı varsayılan
# gösteriyordu — AUTO brief'i `spot`, `/backtest` `linear`, SIMPLE sihirbazı
# `linear`. Üçü de HTML'e gömülü sabitlerdi, yani hiçbiri kullanıcının
# kutusunda hangi serinin gerçekten indirilmiş olduğunu bilmiyordu: yanlış
# tahmin eden yüzeyde MAX 404 veriyor, koşu da olmayan seriyi arıyordu.
# Aynı gerekçe SIMPLE'ın sembol→kategori haritasını doğurmuştu
# (`studio._bybit_symbol_category_map`); eksik olan, AÇILIŞ değerinin de aynı
# kaynaktan gelmesiydi. Jinja global'i seçildi çünkü üç şablon üç ayrı
# route'tan besleniyor — bağlamı üç yerden geçirmek yerine tek yerden okusunlar.
_CATEGORY_PRIORITY = ("linear", "spot", "inverse")


def bybit_default_category(symbol: str = "BTCUSDT") -> str:
    """``symbol``'ün katalogda bulunduğu pazar; hiç yoksa ``linear``."""
    try:
        from data import list_catalog_bybit_symbols

        order = {c: i for i, c in enumerate(_CATEGORY_PRIORITY)}
        cats = [
            r.get("category") or "linear"
            for r in list_catalog_bybit_symbols()
            if r.get("symbol") == symbol
        ]
        if cats:
            return min(cats, key=lambda c: order.get(c, 99))
    except Exception:
        pass
    return "linear"


templates.env.globals["bybit_default_category"] = bybit_default_category


# ---------------------------------------------------------------------------
# Süreç-ömürlü piyasa bağlamı (startup'ta doldurulur)
# ---------------------------------------------------------------------------
_context: dict = {"bars": None, "market": None}


def set_market_context(bars=None, market: dict | None = None) -> None:
    """`server.lifespan` açılışta çağırır. Tek yazar var; kilit gerekmez."""
    if bars is not None:
        _context["bars"] = bars
    if market is not None:
        _context["market"] = market


def get_bars():
    return _context["bars"]


def get_market_info() -> dict:
    return _context["market"] or {
        "symbol": DEFAULT_SYMBOL,
        "venue": DEFAULT_CATEGORY.upper(),
        "bars": 0,
        "start": "—",
        "end": "—",
        "last_price": 0.0,
        "spark": [],
    }
