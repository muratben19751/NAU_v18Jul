"""X (Twitter) anahtar kelime izleyicisi — giriş yapmış oturumla, 5 dakikada bir.

Kullanıcının kendi X hesabıyla `ttkom` (Türk Telekom, BIST) gibi bir anahtar
kelimeyi `f=live` arama sayfasından yoklar, YENİ tweetleri append-only bir JSONL
defterine yazar, konsola tek satır özet basar ve (ayarlıysa) toplu e-posta
gönderir.

## Bu klasör bağımsızdır

`twitter/` aynı depoda durur ama Nautilus uygulamasının parçası DEĞİLDİR: ondan
hiçbir modül import etmez, onun `NAU_*` ortam değişkenlerini ve veri kökünü
kullanmaz, onun test süitine ve PM2 girdisine karışmaz. Tek bağımlılığı
Playwright'tır ve o da tembel import edilir. Buradaki bir hata Nautilus'u,
oradaki bir değişiklik burayı etkilemez — ayrı çalıştırılır, ayrı test edilir.

## Neden API değil

X API v2'nin `search/recent` ucu 2026'da kullandıkça-öde kredi modeline geçti ve
ücretsiz katman kalmadı. Operatör anahtarsız yolu bilerek seçti (2026-08-15):
Playwright + kendi hesabının oturum çerezi. Bunun BEDELİ var ve burada yazılı
olması gerekir:

  • X kullanım şartlarına aykırıdır ve hesap askıya alınabilir. Karar operatörün.
  • X'in HTML'i habersiz değişir; `parse_tweets` bir gün boş döner. Bu yüzden
    "sıfır tweet" ile "parser bozuldu" AYRI durumlar olarak izlenir — üst üste
    `PARSE_FAIL_ALERT_AFTER` kez çekilen sayfada hiç tweet bulunamazsa operatöre
    uyarı gider. Sessiz sıfır, bozuk bir izleyicinin en tehlikeli hâlidir.
  • Oturum çerezi birkaç haftada bir düşer. Login duvarı görülünce döngü BOŞUNA
    istek atmaya devam etmez: ya (kimlik bilgileri ayarlıysa) yeniden giriş
    dener, ya durup e-posta atar.

## Tasarım sözleşmesi

- `run_once` çağrı yoluna ASLA exception sızdırmaz — tek bir kötü döngü
  izleyiciyi öldüremez.
- Playwright TEMBEL import edilir: modülü import etmek tarayıcı gerektirmez,
  testler ağa çıkmadan `parse_tweets`/defter/throttle mantığını sınayabilir.
- Kalıcı her yol `DATA_DIR`'den türer (`XWATCH_DATA_DIR` ile taşınabilir);
  varsayılan `~/.cache/x_watch`.

Ayrıntı: `twitter/README.md`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import smtplib
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)


def _env_int(
    name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Ortam değişkenini int'e çevir; bozuk/boş değerde ``default`` (+ kırpma)."""
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        v = int(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def data_dir() -> Path:
    """Kalıcı veri kökü — ``XWATCH_DATA_DIR`` ile taşınabilir.

    Repo ağacının DIŞINDA: defter büyür, oturum çerezi ise hesaba tam erişim
    demektir; ikisinin de versiyonlanabilir bir dizinde işi yok.
    """
    raw = (os.environ.get("XWATCH_DATA_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "x_watch"


# Import anında çözülür; testler modül-global'lerini değil `Config` alanlarını
# geçersiz kılar (bkz. `Config.ledger_path` ve arkadaşları).
DATA_DIR = data_dir()

LEDGER_PATH = DATA_DIR / "x_watch.jsonl"
STATE_PATH = DATA_DIR / "x_watch_state.json"
STORAGE_STATE_PATH = DATA_DIR / "x_storage_state.json"

# Defter bu eşiği geçince tek kuşaklık arşive (`<ad>.jsonl.1`) devredilir.
LOG_ROTATE_BYTES = 20 * 1024 * 1024

# Açılışta `seen` kümesini kurmak için defterin SONUNDAN okunacak bayt miktarı.
# Tam dosya okumak, defter büyüdükçe her PM2 restart'ını yavaşlatırdı; tweet
# id'leri monoton arttığı için son dilim pratikte yeterli.
SEEN_TAIL_BYTES = 1 * 1024 * 1024

# Üst üste bu kadar "sayfa geldi ama hiç tweet çıkmadı" turundan sonra operatöre
# tek bir uyarı gider (her turda değil — uyarı da spam olmamalı).
PARSE_FAIL_ALERT_AFTER = 5

_LOCK = threading.Lock()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class XWatchError(RuntimeError):
    """İzleyicinin bilinçli olarak ürettiği, operatöre yol gösteren hata."""


class LoginRequired(XWatchError):
    """Oturum yok ya da düşmüş — `x_login.py` yeniden koşmalı."""


class RateLimited(XWatchError):
    """X hız sınırı / doğrulama duvarı — üstel geri çekilme uygulanmalı."""


# ── Yapılandırma ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    query: str = "ttkom"
    interval_s: int = 300
    headless: bool = True
    mail_to: str = ""
    mail_min_s: int = 900
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    x_user: str = ""
    x_password: str = ""
    nav_timeout_ms: int = 45_000
    ledger_path: Path = field(default=LEDGER_PATH)
    state_path: Path = field(default=STATE_PATH)
    storage_state_path: Path = field(default=STORAGE_STATE_PATH)

    @classmethod
    def from_env(cls, **overrides) -> Config:
        """Ortamdan oku. Çağrı anında okunur (import anında değil) ki testler
        `monkeypatch.setenv` ile davranışı değiştirebilsin."""
        cfg = cls(
            query=(os.environ.get("XWATCH_QUERY") or "ttkom").strip(),
            interval_s=_env_int("XWATCH_INTERVAL_S", 300, lo=30),
            headless=(os.environ.get("XWATCH_HEADLESS") or "1").strip()
            not in {"0", "false", "no"},
            mail_to=(os.environ.get("XWATCH_MAIL_TO") or "").strip(),
            mail_min_s=_env_int("XWATCH_MAIL_MIN_S", 900, lo=0),
            smtp_host=(os.environ.get("XWATCH_SMTP_HOST") or "smtp.gmail.com").strip(),
            smtp_port=_env_int("XWATCH_SMTP_PORT", 465, lo=1, hi=65535),
            smtp_user=(os.environ.get("XWATCH_SMTP_USER") or "").strip(),
            smtp_password=os.environ.get("XWATCH_SMTP_PASSWORD") or "",
            x_user=(os.environ.get("XWATCH_X_USER") or "").strip(),
            x_password=os.environ.get("XWATCH_X_PASSWORD") or "",
        )
        return cls(**{**cfg.__dict__, **overrides}) if overrides else cfg


# ── HTML → tweet kayıtları ───────────────────────────────────────────────────

# `<a href="/kullanici/status/1234567890">` — tweet'in kimliği ve yazarı bu tek
# bağlantıdan gelir. Arama sayfasındaki her kartta en az bir tane bulunur.
_STATUS_HREF_RE = re.compile(r"^/([A-Za-z0-9_]{1,20})/status/(\d+)")

# Metin toplarken kapanış etiketi ÜRETMEYEN elemanlar; sayaç bunlarda azalmamalı
# yoksa `tweetText` bloğu erken kapanmış sayılır (emoji'ler `<img>` olarak gelir).
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _TweetCardParser(HTMLParser):
    """`article[data-testid="tweet"]` kartlarından {id, author, text, ...} çıkarır.

    stdlib `html.parser` ile yazıldı: repoda bs4/lxml yok ve bir kazıyıcı için
    yeni bir çalışma-zamanı bağımlılığı (+ `uv.lock` kilidi) eklemek, kazandığı
    kolaylıktan pahalı. İç içe geçme yalnız `article` ve metin bloğu için
    sayılır; sayfanın geri kalanının derinliği umursanmaz, böylece X'in React
    ağacındaki değişiklikler bu makineyi kolay kolay bozmaz.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._article_depth = 0
        self._cur: dict | None = None
        self._text_depth = 0
        self._chunks: list[str] = []

    # -- yardımcılar ----------------------------------------------------------
    @staticmethod
    def _attr(attrs, name):
        for k, v in attrs:
            if k == name:
                return v
        return None

    def _flush_text(self) -> None:
        # `is None` şart: kart yeni açıldığında `_cur` BOŞ sözlüktür ve `not {}`
        # doğrudur — truthiness kullanmak kartın tüm içeriğini sessizce eler.
        if self._cur is None:
            return
        text = "".join(self._chunks)
        text = re.sub(r"[ \t ]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if text and not self._cur.get("text"):
            self._cur["text"] = text
        self._chunks = []

    # -- HTMLParser kancaları -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        testid = self._attr(attrs, "data-testid")

        if tag == "article":
            if self._article_depth:
                self._article_depth += 1  # iç içe alıntı kartı — dışarıdakine ait
            elif testid == "tweet":
                self._article_depth = 1
                self._cur = {}
            return

        if self._cur is None:
            return

        if self._text_depth:
            # `tweetText` içindeyiz: metni topla, iç etiketleri say.
            if tag == "img":
                alt = self._attr(attrs, "alt")
                if alt:
                    self._chunks.append(alt)  # emoji `<img alt="🙂">` olarak gelir
            elif tag == "br":
                self._chunks.append("\n")
            if tag not in _VOID_TAGS:
                self._text_depth += 1
            return

        if testid == "tweetText":
            self._text_depth = 1
            self._chunks = []
            return

        if tag == "a":
            href = self._attr(attrs, "href") or ""
            m = _STATUS_HREF_RE.match(href)
            # İlk eşleşme kartın KENDİ tweet'idir; sonrakiler alıntı/yanıt
            # bağlantıları olabilir, bu yüzden üzerine yazılmaz.
            if m and not self._cur.get("id"):
                self._cur["author"] = m.group(1)
                self._cur["id"] = m.group(2)
                self._cur["url"] = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
            return

        if tag == "time":
            dt = self._attr(attrs, "datetime")
            if dt and not self._cur.get("created_at"):
                self._cur["created_at"] = dt

    def handle_endtag(self, tag):
        if self._text_depth:
            if tag not in _VOID_TAGS:
                self._text_depth -= 1
                if self._text_depth == 0:
                    self._flush_text()
            # `</article>` metin bloğunun içinde kapanırsa aşağıya da düşmeli.
            if tag != "article":
                return

        if tag == "article" and self._article_depth:
            self._article_depth -= 1
            if self._article_depth == 0:
                self._text_depth = 0
                self._flush_text()
                cur, self._cur = self._cur, None
                if cur and cur.get("id"):
                    self.rows.append(cur)

    def handle_data(self, data):
        if self._text_depth and self._cur is not None:
            self._chunks.append(data)


def parse_tweets(html: str) -> list[dict]:
    """Arama sayfası HTML'inden tweet kayıtları. Ağa DOKUNMAZ.

    Bozuk/boş HTML'de patlamaz, `[]` döner — çağıran "sıfır tweet" ile "parser
    bozuldu" ayrımını `Result.parse_failed` üzerinden yapar.
    """
    if not html:
        return []
    p = _TweetCardParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        log.warning("x_watch.parse_tweets failed", exc_info=True)
        return p.rows  # o ana kadar toplananlar; yarım sayfa hiç yoktan iyidir
    # Aynı tweet arama sayfasında iki kez görünebilir (pin/alıntı); id'ye göre
    # sırayı bozmadan tekilleştir.
    seen: set[str] = set()
    out: list[dict] = []
    for row in p.rows:
        tid = row.get("id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(row)
    return out


# ── Defter (append-only JSONL) ───────────────────────────────────────────────


def _rotate_if_large(path: Path, max_bytes: int = LOG_ROTATE_BYTES) -> None:
    """Eşiği aşınca `<ad>.jsonl.1` arşivine devret. Hata döngüyü durdurmaz."""
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            archive = path.with_name(path.name + ".1")
            if archive.exists():
                archive.unlink()
            path.rename(archive)
    except OSError:
        pass


def load_seen_ids(
    path: Path | None = None, *, tail_bytes: int = SEEN_TAIL_BYTES
) -> set[str]:
    """Defterin sonundan okunmuş tweet id'leri. Dosya yoksa boş küme."""
    p = path or LEDGER_PATH
    out: set[str] = set()
    try:
        size = p.stat().st_size
    except OSError:
        return out
    try:
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # ilk satır yarım olabilir — at
            chunk = f.read()
    except OSError:
        return out
    for ln in chunk.decode("utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue  # eşzamanlı append'in yırttığı satır — atla, ölme
        tid = rec.get("id")
        if tid:
            out.add(str(tid))
    return out


def append_tweets(rows: list[dict], path: Path | None = None) -> None:
    """Kayıtları deftere ekle. Best-effort — çağrı yoluna exception sızdırmaz."""
    if not rows:
        return
    p = path or LEDGER_PATH
    try:
        payload = "".join(
            json.dumps(
                {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **r},
                ensure_ascii=False,
            )
            + "\n"
            for r in rows
        )
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_large(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(payload)
    except Exception:
        log.warning("x_watch.append_tweets failed", exc_info=True)


# ── Çalışma durumu (mail kısması + parser sağlığı) ───────────────────────────


def load_state(path: Path | None = None) -> dict:
    """`{last_mail_ts, pending, parse_fail_streak, parse_alert_sent}`.

    Durum DİSKTE tutulur çünkü PM2 `autorestart` süreci habersiz yeniden başlatır;
    bellekte tutulsa her restart mail kısmasını sıfırlar ve henüz gönderilmemiş
    tweetler sessizce kaybolurdu.
    """
    p = path or STATE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("last_mail_ts", 0.0)
    raw.setdefault("pending", [])
    raw.setdefault("parse_fail_streak", 0)
    raw.setdefault("parse_alert_sent", False)
    return raw


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        log.warning("x_watch.save_state failed", exc_info=True)


# ── E-posta ──────────────────────────────────────────────────────────────────


def format_digest(rows: list[dict], query: str) -> str:
    lines = [f"'{query}' için {len(rows)} yeni tweet:", ""]
    for r in rows:
        when = r.get("created_at") or ""
        lines.append(f"@{r.get('author', '?')}  {when}")
        lines.append((r.get("text") or "(metin çıkarılamadı)").strip())
        lines.append(r.get("url") or "")
        lines.append("-" * 60)
    return "\n".join(lines)


def send_mail(subject: str, body: str, cfg: Config) -> bool:
    """Tek bir e-posta gönder. Başarılıysa True. Asla exception sızdırmaz.

    `mail_to` boşsa mail kapalıdır ve SMTP'ye HİÇ dokunulmaz — operatör
    e-postayı istemiyorsa sadece log/konsol yolu çalışır.
    """
    if not cfg.mail_to:
        return False
    if not (cfg.smtp_user and cfg.smtp_password):
        log.warning(
            "x_watch: XWATCH_MAIL_TO ayarlı ama XWATCH_SMTP_USER/"
            "XWATCH_SMTP_PASSWORD eksik — mail gönderilemiyor."
        )
        return False
    msg = EmailMessage()
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.mail_to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(
                cfg.smtp_host,
                cfg.smtp_port,
                context=ssl.create_default_context(),
                timeout=30,
            ) as s:
                s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
        return True
    except Exception:
        log.warning("x_watch.send_mail failed", exc_info=True)
        return False


# ── Playwright (tembel) ──────────────────────────────────────────────────────


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - ortam kurulumu
        raise XWatchError(
            "playwright kurulu değil. Kurulum:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


# X'in kendi makine-okur oturum durumu. Ölçüm 2026-08-15: oturumsuz çekilen
# arama sayfası HTTP 200 ve 276 KB dönüyor, `/i/flow/login`'e YÖNLENDİRMİYOR ve
# aşağıdaki HTML işaretçilerinin HİÇBİRİNİ içermiyor — ama `"isLoggedIn":false`
# alanını içeriyor. Tespitin dayanağı bu; İngilizce hata metni değil.
_LOGGED_OUT_MARKER = '"isLoggedIn":false'
_LOGGED_IN_MARKER = '"isLoggedIn":true'

# Yardımcı işaretçiler: gerçek giriş FORMU render edildiğinde görünürler.
# Tek başlarına yetmezler (yukarıdaki ölçüm), o yüzden ikincil sıradalar.
_LOGIN_MARKERS = (
    'data-testid="loginButton"',
    "/i/flow/login",
    'name="session[username_or_email]"',
)

# DİKKAT — buraya "Something went wrong" EKLEMEYİN. O metin X'in oturumsuz
# arama sayfasında render ediliyor ("Something went wrong, but don't fret…") ve
# bir zamanlar bu listedeydi: sonucu, çerez düştüğünde izleyicinin durumu
# `RateLimited` sanması, 1 saate kadar üstel geri çekilmeyle SONSUZA DEK dönmesi
# ve "yeniden giriş gerekiyor" e-postasını hiç atmamasıydı. Tasarımın en önemli
# güvenlik davranışı tam da bu yüzden sessizce ölüydü (bulundu 2026-08-15,
# gerçek yakalama: tests/fixtures/x_search_logged_out.html).
_LIMIT_MARKERS = ("Rate limit exceeded", "Too Many Requests", "rate limit exceeded")

_TWEET_CARD_MARKER = 'data-testid="tweet"'


def _raise_if_blocked(html: str, final_url: str) -> None:
    """Sayfa kullanılabilir mi? Değilse SEBEBİNE göre ayrı istisna at.

    Ayrım kozmetik değil, kurtarma yolunu belirliyor: `LoginRequired` döngüyü
    durdurup operatöre haber verir, `RateLimited` ise geri çekilip devam eder.
    Birini diğeri sanmak, izleyiciyi ya gereksiz durdurur ya da sonsuza dek
    sessiz bırakır.
    """
    if "/i/flow/login" in final_url or "/login" in final_url:
        raise LoginRequired(
            "X giriş sayfasına yönlendirildi — oturum düşmüş. "
            "`python x_login.py` ile yeniden giriş yapın."
        )
    if _LOGGED_OUT_MARKER in html:
        raise LoginRequired(
            "X sayfayı oturumsuz döndürdü (isLoggedIn=false) — çerez düşmüş. "
            "`python x_login.py` ile yeniden giriş yapın."
        )
    if any(m in html for m in _LOGIN_MARKERS):
        raise LoginRequired(
            "Sayfada giriş duvarı var — oturum çerezi geçersiz. "
            "`python x_login.py` ile yeniden giriş yapın."
        )
    if any(m in html for m in _LIMIT_MARKERS) and _TWEET_CARD_MARKER not in html:
        raise RateLimited("X hız sınırı döndürdü.")


def fetch_search_html(query: str, cfg: Config | None = None) -> str:
    """`https://x.com/search?q=<query>&f=live` sayfasını giriş yapmış oturumla çeker.

    Testlerde bu fonksiyon monkeypatch edilir (`data._fetch_bybit_page` kalıbı) —
    tüm ağ teması buraya hapsedilmiştir.
    """
    cfg = cfg or Config.from_env()
    if not cfg.storage_state_path.exists():
        raise LoginRequired(
            f"Oturum dosyası yok ({cfg.storage_state_path.name}). "
            "Önce `python x_login.py` çalıştırın."
        )
    sync_playwright = _sync_playwright()
    url = f"https://x.com/search?q={quote(query)}&f=live"
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=cfg.headless)
        except Exception as exc:
            # Paketin kurulu olması tarayıcının da kurulu olduğu anlamına gelmez;
            # playwright yükseltilince beklediği chromium BUILD numarası da
            # değişir ve eskisi artık kabul edilmez. Ham Playwright hatası
            # `run_once`'ın genel yutucusuna düşüp tek satırlık bir log oluyordu;
            # operatör "ne kurmam gerek" sorusunun cevabını göremiyordu.
            raise XWatchError(
                "Chromium başlatılamadı — tarayıcı ikilisi eksik ya da "
                "playwright sürümüyle uyumsuz. Çözüm:\n"
                "  playwright install chromium\n"
                f"(ham hata: {str(exc).splitlines()[0][:160]})"
            ) from exc
        try:
            ctx = browser.new_context(
                storage_state=str(cfg.storage_state_path),
                user_agent=_UA,
                locale="tr-TR",
                viewport={"width": 1280, "height": 1600},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.nav_timeout_ms)
            try:
                page.wait_for_selector(f"article[{_TWEET_CARD_MARKER}]", timeout=15_000)
            except Exception:
                # Gerçekten sıfır sonuç da olabilir, login duvarı da — ayrımı
                # aşağıdaki `_raise_if_blocked` yapar, burada yutulur.
                pass
            html = page.content()
            final_url = page.url
        finally:
            browser.close()
    _raise_if_blocked(html, final_url)
    return html


def relogin(cfg: Config | None = None) -> bool:
    """Kimlik bilgileri ayarlıysa headless yeniden giriş dener, oturumu tazeler.

    Varsayılan kurulumda parola SAKLANMAZ (`XWATCH_X_PASSWORD` boştur) ve bu
    fonksiyon False döner — çağıran o zaman operatöre "elle giriş gerekiyor"
    e-postası atar. 2FA açık bir hesapta otomatik giriş zaten tamamlanamaz.
    """
    cfg = cfg or Config.from_env()
    if not (cfg.x_user and cfg.x_password):
        return False
    sync_playwright = _sync_playwright()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg.headless)
            try:
                ctx = browser.new_context(user_agent=_UA, locale="tr-TR")
                page = ctx.new_page()
                page.goto(
                    "https://x.com/i/flow/login",
                    wait_until="domcontentloaded",
                    timeout=cfg.nav_timeout_ms,
                )
                page.fill('input[autocomplete="username"]', cfg.x_user, timeout=30_000)
                page.keyboard.press("Enter")
                page.wait_for_timeout(2_000)
                # X bazen "olağandışı etkinlik" deyip kullanıcı adını tekrar sorar.
                try:
                    page.fill(
                        'input[data-testid="ocfEnterTextTextInput"]',
                        cfg.x_user,
                        timeout=4_000,
                    )
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2_000)
                except Exception:
                    pass
                page.fill('input[name="password"]', cfg.x_password, timeout=30_000)
                page.keyboard.press("Enter")
                page.wait_for_url(
                    re.compile(r"https://x\.com/(home|search).*"), timeout=45_000
                )
                cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                ctx.storage_state(path=str(cfg.storage_state_path))
                _harden_permissions(cfg.storage_state_path)
                return True
            finally:
                browser.close()
    except Exception:
        log.warning("x_watch.relogin failed", exc_info=True)
        return False


def _harden_permissions(path: Path) -> None:
    """Oturum dosyasını yalnız sahibine okunur yap (POSIX). Windows'ta no-op."""
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        pass


# ── Tek tur ──────────────────────────────────────────────────────────────────


@dataclass
class Result:
    fetched: int = 0
    new: int = 0
    mailed: bool = False
    parse_failed: bool = False
    error: str = ""
    rate_limited: bool = False
    login_required: bool = False

    def line(self, query: str, next_s: float | None = None) -> str:
        parts = [
            f"[x_watch] q={query}",
            f"fetched={self.fetched}",
            f"new={self.new}",
            f"mailed={'yes' if self.mailed else 'no'}",
        ]
        if self.parse_failed:
            parts.append("parse=FAILED")
        if self.error:
            parts.append(f"error={self.error}")
        if next_s is not None:
            parts.append(f"next={int(next_s)}s")
        return " ".join(parts)


def run_once(
    cfg: Config | None = None,
    *,
    seen: set[str] | None = None,
    html: str | None = None,
    dry_run: bool = False,
) -> Result:
    """fetch → parse → dedupe → defter → (kısma geçerse) mail. ASLA raise etmez.

    `html` verilirse ağa çıkılmaz (`--dry-run --html` yolu ve testler).
    `seen` verilirse defterden yeniden okunmaz (döngü onu bellekte taşır).
    """
    cfg = cfg or Config.from_env()
    res = Result()
    try:
        page_html = html if html is not None else fetch_search_html(cfg.query, cfg)
    except LoginRequired as exc:
        res.error = str(exc).splitlines()[0]
        res.login_required = True
        return res
    except RateLimited as exc:
        res.error = str(exc).splitlines()[0]
        res.rate_limited = True
        return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
        return res

    rows = parse_tweets(page_html)
    res.fetched = len(rows)
    # "Sayfa geldi ama hiç kart yok" → parser bozulmuş OLABİLİR. Gerçekten sonuç
    # yoksa X yine de boş-durum kabuğunu döndürür, o yüzden bu bir kesinlik değil
    # sinyaldir; ardışık tekrarı anlamlı kılar (bkz. PARSE_FAIL_ALERT_AFTER).
    res.parse_failed = not rows and _TWEET_CARD_MARKER not in page_html

    if seen is None:
        seen = load_seen_ids(cfg.ledger_path)
    fresh = [r for r in rows if r.get("id") and r["id"] not in seen]
    res.new = len(fresh)
    for r in fresh:
        seen.add(r["id"])

    if dry_run:
        if fresh:
            print(format_digest(fresh, cfg.query))
        return res

    append_tweets(fresh, cfg.ledger_path)

    state = load_state(cfg.state_path)
    dirty = False

    if fresh:
        state["pending"].extend(fresh)
        dirty = True

    # Mail kısması: yoğun günde 288 mail yerine `mail_min_s` başına en fazla bir
    # toplu özet. Bekleyenler diskte durur, sıradaki uygun turda tek mailde çıkar.
    pending = state.get("pending") or []
    if pending and cfg.mail_to:
        waited = time.time() - float(state.get("last_mail_ts") or 0.0)
        if waited >= cfg.mail_min_s:
            subject = f"[x_watch] {len(pending)} yeni '{cfg.query}' tweet'i"
            if send_mail(subject, format_digest(pending, cfg.query), cfg):
                state["pending"] = []
                state["last_mail_ts"] = time.time()
                res.mailed = True
                dirty = True

    # Parser sağlığı: ardışık başarısızlık sayacı ve TEK seferlik uyarı.
    if res.parse_failed:
        state["parse_fail_streak"] = int(state.get("parse_fail_streak") or 0) + 1
        dirty = True
        if state["parse_fail_streak"] >= PARSE_FAIL_ALERT_AFTER and not state.get(
            "parse_alert_sent"
        ):
            send_mail(
                "[x_watch] parser boş dönüyor",
                f"Son {state['parse_fail_streak']} turda sayfa alındı ama hiç tweet "
                f"kartı çıkarılamadı. X'in HTML'i değişmiş olabilir; "
                f"`x_watch.parse_tweets` gözden geçirilmeli.",
                cfg,
            )
            state["parse_alert_sent"] = True
    elif state.get("parse_fail_streak") or state.get("parse_alert_sent"):
        state["parse_fail_streak"] = 0
        state["parse_alert_sent"] = False
        dirty = True

    if dirty:
        save_state(state, cfg.state_path)
    return res


# ── Döngü ────────────────────────────────────────────────────────────────────

_stop = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):  # pragma: no cover - sinyal yolu
        print(f"[x_watch] signal {signum} — kapanıyor", flush=True)
        _stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # ana thread değilse
            pass


def _sleep_seconds(cfg: Config, backoff: int) -> float:
    """Aralık + ±%15 jitter, hız sınırında üstel geri çekilme (tavan 1 saat).

    Jitter tesadüf değil: tam 300.0 saniyelik metronom, otomasyon tespitinin en
    kolay yakaladığı imzadır.
    """
    base = cfg.interval_s * (2**backoff)
    base = min(base, 3600)
    return base * random.uniform(0.85, 1.15)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X (Twitter) anahtar kelime izleyicisi")
    ap.add_argument("--once", action="store_true", help="tek tur koş ve çık")
    ap.add_argument("--query", default=None, help="XWATCH_QUERY yerine geçer")
    ap.add_argument("--html", default=None, help="ağa çıkma, bu HTML dosyasını işle")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="deftere yazma, mail atma — sadece bulunanları bas",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="[x_watch] %(levelname)s %(message)s"
    )
    # Windows konsolu cp1254 varsayar ve Türkçe/emoji içeren tweet metinlerinde
    # UnicodeEncodeError atar (`nau_config` ve `server.py` de aynısını yapıyor).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    overrides = {"query": args.query.strip()} if args.query else {}
    cfg = Config.from_env(**overrides)

    html = None
    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="replace")

    if args.once or html is not None:
        res = run_once(cfg, html=html, dry_run=args.dry_run)
        print(res.line(cfg.query), flush=True)
        return 1 if res.error else 0

    _install_signal_handlers()
    print(
        f"[x_watch] başladı · q={cfg.query} · aralık={cfg.interval_s}s · "
        f"mail={'açık' if cfg.mail_to else 'kapalı'} · defter={cfg.ledger_path}",
        flush=True,
    )
    seen = load_seen_ids(cfg.ledger_path)
    backoff = 0
    while not _stop.is_set():
        res = run_once(cfg, seen=seen, dry_run=args.dry_run)

        if res.login_required:
            # Boşuna istek atmaya devam etmek hem beyhude hem de hesabı daha çok
            # riske atar: ya tazele, ya dur.
            if relogin(cfg):
                print("[x_watch] oturum tazelendi, döngü sürüyor", flush=True)
                backoff = 0
                continue
            send_mail(
                "[x_watch] yeniden giriş gerekiyor",
                "X oturumu düştü ve otomatik yeniden giriş yapılamadı "
                "(XWATCH_X_USER/PASSWORD ayarlı değil ya da 2FA var).\n\n"
                "Kendi makinenizde `python x_login.py` çalıştırıp "
                "`pm2 restart nau-xwatch` deyin.",
                cfg,
            )
            print(f"[x_watch] DURDU — {res.error}", flush=True)
            return 2

        backoff = min(backoff + 1, 4) if res.rate_limited else 0
        nap = _sleep_seconds(cfg, backoff)
        print(res.line(cfg.query, nap), flush=True)
        _stop.wait(nap)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
