"""Bybit cache: okuma yolu kilitsiz, yazma yolu hâlâ serileştirilmiş.

DeepR 2026-08-11 [YÜKSEK]: `load_bybit_bars` KOŞULSUZ olarak exclusive
`_cache_lock`'a giriyordu -- cache tamamen yetse, ağa hiç çıkılmayacak olsa
bile. Kilit ağ I/O'sunu da kapsadığı ve uzun bir backfill'de ~10 dk tutulduğu
için, o sırada aynı (kategori, sembol, aralık) serisini yalnızca OKUMAK
isteyen herkes -- /data sayfası, backtest, loop_runner'ın iterasyon başı
refresh'i, parallel_exec işçileri -- önce bir thread'i bekletiyor, sonra
120 saniyelik timeout'a takılıp TimeoutError alıyordu. Yani bekleme, tek
anlamlı sonucu (öbürünün indirdiği veriyi kullanmak) tam da kaçıracak kadar
kısaydı.

Düzeltme iki parçalı:
1. Okuma yolu kilide hiç girmez. Yazma `os.replace` ile atomik olduğundan
   okuyan ya eski ya yeni dosyanın TAMAMINI görür.
2. Bekleme süresi gerçek tutma süresinin üstüne çekildi (`_CACHE_LOCK_TIMEOUT_S`),
   ama "terk edilmiş kilit" eşiğinin (`stale_after`) altında kaldı -- canlı iş
   beklenir, ölü kilit kırılır (M121'in dersi bozulmadan).

Wiki References
---------------
See: [[parquet_data_catalog]], [[nau_deepr_ucuncu_tur_2026_08_09]].
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

import data

BASE = datetime(2024, 3, 1, tzinfo=UTC)
STEP_MS = data._BYBIT_MS["1"]


def _frame(start: datetime, n: int) -> pd.DataFrame:
    idx = pd.to_datetime(
        [int((start + timedelta(minutes=i)).timestamp() * 1000) for i in range(n)],
        unit="ms",
        utc=True,
    )
    return pd.DataFrame(
        {c: [1.0] * n for c in ("open", "high", "low", "close", "volume")}, index=idx
    )


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """60 dakikalık taze bir cache + izole edilmiş cache/katalog kökleri."""
    monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path / "bybit")
    monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
    data.BYBIT_CACHE_DIR.mkdir(parents=True)
    # Katalog yazımı bu dosyanın konusu değil; gerçek Nautilus yazımı testi
    # yavaşlatmaktan başka bir şey ölçmez.
    monkeypatch.setattr(data, "_auto_write_bybit_catalog", lambda *a, **k: None)
    path = data._bybit_cache_path("linear", "BTCUSDT", "1")
    _frame(BASE, 60).to_parquet(path)
    return path


def _no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("ağa çıkılmamalıydı — istek cache'ten karşılanabilir")

    monkeypatch.setattr(data, "_fetch_bybit_page", _boom)


class TestCoveredReadTakesNoLock:
    def test_a_fully_covered_request_never_enters_the_cache_lock(
        self, cache, monkeypatch
    ):
        _no_network(monkeypatch)

        def _forbidden(*a, **k):
            raise AssertionError("okuma yolu kilit almamalı")

        monkeypatch.setattr(data, "_cache_lock", _forbidden)

        out = data.load_bybit_bars(
            "BTCUSDT",
            interval="1",
            category="linear",
            start=BASE + timedelta(minutes=10),
            end=BASE + timedelta(minutes=40),
        )

        assert len(out) == 30

    def test_a_read_is_not_blocked_by_an_in_flight_download(self, cache, monkeypatch):
        """Denetlenen senaryo: indirme sürerken /data ya da backtest OKUMAK istiyor."""
        _no_network(monkeypatch)
        holding, release = threading.Event(), threading.Event()

        def _pretend_download():
            # Gerçek yazarın yaptığı: kilidi al ve uzun süre tut.
            with data._cache_lock(cache.with_suffix(".lock")):
                holding.set()
                release.wait(timeout=10)

        writer = threading.Thread(target=_pretend_download)
        writer.start()
        assert holding.wait(timeout=5), "yazar kilidi alamadı"
        try:
            t0 = time.monotonic()
            out = data.load_bybit_bars(
                "BTCUSDT",
                interval="1",
                category="linear",
                start=BASE + timedelta(minutes=5),
                end=BASE + timedelta(minutes=50),
            )
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            writer.join(timeout=5)

        assert len(out) == 45
        # Eski davranışta bu satıra ancak 120 s sonra TimeoutError ile gelinirdi.
        assert elapsed < 2.0, f"okuma kilidi bekledi ({elapsed:.1f}s)"

    def test_reads_stay_consistent_while_a_writer_extends_the_cache(
        self, cache, monkeypatch
    ):
        """Eşzamanlı okuma + yazma: okuyan hiçbir zaman yarım dosya görmez."""

        # Yazar cache'in KUYRUĞUNU uzatıyor; her sayfa biraz sürüyor.
        def _slow_fetch(category, symbol, interval, start_ms, end_ms):
            time.sleep(0.01)
            ts = list(range(start_ms, end_ms + 1, STEP_MS))
            if not ts:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            idx = pd.to_datetime(ts, unit="ms", utc=True)
            n = len(ts)
            return pd.DataFrame(
                {c: [2.0] * n for c in ("open", "high", "low", "close", "volume")},
                index=idx,
            )

        monkeypatch.setattr(data, "_fetch_bybit_page", _slow_fetch)
        errors: list[Exception] = []

        def _writer():
            try:
                data.load_bybit_bars(
                    "BTCUSDT",
                    interval="1",
                    category="linear",
                    start=BASE,
                    end=BASE + timedelta(minutes=600),
                )
            except Exception as e:  # noqa: BLE001 — ana thread'e taşınıyor
                errors.append(e)

        t = threading.Thread(target=_writer)
        t.start()
        try:
            for _ in range(20):
                out = data.load_bybit_bars(
                    "BTCUSDT",
                    interval="1",
                    category="linear",
                    start=BASE,
                    end=BASE + timedelta(minutes=60),
                )
                # Cache hiçbir an küçülmez ve okunan pencere hep tam gelir.
                assert len(out) == 60
        finally:
            t.join(timeout=30)

        assert not errors, errors
        assert not t.is_alive()
        # Yazar işini bitirdi: kuyruk gerçekten uzadı.
        assert len(pd.read_parquet(cache)) > 60


class TestWritePathStillSerialized:
    def test_two_concurrent_writers_do_not_lose_each_other_s_bars(
        self, cache, monkeypatch
    ):
        """M3b bozulmadı: okuma-değiştirme-yazma hâlâ tek sırada."""
        gate = threading.Barrier(2, timeout=10)

        def _fetch(category, symbol, interval, start_ms, end_ms):
            ts = list(range(start_ms, end_ms + 1, STEP_MS))
            if not ts:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            idx = pd.to_datetime(ts, unit="ms", utc=True)
            n = len(ts)
            return pd.DataFrame(
                {c: [3.0] * n for c in ("open", "high", "low", "close", "volume")},
                index=idx,
            )

        monkeypatch.setattr(data, "_fetch_bybit_page", _fetch)
        errors: list[Exception] = []

        def _run(start, end):
            try:
                gate.wait()  # ikisi de aynı anda kilide koşsun
                data.load_bybit_bars(
                    "BTCUSDT",
                    interval="1",
                    category="linear",
                    start=start,
                    end=end,
                )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        head = threading.Thread(
            target=_run, args=(BASE - timedelta(minutes=120), BASE + timedelta(hours=1))
        )
        tail = threading.Thread(target=_run, args=(BASE, BASE + timedelta(minutes=180)))
        head.start()
        tail.start()
        head.join(timeout=30)
        tail.join(timeout=30)

        assert not errors, errors
        combined = pd.read_parquet(cache)
        # İki yazarın da getirdiği barlar diskte: biri diğerinin yazdığını ezmedi.
        assert combined.index[0] <= pd.Timestamp(BASE - timedelta(minutes=119))
        assert combined.index[-1] >= pd.Timestamp(BASE + timedelta(minutes=170))

    def test_a_waiting_writer_re_reads_under_the_lock_and_refetches_nothing(
        self, cache, monkeypatch
    ):
        """Kilit altında TAZE okuma: beklerken gelen veri ikinci kez indirilmez."""
        calls = {"n": 0}

        def _count(*a, **k):
            calls["n"] += 1
            raise AssertionError("bekleyen yazar aynı barları yeniden indirdi")

        holding, release = threading.Event(), threading.Event()

        def _holder():
            with data._cache_lock(cache.with_suffix(".lock")):
                holding.set()
                # Beklerken cache tam olarak isteneni kapsayacak hâle geliyor.
                _frame(BASE - timedelta(minutes=60), 240).to_parquet(cache)
                release.wait(timeout=10)

        t = threading.Thread(target=_holder)
        t.start()
        assert holding.wait(timeout=5)
        monkeypatch.setattr(data, "_fetch_bybit_page", _count)
        # İstek şu an EKSİK görünüyor (baş 60 dk daha geride) → yazma yoluna girer
        # ve kilidi bekler. Kilit bırakıldığında cache zaten yeterlidir.
        done = threading.Event()
        out: list[pd.DataFrame] = []

        def _waiter():
            out.append(
                data.load_bybit_bars(
                    "BTCUSDT",
                    interval="1",
                    category="linear",
                    start=BASE - timedelta(minutes=60),
                    end=BASE + timedelta(minutes=60),
                )
            )
            done.set()

        w = threading.Thread(target=_waiter)
        w.start()
        time.sleep(0.3)  # bekleyen gerçekten kilitte sıraya girsin
        release.set()
        t.join(timeout=5)
        assert done.wait(timeout=10), "bekleyen yazar hiç dönmedi"
        w.join(timeout=5)

        assert calls["n"] == 0
        assert len(out[0]) == 120


class TestCacheCoverageDecision:
    def test_a_missing_head_is_not_covered(self, cache):
        cached = pd.read_parquet(cache)

        assert not data._bybit_cache_covers(
            cached,
            cache,
            int((BASE - timedelta(minutes=10)).timestamp() * 1000),
            int((BASE + timedelta(minutes=10)).timestamp() * 1000),
            STEP_MS,
        )

    def test_a_missing_tail_is_not_covered(self, cache):
        cached = pd.read_parquet(cache)

        assert not data._bybit_cache_covers(
            cached,
            cache,
            int(BASE.timestamp() * 1000),
            int((BASE + timedelta(minutes=600)).timestamp() * 1000),
            STEP_MS,
        )

    def test_an_empty_cache_is_never_covered(self, cache):
        assert not data._bybit_cache_covers(pd.DataFrame(), cache, 0, 1, STEP_MS)

    def test_a_hole_inside_the_range_is_not_covered(self, cache):
        # Ortadan 10 bar sil: M2'nin delik doldurması hâlâ tetiklenmeli, yani
        # bu istek "yalnız cache'ten karşılanır" sayılmamalı.
        holed = pd.concat([_frame(BASE, 20), _frame(BASE + timedelta(minutes=30), 30)])
        holed.to_parquet(cache)

        assert not data._bybit_cache_covers(
            holed,
            cache,
            int(BASE.timestamp() * 1000),
            int((BASE + timedelta(minutes=40)).timestamp() * 1000),
            STEP_MS,
        )

    def test_a_hole_already_known_empty_counts_as_covered(self, cache):
        """Bilinen boş aralık için ağa çıkılmıyorsa kilide de gerek yok."""
        holed = pd.concat([_frame(BASE, 20), _frame(BASE + timedelta(minutes=30), 30)])
        holed.to_parquet(cache)
        gs = int((BASE + timedelta(minutes=20)).timestamp() * 1000)
        ge = int((BASE + timedelta(minutes=29)).timestamp() * 1000)
        data._atomic_write_json([[gs, ge]], data._bybit_gaps_path(cache))

        assert data._bybit_cache_covers(
            holed,
            cache,
            int(BASE.timestamp() * 1000),
            int((BASE + timedelta(minutes=40)).timestamp() * 1000),
            STEP_MS,
        )


class TestLockWaitBudget:
    def test_the_default_wait_exceeds_a_real_backfill_hold(self):
        # Modülün kendi belgelediği en uzun meşru tutma: ~10 dk.
        assert data._CACHE_LOCK_TIMEOUT_S >= 600.0

    def test_the_wait_stays_below_the_stale_threshold(self):
        # Aksi hâlde bekleyen, canlı bir kilidi "terk edilmiş" sanıp kırardı —
        # M121'in tam olarak düzelttiği hata.
        assert data._CACHE_LOCK_TIMEOUT_S < 1800.0


class TestLockFreeReadRobustness:
    def test_a_transient_sharing_violation_is_retried_not_called_corrupt(
        self, cache, monkeypatch, caplog
    ):
        """Windows'ta yazar `os.replace` yaparken okuma bir kez düşebilir."""
        real = pd.read_parquet
        calls = {"n": 0}

        def _flaky(path, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("The process cannot access the file")
            return real(path, *a, **k)

        monkeypatch.setattr(pd, "read_parquet", _flaky)
        with caplog.at_level("WARNING"):
            out = data._read_bybit_cache(cache)

        assert len(out) == 60
        assert "corrupt" not in caplog.text

    def test_a_genuinely_corrupt_cache_still_degrades_to_empty(
        self, cache, monkeypatch, caplog
    ):
        # M3c korunuyor: bozuk parquet sessizce yutulmaz, uyarı düşer.
        monkeypatch.setattr(
            pd,
            "read_parquet",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("not a parquet file")),
        )
        with caplog.at_level("WARNING"):
            out = data._read_bybit_cache(cache)

        assert out.empty
        assert "corrupt bybit cache" in caplog.text

    def test_replace_retries_a_windows_permission_error(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "a.tmp", tmp_path / "a.parquet"
        src.write_text("new")
        dst.write_text("old")
        real_replace = data.os.replace
        calls = {"n": 0}

        def _flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("used by another process")
            return real_replace(a, b)

        monkeypatch.setattr(data.os, "replace", _flaky)
        data._replace_with_retry(src, dst)

        assert dst.read_text() == "new"
        assert calls["n"] == 2
