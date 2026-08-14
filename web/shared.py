"""Shared web-layer infrastructure — progress stores + logging/chart helpers.

Leaf module: routes import FROM here, and this imports nothing from
``web.routes`` (keeps the dependency arrow one-directional). Extracted to end
two smells the route modules had grown:

- the same "progress dict + lock + capped done-first eviction" was hand-copied
  into five route modules (``ProgressStore`` replaces it), and
- ``_log_backtest`` / ``_log_robustness`` / ``_chart_url`` / ``_rotate_if_large``
  were cross-imported between route modules (content coupling). They live here
  now as public functions; the route modules keep thin ``_name`` re-export
  aliases for internal use and backward compat with tests.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi.responses import HTMLResponse
from markupsafe import Markup, escape

from app_constants import DATA_DIR

try:
    import markdown as _md

    def render_md(
        txt: str, extensions: tuple[str, ...] = ("fenced_code", "tables")
    ) -> str:
        """Render markdown → HTML. Was hand-copied into strategy/backtest/wiki
        routes; lives here now (leaf module). ``extensions`` lets the wiki route
        keep its ``toc`` variant without a second copy."""
        return _md.markdown(txt, extensions=list(extensions))
except Exception:  # pragma: no cover

    def render_md(
        txt: str, extensions: tuple[str, ...] = ("fenced_code", "tables")
    ) -> str:
        return f"<pre>{txt}</pre>"


# ---------------------------------------------------------------------------
# XSS'e kapalı hata gövdeleri — kullanıcı verisinin HTML'e girdiği TEK kapı.
#
# DeepR 2026-08-11 [ORTA]: onlarca hata yolunda ham istek verisi f-string ile
# `HTMLResponse` gövdesine yazılıyordu (`f"<div class='empty-state'>Invalid
# name: {name}</div>"`). Bunun teorik kalmamasının üç sebebi vardı: yanıtlar
# text/html, `web/templates/studio.html` /agent/run için 4xx gövdelerini bile
# bilerek DOM'a basıyor (`shouldSwap = true`), ve `htmx.config.allowScriptTags`
# açık. Uygulama cloudflared tüneliyle internete bakıyor.
#
# Neden ortak yardımcı, neden tek tek f-string yaması değil: bu sink'ler 6
# route modülüne dağılmış ve sürekli yenisi ekleniyor. `escape()` çağırmayı
# hatırlamak bir SÜREÇ; şablonun kendisini `Markup` yapıp değerleri formatter'a
# vermek bir MEKANİZMA. `server.py`'nin `nl2br` filtresi zaten aynı duruşu
# alıyor (önce escape, sonra bilinçli Markup); burası onun route karşılığı.
# ---------------------------------------------------------------------------


def esc(value) -> Markup:
    """Tek bir değeri HTML gövdesine gömülebilir hâle getir (None → boş).

    ``escape()`` üstünde ince bir sarmalayıcı: ``None``'ı ``"None"`` diye
    basmak yerine boş bırakır. ``str()`` çağırMAZ — bu önemli: ``str()``
    bir ``Markup``'ı düz metne indirger ve ``escape()`` onu ikinci kez
    kaçırırdı (``&lt;b&gt;``). ``escape()`` zaten ``__html__`` varsa ona,
    yoksa ``str()``'e düşüyor; iç içe kullanım bu sayede güvenli.
    """
    return Markup("") if value is None else escape(value)


def safe_html(template: str, /, **values) -> Markup:
    """``template`` GÜVENİLİR bir literal, ``values`` GÜVENİLMEZ veridir.

    ``Markup(template).format(...)`` markupsafe'in ``EscapeFormatter``'ını
    kullanır: yer tutuculara giden her değer otomatik kaçırılır, şablonun
    kendi etiketleri olduğu gibi kalır. Yani şablonu yazan (biz) HTML
    üretebilir, veriyi gönderen (kullanıcı) üretemez.

    ÖNEMLİ: ``template`` asla kullanıcı verisinden gelmemeli — literal olmalı.
    Şablonda gerçek bir süslü parantez gerekiyorsa ``{{`` / ``}}`` ile kaçır
    (ör. inline CSS).
    """
    return Markup(template).format(**{k: esc(v) for k, v in values.items()})


def _toast_header(text: str) -> str:
    """``HX-Toast`` başlığı için güvenli bir değer üret.

    İki ayrı tuzak var ve ikisi de gövdedeki XSS'ten bağımsız:
    (a) CR/LF içeren bir değer başlık enjeksiyonu demek — h11 çoğunu reddeder
    ama biz hiç göndermeyelim; (b) Starlette başlıkları latin-1 kodlar, yani
    Türkçe bir 'ş'/'ı' (ya da kullanıcının yapıştırdığı herhangi bir emoji)
    doğrudan ``UnicodeEncodeError`` → 500 üretir. Hata yolunun kendisi 500
    atmasın diye kodlanamayan karakterleri düşürüyoruz.

    Gövdeye HTML kaçışı UYGULANMAZ: base.html toast'u ``textContent`` ile
    basıyor, yani orada ``&lt;`` görünürdü.
    """
    flat = " ".join(str(text).split())
    return flat.encode("latin-1", "replace").decode("latin-1")


def error_html(
    template: str,
    status_code: int = 400,
    *,
    css_class: str = "empty-state",
    toast: str | bool | None = None,
    **values,
) -> HTMLResponse:
    """``<div class="empty-state">…</div>`` biçiminde kaçırılmış bir hata yanıtı.

    ``template`` güvenilir literal, ``values`` kullanıcı verisi — kural
    ``safe_html`` ile aynı. ``toast=True`` gövdenin düz-metin hâlini
    ``HX-Toast`` başlığına da koyar (çağıranların elle ikinci kez yazdığı
    mesajın tek kaynaktan gelmesi için); ``toast="..."`` ayrı bir metin verir.

    ``css_class`` yalnızca çağıranın seçtiği sabit sınıf adıdır; yine de
    kaçırılır ki bir gün değişkenden beslenirse sink açılmasın.
    """
    body = safe_html(template, **values)
    resp = HTMLResponse(
        Markup('<div class="{cls}">{body}</div>').format(cls=css_class, body=body),
        status_code=status_code,
    )
    if toast:
        text = toast if isinstance(toast, str) else template.format(**values)
        resp.headers["HX-Toast"] = "err|" + _toast_header(text)
    return resp


# ---------------------------------------------------------------------------
# Anonymous session id — a cookie-scoped id used to partition per-user state
# (draft blocks in the composer, last backtest result). Lived only in
# strategy.py; moved here so backtest.py can share the SAME session dimension
# (its _LAST_RESULT was a process-global single slot — last-writer-wins across
# users). Leaf module: takes duck-typed request/response (``.cookies`` /
# ``.set_cookie``) so it need not import FastAPI.
# ---------------------------------------------------------------------------
SESSION_COOKIE = "nautlab_sid"

# When this server process came up (module import ≈ process start). The token
# badge uses it as the "this session" cutoff into the persistent ledger —
# session-scoping is a VIEW over one ledger file, not a second counter that
# could drift from it. Same isoformat shape as token_ledger lines, so plain
# string comparison filters correctly.
SERVER_STARTED_AT = datetime.now(UTC).isoformat(timespec="seconds")

# Oturum çerezinin ömrü. 1 SAATTİ ve bu, bu uygulamanın gerçek kullanım
# ritmiyle çelişiyordu (DeepR 2026-08-11 [ORTA]): AUTO'nun varsayılan bütçesi
# 4 saat, backtest+robustness turları da uzun. Kullanıcı bir saatten fazla aynı
# sekmede çalıştığında çerez düşüyor, bir sonraki istekte yeni bir sid basılıyor
# ve (a) son sonuç paneli, (b) Compose taslakları, (c) oturum başına
# tek-aktif-koşu koruması sıfırlanıyordu — üçü de sid ile anahtarlı.
# 30 gün: auth çerezi (server.py) ile aynı ölçek. Arkasındaki state zaten süreç
# ömrüyle sınırlı bellek-içi sözlükler, yani uzun çerez fazladan bir şey
# saklamıyor — sadece aynı tarayıcıya aynı adı vermeye devam ediyor.
SESSION_COOKIE_MAX_AGE = 30 * 24 * 3600
_SESSION_COOKIE_MAX_AGE = SESSION_COOKIE_MAX_AGE  # geriye dönük ad


# LLM'e serbest metin geçiren her ucun üst sınırı.
#
# DeepR 2026-08-11 [DÜŞÜK]: sınır `web/routes/strategy.py` ve
# `web/routes/backtest.py` içinde iki ayrı kopya olarak yaşıyordu ve iki uç onu
# hiç uygulamıyordu: AUTO'nun `hint`'i (`POST /agent/run`) ve Studio'nun `ask`'i
# (`/studio/{id}/ai/suggest`, `/studio/{id}/loop/start`). Yapıştırılan çok
# megabaytlık bir metin oradan doğrudan ücretli bir çağrıya gidiyordu; AUTO
# tarafında daha da kötüsü, `hint` her iterasyonun prompt'una yeniden
# ekleniyor, yani maliyet tur sayısıyla ÇARPILIYORDU.
#
# Kopya sayısı ikiden dörde çıkacaktı; o eşikte "yerel tutmak yeni bir import'u
# hak etmiyor" gerekçesi geçerliliğini yitirir. Yaprak modülde tek tanım.
MAX_LLM_TEXT_LEN = 4000


def invalid_date_range(start: str, end: str) -> str | None:
    """Bozuk ya da ters çevrilmiş bir tarih aralığı için hata cümlesi, yoksa None.

    Boş değerler geçerli (boş = tüm önbellek). Yalnız ikisi de verilip
    ``start > end`` olduğunda ya da biri ``YYYY-MM-DD`` olarak ayrıştırılamadığında
    hata döner.

    DeepR 2026-08-11 [DÜŞÜK]: `/backtest` bu kuralı uyguluyordu, `/lab/run`
    uygulamıyordu — geçersiz tarihi sessizce ``None``'a çevirip VARSAYILAN
    pencerede koşuyordu. Kullanıcı "2024 için test ettim" sanıyor, sonuç başka
    bir dönemin sonucu oluyordu; hiçbir yerde uyarı yoktu. Kuralın yaprak
    modülde tek kopyası olsun ki üçüncü bir yüzey eklendiğinde de aynı şeyi
    söylesin.
    """
    s, e = (start or "").strip(), (end or "").strip()
    if not s and not e:
        return None
    try:
        sd = datetime.strptime(s, "%Y-%m-%d").date() if s else None
        ed = datetime.strptime(e, "%Y-%m-%d").date() if e else None
    except ValueError:
        return "Dates must be in YYYY-MM-DD format."
    if sd and ed and sd > ed:
        return "End date cannot be before the start date."
    return None


def set_session_cookie(response, sid: str) -> None:
    """``nautlab_sid``'i ``response``'a yaz — çerez seçeneklerinin TEK yeri.

    Aynı dört seçenek (httponly/samesite/max_age) dört ayrı modülde elle
    kopyalanmıştı; biri güncellenince ötekiler sessizce eski ömürde kalıyordu.
    """
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        max_age=SESSION_COOKIE_MAX_AGE,
    )


def session_id(request, response=None) -> str:
    """Return the caller's ``nautlab_sid`` (minting one if absent). When
    ``response`` is given, (re)set the cookie for sliding expiry — refreshed on
    EVERY response so a draft doesn't expire mid-composition."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = uuid.uuid4().hex
    if response is not None:
        set_session_cookie(response, sid)
    return sid


# ---------------------------------------------------------------------------
# Progress store — one per long-running feature (backtest/gen/sweep/lab/robust/
# agent). Holds the run→state dict, its lock, and the capacity policy so the
# identical eviction block isn't copy-pasted per module. Callers keep using the
# underlying dict/lock directly (via .raw()/.lock aliases) for reads/updates;
# only the create+evict step goes through the store.
# ---------------------------------------------------------------------------
class ProgressStore:
    """Bounded run-id → state-dict registry with a done-first eviction policy.

    ``max_entries`` caps memory from abandoned runs. Two create policies match
    the two behaviours the routes had: ``create_evicting`` drops the oldest
    entry (done ones first) to make room — accepting that a still-running run
    could lose its slot under load; ``create_or_refuse`` drops only *done*
    entries and refuses (returns ``False``) if every slot is an active run.
    """

    def __init__(self, max_entries: int) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries

    @property
    def lock(self) -> threading.Lock:
        """The store's lock — alias so existing ``with _X_LOCK:`` sites work."""
        return self._lock

    def raw(self) -> dict[str, dict]:
        """The underlying dict — alias so existing direct access is unchanged.

        Callers must hold ``self.lock`` while touching it, exactly as before.
        """
        return self._d

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._d.get(run_id)

    def _evict_oldest_locked(self, on_evict) -> None:
        # done-first, else the oldest run (data-loss last resort). Caller holds
        # the lock; the store is non-empty (len >= max_entries >= 1).
        oldest = next(
            (k for k, v in self._d.items() if v.get("done")),
            next(iter(self._d)),
        )
        self._d.pop(oldest, None)
        if on_evict is not None:
            on_evict(oldest)

    def create_evicting(self, run_id: str, initial: dict, on_evict=None) -> None:
        """Insert ``initial`` under ``run_id``, evicting to stay within cap."""
        with self._lock:
            while len(self._d) >= self.max_entries:
                self._evict_oldest_locked(on_evict)
            self._d[run_id] = initial

    def create_or_refuse(self, run_id: str, initial: dict, on_evict=None) -> bool:
        """Insert ``initial`` unless every slot holds an active (not done) run.

        Returns ``True`` on insert, ``False`` when refused (all running).
        ``on_evict(evicted_id)`` runs under the lock for each dropped done run.
        """
        with self._lock:
            while len(self._d) >= self.max_entries:
                evict_id = next((k for k, v in self._d.items() if v.get("done")), None)
                if evict_id is None:
                    return False
                self._d.pop(evict_id, None)
                if on_evict is not None:
                    on_evict(evict_id)
            self._d[run_id] = initial
            return True


class SessionRunGuard:
    """Oturum başına tek aktif koşu: ``sid → (kind, run_id)`` kaydı.

    DeepR 2026-08-11 [DÜŞÜK]: bu koruma üç route modülüne elle kopyalanmıştı
    (``backtest``, ``lab``, ``robustness``) ve üçünden yalnız biri test
    ediliyordu. Kopyalar zaten ayrışmıştı: ``backtest.py``'ninki terk edilmiş
    oturumlar için bir tavan + budama taşıyor, ``lab``/``robustness``
    kopyaları taşımıyordu — yani o iki sözlük süreç ömrü boyunca sınırsız
    büyüyordu. Kopyalanan bir korumanın sessizce ayrışması tam da bunun gibi
    görünür: davranışsal fark değil, yalnız bir tanesine yapılmış bir
    iyileştirme.

    Tek sınıf, üç kullanım. ``kind`` çoklu-store ayrımı içindir
    (``run``/``gen``/``sweep``); tek store'lu çağrılar varsayılanı kullanır.

    Kilitleme sözleşmesi kopyalardan devralındı ve bilinçlidir: store
    aramaları kayıt kilidinin DIŞINDA yapılır — iç içe kilit yok.
    """

    def __init__(self, resolve_store, max_entries: int = 500) -> None:
        # resolve_store: kind -> ProgressStore (ya da .get(rid) sunan herhangi bir şey)
        self._resolve = resolve_store
        self._d: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries

    def active_kind(self, sid: str) -> str | None:
        """Bu oturumun canlı işinin türü, yoksa None (bayat kaydı temizler)."""
        with self._lock:
            ent = self._d.get(sid)
        if ent is None:
            return None
        kind, rid = ent
        raw = self._resolve(kind).get(rid)
        if raw is None or raw.get("done"):
            # Bitti (ya da tahliye edildi / sunucu yeniden başladı) — kaydı düşür.
            with self._lock:
                if self._d.get(sid) == ent:
                    self._d.pop(sid, None)
            return None
        return kind

    def busy(self, sid: str) -> bool:
        return self.active_kind(sid) is not None

    def set_active(self, sid: str, rid: str, kind: str = "run") -> None:
        ent = (kind, rid)
        with self._lock:
            over_cap = len(self._d) >= self.max_entries and sid not in self._d
            snapshot = list(self._d.items()) if over_cap else []
            self._d[sid] = ent
        if not over_cap:
            return
        # Terk edilmiş oturumları fırsatçı budama. Store aramaları kilit
        # DIŞINDA — kilit iç içe geçmesin.
        for other_sid, other_ent in snapshot:
            other_kind, other_rid = other_ent
            raw = self._resolve(other_kind).get(other_rid)
            if raw is None or raw.get("done"):
                with self._lock:
                    if self._d.get(other_sid) == other_ent:
                        self._d.pop(other_sid, None)

    def raw(self) -> dict[str, tuple[str, str]]:
        """Altındaki sözlük — testlerin ve mevcut doğrudan erişimin kullandığı."""
        return self._d

    @property
    def lock(self) -> threading.Lock:
        return self._lock


# ---------------------------------------------------------------------------
# Chat store — server-side multi-turn conversation registry. Distinct from
# ProgressStore: a chat has no "done" flag, so eviction is TTL + oldest-first
# (not done-first). Generalizes the hand-rolled _CHAT_STORE/_CHAT_LOCK/
# _chat_evict_locked block that "AI ile iyileştir" grew in web/routes/backtest.py
# (:137-157); the new block-edit / draft-edit chats reuse it instead of copying.
#
# The LLM call MUST NOT run under the lock. Use the read-copy / commit pattern:
#   conv = store.get(cid)                 # read a copy (short lock)
#   reply = await asyncio.to_thread(...)  # LLM call, no lock held
#   store.commit(cid, lambda c: ...)      # re-fetch under lock, mutate, stamp ts
# ---------------------------------------------------------------------------
class ChatStore:
    """Bounded conv-id → conversation-dict registry with TTL + oldest-first eviction.

    Each conversation dict is caller-defined but must carry a ``ts`` key
    (``time.monotonic()``) which this store maintains. ``ttl_sec`` drops idle
    conversations; ``max_entries`` caps concurrent conversations (oldest first).
    """

    def __init__(self, ttl_sec: int = 30 * 60, max_entries: int = 100) -> None:
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries

    def _evict_locked(self) -> None:
        """Drop TTL-expired conversations, then the oldest while over capacity.

        Caller holds ``self._lock``.
        """
        now = time.monotonic()
        for cid in [
            c for c, v in self._d.items() if now - v.get("ts", now) > self.ttl_sec
        ]:
            self._d.pop(cid, None)
        while len(self._d) > self.max_entries:
            oldest = min(self._d, key=lambda k: self._d[k].get("ts", 0.0))
            self._d.pop(oldest, None)

    def new(self, conv: dict) -> str:
        """Register ``conv`` under a fresh conv_id, stamp ts, evict, return the id."""
        conv_id = uuid.uuid4().hex[:8]
        conv["ts"] = time.monotonic()
        with self._lock:
            self._d[conv_id] = conv
            self._evict_locked()
        return conv_id

    def get(self, conv_id: str) -> dict | None:
        """Return a shallow copy of the conversation (or None), under the lock.

        A copy so the caller can read/iterate without holding the lock while the
        LLM call runs; writes must go through ``commit``.
        """
        with self._lock:
            conv = self._d.get(conv_id)
            return dict(conv) if conv is not None else None

    def commit(self, conv_id: str, mutate):
        """Re-fetch the live conversation under the lock, apply ``mutate(conv)``,
        refresh its ts, and evict. Returns the mutated conv, or None if it was
        evicted while the LLM call ran (caller renders an "expired" fragment).
        """
        with self._lock:
            conv = self._d.get(conv_id)
            if conv is None:
                return None
            mutate(conv)
            conv["ts"] = time.monotonic()
            self._evict_locked()
            # _evict_locked may have dropped it (TTL) — return only if still live.
            return self._d.get(conv_id)


# ---------------------------------------------------------------------------
# Append-only JSONL logs (single source of truth for the paths, which used to
# be duplicated across the writer modules and reports.py).
# ---------------------------------------------------------------------------
# DeepR 2026-08-14 [YÜKSEK]: kök elle kuruluydu (`Path.home() / ".cache" / ...`),
# yani `app_constants.DATA_DIR`'in NAU_DATA_DIR yönlendirmesi buraya HİÇ ulaşmıyordu.
# AUTO'nun kalıcı çıktılarının çoğu (backtest/robustness logu, oturum logları) tam
# olarak burada tanımlı olduğu için, `nau_config`'in "NAU_DATA_DIR oturum loglarını
# taşır" belgesi gerçekleşmiyor ve `tests/browser/conftest.py`'nin "gerçek ~/.cache
# kataloğuna ASLA dokunma" izolasyonu (ayrı süreç: yalnız env ile kurulabiliyor)
# sessizce deliniyordu.
_CACHE_DIR = DATA_DIR
BACKTEST_LOG = _CACHE_DIR / "backtest_log.jsonl"
ROBUSTNESS_LOG = _CACHE_DIR / "robustness_log.jsonl"

# AUTO session event log directory. Formerly defined in
# web/routes/agent_backtest.py with web/routes/sessions.py and
# web/routes/tearsheet.py both reaching into that route module to read it
# (DeepR 2026-08-09 [YÜKSEK] — one feature file owned a constant 3 modules
# needed). Lives here now, alongside the other shared log paths above.
SESSION_LOG_DIR = _CACHE_DIR / "agent_sessions"

# Append-only JSONL logs were growing without bound (backtest_log ~10MB,
# robustness ~5MB). On threshold exceed, roll over to a single-generation
# archive: the active file starts clean, and readers (tail-read / full read)
# see only the active file.
LOG_ROTATE_BYTES = 20 * 1024 * 1024

_BACKTEST_LOG_LOCK = threading.Lock()
_ROBUSTNESS_LOG_LOCK = threading.Lock()


def rotate_if_large(path: Path, max_bytes: int | None = None) -> None:
    """Roll the file over to `<name>.jsonl.1` if it exceeds the threshold (overwriting the existing archive).

    The threshold is resolved at call time (so it can be monkeypatched in tests)."""
    limit = max_bytes if max_bytes is not None else LOG_ROTATE_BYTES
    try:
        if path.exists() and path.stat().st_size >= limit:
            archive = path.with_name(path.name + ".1")
            if archive.exists():
                archive.unlink()
            path.rename(archive)
    except OSError:
        pass  # a rotation failure must not block log writing


def sanitize_floats(obj):
    """Replace NaN/Inf floats with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    return obj


def chart_url(bi: dict, spec_id: str = "") -> str:
    """Build chart URL scoped to the backtest's actual time window + interval.

    Interval → chart TF mapping picks a resolution that keeps bar count sane
    while covering the full backtest range so trade markers land on-screen.
    spec_id → chart, draws the strategy's actual indicators.
    """
    sym = bi.get("symbol")
    if not sym:
        return ""  # Index path (no symbol) — chart not supported
    cat = bi.get("category", "linear")
    interval = bi.get("interval", "60")
    sid = f"&spec_id={spec_id}" if spec_id else ""
    # Convert the backtest time range to a timestamp
    start_ts = end_ts = None
    try:
        from pandas import Timestamp

        if bi.get("start"):
            start_ts = int(Timestamp(bi["start"]).timestamp())
        if bi.get("end"):
            end_ts = int(Timestamp(bi["end"]).timestamp())
    except Exception:
        pass
    if start_ts and end_ts:
        # For a long range pick a larger TF (keep bar count under ~2000)
        span_days = (end_ts - start_ts) / 86400
        tf = interval
        if span_days > 400:
            tf = "D"
        elif span_days > 60:
            tf = "240"
        elif span_days > 14:
            tf = "60"
        elif span_days > 3:
            tf = "15"
        return f"/chart/data?symbol={sym}&category={cat}&interval={tf}&start_ts={start_ts}&end_ts={end_ts}{sid}"
    return f"/chart/data?symbol={sym}&category={cat}&interval={interval}&bars=2000{sid}"


def log_backtest(
    spec,
    result,
    instrument_kind: str,
    bars_info: dict,
    elapsed_sec: float | None = None,
    run_id: str | None = None,
) -> str:
    """Append one backtest to the log; returns the record's ``ts``.

    The ts is the log's identity key, so a caller that keeps its own row for the
    same run (the AUTO loop's live iteration list) can store it and link
    straight to the tear sheet instead of re-matching on metrics later.
    """
    BACKTEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "elapsed_sec": round(elapsed_sec, 3) if elapsed_sec is not None else None,
        "spec": {
            "id": spec.id,
            "name": spec.name,
            "blocks": [
                {"type": b.type, "role": b.role, "params": b.params}
                for b in spec.blocks
            ],
            "entry_logic": spec.entry_logic,
            "exit_logic": spec.exit_logic,
            "order_type": spec.order_type,
            "trade_size": float(spec.trade_size),
            "trade_size_mode": spec.trade_size_mode,
            "use_bracket": spec.use_bracket,
            "sl_type": spec.sl_type,
            "sl_value": spec.sl_value,
            "tp_type": spec.tp_type,
            "tp_value": spec.tp_value,
            "allow_short": spec.allow_short,
            "emulate": spec.emulate,
            # Remaining spec fields for deterministic re-run (reports/detail)
            # — getattr: don't break duck-typed fake specs in tests.
            "limit_offset_bps": getattr(spec, "limit_offset_bps", 0.0),
            "atr_period": getattr(spec, "atr_period", 14),
            "trade_size_percent": getattr(spec, "trade_size_percent", 5.0),
            "trade_size_atr_risk": getattr(spec, "trade_size_atr_risk", 1.0),
            "trade_size_usdt": getattr(spec, "trade_size_usdt", 1000.0),
            "trend_filter": getattr(spec, "trend_filter", False),
            "trend_interval": getattr(spec, "trend_interval", "60"),
            "trend_ema_period": getattr(spec, "trend_ema_period", 50),
            "delay_fill": getattr(spec, "delay_fill", True),
        },
        "instrument": instrument_kind,
        "bars": bars_info,
        "rationale": result.rationale,
        "error": result.error,
        "metrics": sanitize_floats(result.metrics),
        "n_equity_points": len(result.equity_curve),
    }
    with _BACKTEST_LOG_LOCK:
        rotate_if_large(BACKTEST_LOG)
        with open(BACKTEST_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    return record["ts"]


# ── Full-result snapshot store ────────────────────────────────────────────
# The jsonl log keeps only scalar metrics (no equity curve / trade list), so a
# history row can't rebuild the full result screen from it. We additionally
# persist the complete result view-model per run to bt_results/<run_id>.json and
# reload it verbatim from GET /backtest/result/<run_id>.
_RESULTS_DIR = _CACHE_DIR / "bt_results"
_RESULTS_KEEP = 20  # cap: newest N snapshots kept, older ones pruned


def save_result_snapshot(run_id: str, viewmodel: dict) -> None:
    """Persist a full backtest result view-model; prune to the newest N."""
    if not run_id:
        return
    try:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _RESULTS_DIR / f"{run_id}.json"
        with open(path, "w") as f:
            f.write(json.dumps(sanitize_floats(viewmodel), default=str))
        # Prune oldest beyond the cap (by mtime).
        snaps = sorted(
            _RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for stale in snaps[_RESULTS_KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass  # a snapshot failure must never break the backtest


def load_result_snapshot(run_id: str) -> dict | None:
    """Read back a stored result view-model; None if missing/unreadable."""
    try:
        path = _RESULTS_DIR / f"{run_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def log_robustness(
    spec_id: str,
    spec_name: str,
    result: dict,
    *,
    symbol: str | None = None,
    category: str | None = None,
    interval: str | None = None,
    venue: str | None = None,
) -> None:
    """Write the scalar robustness summary to disk (excluding equity curves).

    Identity fields (symbol/category/interval/venue) are added to the record; if
    not provided they are read from ``result``. Without them, different
    symbol/TF runs of the same spec were overwriting each other in a
    last-writer-wins fashion in the report.
    """
    try:
        ROBUSTNESS_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Walk-forward: without equity curve
        wf_clean = []
        for w in result.get("wfo_windows") or []:
            wf_clean.append(
                {
                    "window": w.get("window"),
                    "train_start": w.get("train_start"),
                    "train_end": w.get("train_end"),
                    "test_start": w.get("test_start"),
                    "test_end": w.get("test_end"),
                    "chosen_params": w.get("chosen_params") or {},
                    "train_objective": w.get("train_objective"),
                    "objective_metric": w.get("objective_metric"),
                    "train_metrics": w.get("train_metrics") or {},
                    "test_metrics": w.get("test_metrics") or {},
                    "test_metrics_naive": w.get("test_metrics_naive") or {},
                    "train_n_trades": w.get("train_n_trades"),
                    "test_n_trades": w.get("test_n_trades"),
                }
            )

        # Monte Carlo: excluding large lists
        mc_raw = result.get("mc") or {}
        mc_clean = {
            k: mc_raw[k]
            for k in (
                "n_sims",
                "n_trades",
                "starting_cash",
                "original_final",
                "p5_final",
                "p25_final",
                "median_final",
                "p75_final",
                "p95_final",
                "max_dd_p50",
                "max_dd_p95",
                "win_rate_mean",
                "win_rate_std",
                "method",
            )
            if k in mc_raw
        }
        # Preserve error field so log accurately reflects MC failures.
        if "error" in mc_raw:
            mc_clean["error"] = mc_raw["error"]
        # DeepR 2026-08-11 [YÜKSEK]: "0 işlem" ile "backtest çöktü" ayrımı log'da
        # da yaşamalı — /reports geçmişe bakarken ekranın söylediğinden farklı
        # bir hikâye anlatmasın.
        if "failed" in mc_raw:
            mc_clean["failed"] = bool(mc_raw["failed"])

        # In/Out-of-Sample: without equity curve
        sp_raw = result.get("split") or {}
        sp_clean = {
            k: sp_raw[k]
            for k in (
                "split_pct",
                "split_date",
                "in_sample_n_bars",
                "oos_n_bars",
                "overfitting_score",
                "overfitting_label",
                "in_sample_error",
                "oos_error",
            )
            if k in sp_raw
        }
        sp_clean["in_sample_metrics"] = sp_raw.get("in_sample_metrics") or {}
        sp_clean["oos_metrics"] = sp_raw.get("oos_metrics") or {}

        record = sanitize_floats(
            {
                "ts": datetime.now(UTC).isoformat(),
                "spec_id": spec_id,
                "spec_name": spec_name,
                "symbol": symbol or result.get("symbol"),
                "category": category or result.get("category"),
                "interval": interval or result.get("interval"),
                "venue": venue or result.get("venue"),
                "walk_forward": wf_clean,
                "wfo_summary": result.get("wfo_summary") or {},
                "monte_carlo": mc_clean,
                "in_out_split": sp_clean,
                # Kısmi bozulma işareti: Monte Carlo için koşulan tam backtest
                # patladıysa bu koşu BAŞARILI bir koşu değildi.
                "full_error": result.get("full_error") or "",
            }
        )
        with _ROBUSTNESS_LOG_LOCK:
            rotate_if_large(ROBUSTNESS_LOG)
            with open(ROBUSTNESS_LOG, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logging.getLogger(__name__).warning(
            "log_robustness: write failed for spec_id=%s", spec_id, exc_info=True
        )
