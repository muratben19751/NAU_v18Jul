"""Regresyon çapaları (fixture-ağır) — parquet cache + Nautilus katalog + gerçek
motor koşusu gerektiren, denetim dalgasının en yüksek blast-radius'lu kalan
düzeltmeleri. Pür-birim çapalar için bkz. test_regression_anchors.py.

Kapsanan düzeltmeler:
- #4 (H626)  load_bybit_bars force_refresh cache MERGE (concat+dedup keep='last')
- #5 (M1030) delete_data_range hatası → yazımı ATLA (non-disjoint parquet önlemi)
- #6         composer long→short FLIP yolu (_cancel_working + tags=['flip'])

Her test GERÇEK shipping edilen fonksiyonu sürer; fix geri alınırsa FAIL eder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest


# ===========================================================================
# #4 (critical, H626) — force_refresh cache MERGE (truncate DEĞİL)
# /data refresh butonu (days=7) yılların 1m geçmişini 7 güne kırpıyordu (kalıcı
# parquet veri kaybı). force_refresh=True mevcut cache'i OKUYUP dar pencereyi
# concat+dedup(keep='last') ile birleştirmeli.
# ===========================================================================
def _refresh_fetch(step_ms, marker):
    """Sentetik _fetch_bybit_page: yalnız DAR yeniden-çekilen pencereyi döndürür,
    her kolonda ayırt edici bir değer (marker) ile (open-time UTC index)."""

    def _fetch(category, symbol, interval, start_ms, end_ms):
        ts = list(range(start_ms, end_ms + 1, step_ms))
        if not ts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(ts, unit="ms", utc=True)
        n = len(ts)
        return pd.DataFrame(
            {c: [marker] * n for c in ("open", "high", "low", "close", "volume")},
            index=idx,
        )

    return _fetch


class TestForceRefreshMerge:
    def test_force_refresh_merges_not_truncates(self, tmp_path, monkeypatch):
        import data

        monkeypatch.setattr(data, "BYBIT_CACHE_DIR", tmp_path / "bybit")
        monkeypatch.setattr(data, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
        data.BYBIT_CACHE_DIR.mkdir(parents=True)
        # Katalog auto-write cache-merge'e dik; nautilus_trader'a bağımlı olmasın.
        monkeypatch.setattr(data, "_auto_write_bybit_catalog", lambda *a, **k: None)

        step_ms = data._BYBIT_MS["1"]
        SEED = 1.0
        REFRESH = 999.0
        monkeypatch.setattr(data, "_fetch_bybit_page", _refresh_fetch(step_ms, REFRESH))

        # GENİŞ cache seed: 100 birer-dakikalık bar, tüm kolonlar == SEED.
        base = datetime(2024, 1, 1, tzinfo=UTC)
        cache_idx = pd.to_datetime(
            [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(100)],
            unit="ms",
            utc=True,
        )
        cache_df = pd.DataFrame(
            {c: [SEED] * 100 for c in ("open", "high", "low", "close", "volume")},
            index=cache_idx,
        )
        cache_path = data._bybit_cache_path("linear", "BTCUSDT", "1")
        cache_df.to_parquet(cache_path)

        # /data refresh butonu: seed aralığının derininde DAR bir pencere (40..50).
        start = base + timedelta(minutes=40)
        end = base + timedelta(minutes=50)
        data.load_bybit_bars(
            "BTCUSDT",
            interval="1",
            category="linear",
            start=start,
            end=end,
            force_refresh=True,
        )

        merged = pd.read_parquet(cache_path)

        # 1) Pencere-dışı GEÇMİŞ hayatta: cache hâlâ tüm geniş aralığı kapsıyor.
        assert merged.index[0] == pd.Timestamp(base)
        assert merged.index[-1] == pd.Timestamp(base + timedelta(minutes=99))
        assert len(merged) == 100

        # 2) Pencere-dışı barlar SEED değerini korur (refresh dokunmadı).
        assert merged.loc[pd.Timestamp(base), "open"] == SEED
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=99)), "open"] == SEED
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=30)), "open"] == SEED

        # 3) Pencere-içi barlar REFETCH değerini yansıtır (dedup keep='last').
        assert merged.loc[pd.Timestamp(base + timedelta(minutes=45)), "open"] == REFRESH
        assert (
            merged.loc[pd.Timestamp(base + timedelta(minutes=40)), "close"] == REFRESH
        )


# ===========================================================================
# #5 (critical, M1030) — delete_data_range hatası yazımı ATLAMALI
# Benign değil ('no data'/'not found' DIŞI, ör. PermissionError) bir silme hatası
# sonrası write_data ÇAĞRILIRSA eski+yeni örtüşen (non-disjoint) parquet aralıkları
# kalır → sonraki katalog okuması sert-patlar. write_to_nautilus_catalog RAISE,
# _auto_write_bybit_catalog sessiz RETURN. Benign hata → yazıma DEVAM.
# ===========================================================================
class _FakeCatalog:
    """delete_data_range'i kontrol edilebilir, write_data'yı kaydeden sahte katalog."""

    def __init__(self, delete_error: Exception | None = None):
        self.delete_error = delete_error
        self.write_calls: list[str] = []  # her write_data payload tipi
        self.delete_called = False

    def write_data(self, data):
        # data.py write_data'yı önce [instrument] ile, SONRA bars ile çağırır.
        self.write_calls.append(type(data[0]).__name__ if data else "empty")

    def delete_data_range(self, **kw):
        self.delete_called = True
        if self.delete_error is not None:
            raise self.delete_error

    @property
    def bar_writes(self) -> int:
        return self.write_calls.count("Bar")


def _seed_bybit_cache(data_mod, tmp_path, monkeypatch):
    """5 barlık minik bybit parquet cache — delete satırına ulaşmaya yeter.
    monkeypatch ile atanır (doğrudan atama modül globalini sızdırırdı)."""
    monkeypatch.setattr(data_mod, "BYBIT_CACHE_DIR", tmp_path / "bybit")
    monkeypatch.setattr(data_mod, "NAUTILUS_CATALOG_DIR", tmp_path / "cat")
    data_mod.BYBIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 10, tzinfo=UTC)
    idx = pd.to_datetime(
        [int((base + timedelta(minutes=i)).timestamp() * 1000) for i in range(5)],
        unit="ms",
        utc=True,
    )
    df = pd.DataFrame(
        {c: [1.0] * 5 for c in ("open", "high", "low", "close", "volume")},
        index=idx,
    )
    df.to_parquet(data_mod._bybit_cache_path("linear", "BTCUSDT", "1"))
    return df


class TestDeleteRangeFailureSkipsWrite:
    def test_hard_delete_error_raises_and_skips_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=PermissionError("denied"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        with pytest.raises(RuntimeError, match="delete_data_range"):
            data.write_to_nautilus_catalog(
                "bybit", symbol="BTCUSDT", category="linear", interval="1"
            )

        assert fake.delete_called, "delete dalına ulaşılmadı — fixture yetersiz"
        # M1030 çekirdeği: başarısız silmeden SONRA bars YAZILMAMALI.
        assert fake.bar_writes == 0, (
            f"delete başarısızken bars yazıldı ({fake.bar_writes}) — "
            "örtüşen parquet aralıkları kalır, katalog okuması patlar"
        )

    def test_benign_no_data_error_proceeds_to_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=RuntimeError("no data for identifier"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        out = data.write_to_nautilus_catalog(
            "bybit", symbol="BTCUSDT", category="linear", interval="1"
        )

        assert fake.delete_called
        assert fake.bar_writes == 1, "benign silme yazıma engel olmamalı"
        assert out["rows_written"] == 5

    def test_clean_delete_proceeds_to_write(self, tmp_path, monkeypatch):
        import data

        _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=None)
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        out = data.write_to_nautilus_catalog(
            "bybit", symbol="BTCUSDT", category="linear", interval="1"
        )
        assert fake.bar_writes == 1
        assert out["rows_written"] == 5

    def test_auto_write_hard_delete_error_returns_without_write(
        self, tmp_path, monkeypatch
    ):
        # _auto_write_bybit_catalog: aynı M1030 dalı ama RAISE değil sessiz RETURN.
        import data

        df = _seed_bybit_cache(data, tmp_path, monkeypatch)
        fake = _FakeCatalog(delete_error=PermissionError("denied"))
        monkeypatch.setattr(data, "get_nautilus_catalog", lambda: fake)

        assert (
            data._auto_write_bybit_catalog("BTCUSDT", "1", df, category="linear")
            is None
        )
        assert fake.delete_called
        assert fake.bar_writes == 0, "delete başarısızken _auto_write bars yazmamalı"


# ===========================================================================
# #6 (critical) — composer long→short FLIP yolu
# allow_short hiçbir testte True değildi → reversal dalı (on_bar: _cancel_working
# + close_all_positions(tags=['flip']) sonra ters taraf) hiç sürülmüyordu.
# ===========================================================================
class TestComposerFlipPath:
    def test_long_to_short_reversal_is_observable(self):
        from composer import ComposedStrategySpec, SignalBlock
        from tests.test_trade_reasons import _run

        spec = ComposedStrategySpec(
            id="flip1",
            name="Flip E2E",
            description="",
            blocks=[
                # Altın kesişim → long
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "up"},
                ),
                # Ölüm kesişimi → short (pozisyon long iken flip tetikler)
                SignalBlock(
                    type="ma_cross",
                    role="entry",
                    params={"fast": 5, "slow": 20, "direction": "down"},
                ),
            ],
            trade_size=0.1,
            allow_short=True,  # reversal yolunun tek anahtarı
        )
        r = _run(spec)  # _bars() sinüsü birden çok altın/ölüm kesişimi üretir
        assert r.error is None, r.error
        trades = r.trades or []
        assert trades, "hiç trade yok — flip yolu sürülemedi"

        sides = [t["side"] for t in trades]
        kinds = [t["exit_kind"] for t in trades]

        # (1) Ters taraf GERÇEKTEN açıldı: en az bir net-short (SELL). allow_short
        # kapalıyken (mevcut tüm testler) bu ASLA oluşmaz.
        assert any(s == "SELL" for s in sides), f"short pozisyon yok: {sides}"

        # (2) 'flip' çıkış-atfı: close_all_positions(tags=['flip']) yalnız reversal
        # dalında çağrılır → exit_kind=='flip' flip yolunun kesin izi.
        assert any(k == "flip" for k in kinds), f"flip atfı yok: {kinds}"

        # (3) Yön DÖNÜŞÜ oldu: ardışık zıt-taraf pozisyonlar (M17 guard pozisyonu
        # FLAT bırakmadı — yeni giriş dolabildi).
        assert any(a != b for a, b in zip(sides, sides[1:])), (
            f"taraf dönüşü yok: {sides}"
        )
