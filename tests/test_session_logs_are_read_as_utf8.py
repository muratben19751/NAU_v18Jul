"""Oturum logları UTF-8 okunmalı — yerel kod sayfası sayfayı düşürmesin.

`/sessions` **Internal Server Error** veriyordu. Sebep tek kelimeydi: iki metin
açılışında `encoding` verilmemişti (`path.open()`), dolayısıyla Python yerel kod
sayfasına düşüyordu — bu makinede **cp1254**. Loglar ise UTF-8: içlerinde
strateji adları, rationale metinleri ve adım satırları var
(`🌐 Web araştırması yapılıyor…`). Log'daki İLK Türkçe karakter
`UnicodeDecodeError` fırlatıp bütün liste sayfasını 500'e düşürüyordu.

Ölçüm (2026-08-14): liste ilk sayfasındaki **25 dosyanın 10'u** varsayılan
kodlamayla okunamıyordu. Yani bu nadir bir uç durum değil, çoğunluk durumu —
Türkçe bir uygulamada her koşu er ya da geç Türkçe karakter yazar.

Kusur platforma bağlı olduğu için POSIX CI'da (UTF-8 varsayılan) asla
görünmez; bu yüzden test kodlamayı taklit etmek yerine **açılışın kendisini**
denetler: kaynakta `encoding` belirtmeyen metin açılışı varsa kırmızı.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import web.routes.sessions as S

ROOT = Path(__file__).resolve().parent.parent

# Bu dosyaların log I/O'su UTF-8 olmak ZORUNDA: hepsi aynı jsonl ailesini
# (oturum logu, backtest logu, robustluk logu) yazıp okuyor.
_LOG_IO_MODULES = [
    "web/routes/sessions.py",
    "web/shared.py",
    "web/routes/agent_backtest.py",
]


def _text_opens_without_encoding(path: Path) -> list[int]:
    """`encoding` verilmeden açılan METİN dosyalarının satır numaraları.

    İkili mod (`"rb"`, `"wb"`) hariç — orada kodlama kavramı yok. Mod'un
    kaçıncı argüman olduğu çağrı biçimine bağlı: ``open(p, "rb")`` ile
    ``p.open("rb")`` farklı; ikisi de doğru ele alınmalı (ilk taramamda bu
    ayrımı atlayıp ikili açılışları yanlışlıkla suçlamıştım).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_method = isinstance(fn, ast.Attribute)
        name = fn.attr if is_method else getattr(fn, "id", "")
        if name != "open":
            continue
        mode_index = 0 if is_method else 1  # p.open(mode) / open(p, mode)
        mode = None
        if len(node.args) > mode_index and isinstance(
            node.args[mode_index], ast.Constant
        ):
            mode = node.args[mode_index].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if mode and "b" in str(mode):
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        bad.append(node.lineno)
    return bad


class TestLogIoAlwaysDeclaresItsEncoding:
    @pytest.mark.parametrize("rel", _LOG_IO_MODULES)
    def test_no_text_open_relies_on_the_platform_default(self, rel):
        offenders = _text_opens_without_encoding(ROOT / rel)
        assert not offenders, (
            f"{rel} içinde `encoding` verilmeden açılan metin dosyası var "
            f"(satır {offenders}). Windows'ta varsayılan cp1254'tür ve loglar "
            "UTF-8 — ilk Türkçe karakter sayfayı 500'e düşürür. POSIX CI bunu "
            "asla yakalayamaz."
        )

    def test_the_scan_would_actually_catch_a_bare_open(self, tmp_path):
        """Çıpa: tarama gerçekten ısırıyor mu, ve ikili modu affediyor mu?"""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "from pathlib import Path\n"
            "def a(p): return open(p)\n"  # yakalanmalı
            "def b(p): return Path(p).open()\n"  # yakalanmalı
            "def c(p): return open(p, 'rb')\n"  # ikili — affedilmeli
            "def d(p): return Path(p).open('rb')\n"  # ikili — affedilmeli
            "def e(p): return open(p, encoding='utf-8')\n",  # doğru — affedilmeli
            encoding="utf-8",
        )
        assert _text_opens_without_encoding(probe) == [2, 3]


class TestASessionLogWithTurkishTextStillSummarises:
    def test_the_summary_survives_utf8_content(self, tmp_path, monkeypatch):
        """Kırılmayı üreten gerçek içerik: emoji + Türkçe karakter."""
        d = tmp_path / "agent_sessions"
        d.mkdir(exist_ok=True)
        monkeypatch.setattr(S, "SESSION_LOG_DIR", d)
        log = d / "utf8run.jsonl"
        rows = [
            {
                "ts": "2026-08-14T12:00:00+00:00",
                "event": "session_start",
                "run_id": "utf8run",
                "symbol": "QQQ",
            },
            # `ensure_ascii=False` KASITLI: diskteki gerçek dosyalar böyle —
            # ham UTF-8 baytları, \uXXXX kaçışı değil.
            {
                "ts": "2026-08-14T12:00:05+00:00",
                "event": "step",
                "run_id": "utf8run",
                "msg": "🌐 Web araştırması yapılıyor — göstergeler ayrıştırılıyor",
            },
            {
                "ts": "2026-08-14T12:01:00+00:00",
                "event": "session_end",
                "run_id": "utf8run",
                "outcome": "winner",
            },
        ]
        log.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )

        summary = S._session_summary("utf8run")
        assert summary["run_id"] == "utf8run"
        # Türkçe/emoji satırın ÖTESİ okunabilmiş olmalı: `session_end` o
        # satırdan SONRA geliyor, yani `outcome` doluysa çözümleme kesilmemiştir.
        assert summary["outcome"] == "winner"
        assert summary["symbol"] == "QQQ"
        assert summary["n_step"] >= 1

    def test_a_genuinely_corrupt_byte_does_not_kill_the_page(
        self, tmp_path, monkeypatch
    ):
        """`errors="replace"` sözleşmesi: bozuk tek bayt oturumu yutmasın.

        Özet bir GÖRÜNÜM; yarım yazılmış bir satır yüzünden liste sayfasının
        tamamının kaybolması, bilgiyi korumak değil yok etmektir.
        """
        d = tmp_path / "agent_sessions"
        d.mkdir(exist_ok=True)
        monkeypatch.setattr(S, "SESSION_LOG_DIR", d)
        log = d / "corrupt.jsonl"
        good = json.dumps(
            {
                "ts": "2026-08-14T12:00:00+00:00",
                "event": "session_start",
                "run_id": "corrupt",
            }
        ).encode("utf-8")
        log.write_bytes(good + b"\n" + b'{"event": "step", "msg": "\x90\x90"}\n')

        summary = S._session_summary("corrupt")  # patlamamalı
        assert summary["run_id"] == "corrupt"
