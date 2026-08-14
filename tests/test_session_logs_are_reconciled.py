"""Sahipsiz AUTO oturum logu dürüst bir sona bağlansın — geçmişe dokunmadan.

`session_end` 12 ayrı çağrı yerinde yazılıyor ve hiçbiri dış bir `finally`
içinde değil; süreç ölürse (pm2 restart, çökme) hiçbiri koşmaz. Ölçüm
2026-08-14: 118 oturumun 62'sinde terminal olay yok.

Bunun bedeli kaybolan tek satır değil, telafinin ÇOĞALMASI: `/sessions` açık
span'leri okuma anında `warn` ile kapatıyor, `_terminal_message` logun son
satırına bakıp "muhtemelen sunucu yeniden başladı" diye tahmin yürütüyor,
`scripts/auto_postmortem.py` aynı tahmini bir kez daha yazıyor — ve hiçbiri
"çöktü mü, hâlâ koşuyor mu" sorusunu kesin cevaplayamıyor. Cevap yalnızca
BELLEKTEKİ koşu kaydında var (`live_run_ids`), dosyanın içinde değil.

İki sert kural bu dosyada bağlanıyor:

  1. **Geçmişe dokunulmaz.** İlk uzlaştırma yalnız bir su işareti bırakır;
     ondan eski loglar olduğu gibi kalır. Append-only bir günlüğe bugünkü
     tarihle "şu tarihte kesildi" yazmak kaydı düzeltmek değil bulandırmaktır.
  2. **Canlı koşu ölü ilan edilmez.** Hâlâ yazan bir logun son satırı elbette
     `session_end` değildir; onu kesinti sayan bir uzlaştırma, koşarken
     kendini bitmiş gösteren bir arayüz üretirdi.

Wiki References: [[auto_mission_control]], [[webapp_module_map]]
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import web.routes.sessions as S


def _write_log(directory: Path, run_id: str, events: list[dict]) -> Path:
    p = directory / f"{run_id}.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return p


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _mark_after_watermark(logdir: Path, p: Path) -> None:
    """Logu su işaretinden KESİN sonra yazılmış göster.

    Zamanlamaya güvenmek testi kırılgan yapar: su işaretiyle aynı tikte
    oluşan bir dosya "geçmiş" sayılır ve test yük altında düşer (tam süitte
    bir kez böyle düştü). Sınırı ölçmek isteyen ayrı bir test var; bu
    yardımcı, ondan BAĞIMSIZ testlerin sınıra hiç değmemesini sağlar.
    """
    import os

    cutoff = (logdir / S._WATERMARK_NAME).stat().st_mtime
    os.utime(p, (cutoff + 5, cutoff + 5))


def _half_finished(run_id: str = "r1") -> list[dict]:
    """Bir `timeline` span'i açıkken kesilmiş koşu — en sık ölüm şekli (%58)."""
    return [
        {"ts": "2026-08-14T12:00:00+00:00", "event": "session_start", "run_id": run_id},
        {
            "ts": "2026-08-14T12:01:00+00:00",
            "event": "timeline",
            "run_id": run_id,
            "op": "begin",
            "key": "llm-r1",
        },
    ]


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    # conftest.py bu dizini zaten kuruyor (oturum loglarının gerçek
    # ~/.cache'e sızmasını engelleyen otomatik fixture) — `exist_ok` şart.
    d = tmp_path / "agent_sessions"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(S, "SESSION_LOG_DIR", d)
    monkeypatch.setattr(S, "_RECONCILED", set())
    # Varsayılan: hiçbir koşu canlı değil.
    monkeypatch.setattr(
        "web.routes.agent_backtest.live_run_ids", lambda: set(), raising=True
    )
    return d


class TestHistoryIsNeverTouched:
    def test_the_first_run_only_arms_the_watermark(self, logdir):
        p = _write_log(logdir, "eski", _half_finished("eski"))
        before = p.read_text(encoding="utf-8")

        assert S._reconcile_session_logs() == 0
        assert p.read_text(encoding="utf-8") == before, (
            "İlk uzlaştırma geçmiş bir loga DOKUNDU. Su işaretinden eski kayıtlar "
            "tarihsel veridir; bugünkü tarihle kapatmak onları düzeltmez, bulandırır."
        )
        assert (logdir / S._WATERMARK_NAME).exists()

    def test_a_log_older_than_the_watermark_stays_untouched(self, logdir):
        S._reconcile_session_logs()  # su işaretini kur
        p = _write_log(logdir, "eski", _half_finished("eski"))
        import os

        # Eşik su işaretinin KENDİ mtime'ı — içeriği değil. İçine duvar saati
        # yazıp onu okumak iki farklı saat kaynağını karşılaştırmak olurdu ve
        # sınırda yanlış taraf seçilebilirdi (tam süitte bir kez böyle düştü).
        cutoff = (logdir / S._WATERMARK_NAME).stat().st_mtime
        os.utime(p, (cutoff - 100, cutoff - 100))  # su işaretinden ESKİ
        before = p.read_text(encoding="utf-8")

        assert S._reconcile_session_logs() == 0
        assert p.read_text(encoding="utf-8") == before

    def test_the_boundary_favours_leaving_the_log_alone(self, logdir):
        """Su işaretiyle AYNI anda yazılmış log: dokunma.

        Sınırda bir taraf seçilmek zorunda ve güvenli taraf budur —
        `interrupted` damgası geri alınamaz bir yazımdır, oysa dokunmamak
        yalnızca bir sonraki koşuya erteler. Eşiğin ve dosyanın damgası
        aynı alandan (dosya sistemi) okunduğu için bu karşılaştırma artık
        tutarlı; iki farklı saat kaynağı kullanıldığında sınırın hangi
        tarafına düşüleceği yük altında değişiyordu.
        """
        S._reconcile_session_logs()
        p = _write_log(logdir, "sinir", _half_finished("sinir"))
        import os

        cutoff = (logdir / S._WATERMARK_NAME).stat().st_mtime
        os.utime(p, (cutoff, cutoff))  # TAM sınırda
        before = p.read_text(encoding="utf-8")

        assert S._reconcile_session_logs() == 0
        assert p.read_text(encoding="utf-8") == before


class TestAnOrphanedLogGetsAnHonestEnding:
    def test_a_log_written_after_the_watermark_is_closed(self, logdir):
        S._reconcile_session_logs()  # su işaretini kur
        p = _write_log(logdir, "yeni", _half_finished("yeni"))
        _mark_after_watermark(logdir, p)

        assert S._reconcile_session_logs() == 1
        last = _events(p)[-1]
        assert last["event"] == "session_end"
        assert last["outcome"] == "interrupted"
        assert last["last_event"] == "timeline"

    def test_the_stamp_is_the_silence_not_the_detection(self, logdir):
        """`ts` son olayın zamanı olmalı — tespit zamanı ayrı alanda.

        Tespit saatlerce sonra (bir sonraki /sessions ziyaretinde) olur;
        onu `ts`'e yazmak koşuyu saatler sonra bitmiş gösterip zaman
        çizgisini bozardı.
        """
        S._reconcile_session_logs()
        p = _write_log(logdir, "yeni", _half_finished("yeni"))
        _mark_after_watermark(logdir, p)
        S._reconcile_session_logs()

        last = _events(p)[-1]
        assert last["ts"] == "2026-08-14T12:01:00+00:00"
        assert last["detected_at"] != last["ts"]

    def test_interrupted_is_not_error(self, logdir):
        """Kesinti bir arıza yargısı DEĞİL — olmamış bir hata raporlanmamalı."""
        S._reconcile_session_logs()
        p = _write_log(logdir, "yeni", _half_finished("yeni"))
        _mark_after_watermark(logdir, p)
        S._reconcile_session_logs()

        last = _events(p)[-1]
        assert last["outcome"] == "interrupted"
        assert "error" not in last

    def test_reconciling_twice_does_not_append_twice(self, logdir):
        S._reconcile_session_logs()
        p = _write_log(logdir, "yeni", _half_finished("yeni"))
        _mark_after_watermark(logdir, p)

        assert S._reconcile_session_logs() == 1
        assert S._reconcile_session_logs() == 0, (
            "İkinci koşu aynı loga bir kez daha yazdı — artık son olay "
            "`session_end`, dokunulmamalıydı."
        )
        assert sum(1 for e in _events(p) if e["event"] == "session_end") == 1


class TestALiveRunIsNeverDeclaredDead:
    def test_a_running_run_is_skipped(self, logdir, monkeypatch):
        S._reconcile_session_logs()
        p = _write_log(logdir, "kosuyor", _half_finished("kosuyor"))
        _mark_after_watermark(logdir, p)
        monkeypatch.setattr(
            "web.routes.agent_backtest.live_run_ids", lambda: {"kosuyor"}
        )
        before = p.read_text(encoding="utf-8")

        assert S._reconcile_session_logs() == 0
        assert p.read_text(encoding="utf-8") == before, (
            "Hâlâ koşan bir oturum `interrupted` işaretlendi — arayüz canlı bir "
            "koşuyu bitmiş gösterirdi."
        )

    def test_a_properly_finished_run_is_left_alone(self, logdir):
        S._reconcile_session_logs()
        p = _write_log(
            logdir,
            "bitti",
            _half_finished("bitti")
            + [
                {
                    "ts": "2026-08-14T12:05:00+00:00",
                    "event": "session_end",
                    "run_id": "bitti",
                    "outcome": "winner",
                }
            ],
        )
        before = p.read_text(encoding="utf-8")

        assert S._reconcile_session_logs() == 0
        assert p.read_text(encoding="utf-8") == before


class TestTheGuardNeverBreaksThePage:
    def test_a_failing_reconciliation_is_swallowed(self, logdir, monkeypatch):
        def boom():
            raise RuntimeError("disk gitti")

        monkeypatch.setattr(S, "_reconcile_session_logs", boom)
        S._reconcile_session_logs_once()  # patlamamalı

    def test_it_runs_only_once_per_process(self, logdir):
        calls = []
        original = S._reconcile_session_logs
        try:
            S._reconcile_session_logs = lambda: calls.append(1)
            S._reconcile_session_logs_once()
            S._reconcile_session_logs_once()
        finally:
            S._reconcile_session_logs = original
        assert len(calls) == 1
