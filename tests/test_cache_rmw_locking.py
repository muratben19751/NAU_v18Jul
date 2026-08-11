"""Index/external cache: oku-değiştir-yaz dizisi kilit altında.

DeepR 2026-08-11 [ORTA]: `_atomic_to_parquet` ATOMİK yazıyor (yarım dosya
kalmaz), ama atomiklik KAYIP GÜNCELLEMEYİ engellemez. `load_index_bars`
(oku → tara → concat → yaz) ve `load_external_bars` (oku → decode → yaz)
bu diziyi hiçbir kilit almadan yapıyordu: eşzamanlı iki yazar birbirinin
verisini kaybettirir. `load_external_bars` gerçekten eşzamanlı çağrılıyor
(studio `NautilusBacktestAdapter.load_bars` loader'ı bilinçli olarak kilit
DIŞINDA çağırır; `web/routes/lab.py` ve `web/routes/agent_backtest.py` kendi
thread'lerinden çağırır), `load_index_bars` ise iki eşzamanlı
/data/refresh/index ya da /backtest/sweep ile.

Uygulanan desen `load_bybit_bars`'ınkiyle AYNI (2026-08-11'de orada yeniden
düzenlendi, üçüncü bir kilit stili yok):
  1. Okuma yolu kilitsiz — yazma atomik olduğu için okuyan ya eski ya yeni
     dosyanın TAMAMINI görür.
  2. Yazma yolu `_cache_lock` altında ve kilidi aldıktan SONRA diski TAZE
     okur: beklerken öbür yazar zaten getirmiş olabilir.

Wiki References
---------------
Bkz: [[parquet_data_catalog]], [[index_backtest_via_equity_proxy]]
"""

from __future__ import annotations

import threading
from datetime import date

import pandas as pd
import pytest

import data

_D1 = date(2024, 3, 4)
_D2 = date(2024, 3, 5)


def _rows(day: date, value: float) -> pd.DataFrame:
    ts = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=12)
    return pd.DataFrame(
        {"ticker": ["I:SPX"], "value": [value], "timestamp": [ts.value]}
    )


@pytest.fixture()
def index_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "INDEX_CACHE_DIR", tmp_path / "index")
    data.INDEX_CACHE_DIR.mkdir(parents=True)
    # `_index_file_for` yalnız "boş gün kalıcı mı" kararında okunur; tarama
    # zaten monkeypatch'li, kaynak dosyanın varlığı bu testin konusu değil.
    monkeypatch.setattr(data, "INDEX_ROOT", tmp_path / "src")
    return data.INDEX_CACHE_DIR / "I_SPX_1d.parquet"


class TestLoadIndexBarsConcurrentWriters:
    def test_two_writers_do_not_lose_each_others_days(self, index_cache, monkeypatch):
        """A yavaş bir günü tarar, B hızlı başka bir günü tarayıp yazar.

        Kilitsiz hâlde A, B'nin yazdığı günü GÖRMEDEN üstüne yazıyordu:
        cache tek günle kalıyor, öbür gün sessizce kayboluyordu.
        """
        b_written = threading.Event()
        a_started = threading.Event()

        def _stream(ticker: str, day: date) -> pd.DataFrame:
            if day == _D1:  # yavaş yazar (A)
                a_started.set()
                b_written.wait(timeout=10)
                return _rows(_D1, 100.0)
            return _rows(_D2, 200.0)

        monkeypatch.setattr(data, "_stream_ticker_rows", _stream)

        errors: list[BaseException] = []

        def _run(day: date, wait_for_a: bool):
            try:
                if wait_for_a:
                    a_started.wait(timeout=10)
                data.load_index_bars("I:SPX", day, day)
            except BaseException as e:  # noqa: BLE001 — thread hatası testte görünsün
                errors.append(e)

        ta = threading.Thread(target=_run, args=(_D1, False))
        tb = threading.Thread(target=_run, args=(_D2, True))
        ta.start()
        tb.start()
        # B'nin yazması A'nın yazmasından ÖNCE bitmeli: kilitsiz kodda bu, A'nın
        # elindeki bayat görüntünün B'nin gününü ezmesi demekti.
        tb.join(timeout=15)
        b_written.set()
        ta.join(timeout=15)

        assert not errors, errors
        cached = pd.read_parquet(index_cache)
        days = set(cached.index.normalize().date.tolist())
        assert days == {_D1, _D2}, f"kayıp güncelleme: {days}"

    def test_read_only_call_does_not_take_the_lock(self, index_cache, monkeypatch):
        """Cache yeterliyse kilide HİÇ girilmez (bybit yolundaki duruş).

        Kilit tutulurken bile tamamen önbellekten karşılanan bir çağrı
        beklememelidir; aksi hâlde uzun bir tarama tüm okuyucuları kilitler.
        """
        monkeypatch.setattr(
            data, "_stream_ticker_rows", lambda *a, **k: _rows(_D1, 100.0)
        )
        data.load_index_bars("I:SPX", _D1, _D1)  # cache'i doldur

        def _boom(*a, **k):
            raise AssertionError("okuma yolu kilit almamalı")

        monkeypatch.setattr(data, "_cache_lock", _boom)
        out = data.load_index_bars("I:SPX", _D1, _D1)
        assert len(out) == 1


class TestLoadExternalBarsConcurrentDecoders:
    def test_decode_and_write_runs_under_the_lock(self, tmp_path, monkeypatch):
        """Decode+yazma dizisi kilit altında olmalı; ikinci çağıran kilidi
        aldıktan sonra TAZE okuyup yeniden decode ETMEMELİ."""
        monkeypatch.setattr(data, "EXTERNAL_CACHE_DIR", tmp_path / "extcache")
        root = tmp_path / "cat"
        bar_dir = root / "data" / "bar" / "ZZ.NASDAQ-1-DAY-LAST-EXTERNAL"
        bar_dir.mkdir(parents=True)
        (bar_dir / "part-0.parquet").write_bytes(b"not-a-real-parquet")
        monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [root])

        held: list[str] = []
        real_lock = data._cache_lock

        def _tracking_lock(lock_path, *a, **k):
            held.append(lock_path.name)
            return real_lock(lock_path, *a, **k)

        monkeypatch.setattr(data, "_cache_lock", _tracking_lock)

        decodes = {"n": 0}

        class _Bar:
            def __init__(self, ts):
                self.ts_event = ts
                self.open = self.high = self.low = self.close = 1.0
                self.volume = 1.0

        class _FakeCatalog:
            def __init__(self, _root):
                pass

            def bars(self, bar_types=None):
                decodes["n"] += 1
                base = int(pd.Timestamp("2024-03-04", tz="UTC").value)
                return [_Bar(base + i * 86_400_000_000_000) for i in range(3)]

        import nautilus_trader.persistence.catalog as _cat_mod

        monkeypatch.setattr(_cat_mod, "ParquetDataCatalog", _FakeCatalog)

        first = data.load_external_bars("ZZ.NASDAQ", "1-DAY")
        second = data.load_external_bars("ZZ.NASDAQ", "1-DAY")

        assert len(first) == 3 and len(second) == 3
        assert decodes["n"] == 1, "ikinci çağrı cache'ten karşılanmalıydı"
        # Yazma yolu kilide girdi, cache'ten karşılanan ikinci çağrı girmedi.
        assert held == ["ZZ.NASDAQ_1-DAY.lock"]
