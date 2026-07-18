---
title: ParquetDataCatalog
type: entity
status: draft
sources:
  - sources/04_backtesting_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/data
  - https://nautilustrader.io/docs/latest/concepts/backtesting
last_updated: 2026-07-09
summary: Tick/quote/bar ve instrument tanımlarını nanosaniye hassasiyetiyle Parquet olarak kalıcılaştıran; Rust + PyArrow dual-backend; BacktestNode ve LiveNode ortak veri deposu.
related:
  - wiki/tutorials/tutorial_loading_external_data.md
  - wiki/tutorials/tutorial_data_catalog_databento.md
---

# ParquetDataCatalog

Nautilus'un enstrümanları ve piyasa verisini (tick, quote, bar) Parquet formatında kalıcılaştıran ve nanosaniye hassasiyetli zaman damgalarıyla sorgulanabilir kılan merkezi veri katmanı. High-level backtest API'sinin ([[backtest_node]]) beklenen veri kaynağıdır; low-level `BacktestEngine` doğrudan ham CSV/binary ile de çalışabildiği için katalog opsiyoneldir.

## Dual-Backend Mimarisi (doğrulanmış — kaynak: docs/latest/concepts/data)

`ParquetDataCatalog` iki backend kullanır:

| Backend | Hangi tipler | Açıklama |
|---|---|---|
| **Rust** | `OrderBookDelta`, `OrderBookDeltas`, `OrderBookDepth10`, `QuoteTick`, `TradeTick`, `Bar`, `MarkPriceUpdate` (7 tip) | Optimize edilmiş sorgu performansı |
| **PyArrow** | Custom data types | Esnek fallback |

v2.0.0rc1'de `ParquetDataCatalog` tamamen compiled pyo3 Rust sınıfı — `inspect.getsource()` çalışmaz, tüm metodlar `method_descriptor` tipinde.

## Kritik: ts_init Her Zaman Kapanış Zamanı Olmalı

> **"Nautilus strictly expects the initialization timestamp (ts_init) of each bar to represent its closing time to prevent look-ahead bias."**
> (kaynak: docs/latest/concepts/backtesting)

Eğer ham veriniz open-timestamped ise (örn. "09:00 başlayan bar" → "09:00" ts_init), `ts_init_delta` parametresiyle shift yapılmalı:

```python
# ts_init_delta = bar süresini nanosaniye cinsinden kaydır
wrangler = BarDataWrangler(bar_type, price_precision, size_precision)
bars = wrangler.process_record_batch_bytes(data, ts_init_delta=60_000_000_000)  # 1 dakika = 60s
```

Look-ahead bias uyarısı: ts_init bar açılış zamanını gösteriyorsa engine o barın kapatma fiyatını henüz bilemez ama strateji görebilir → gerçekçi olmayan PnL.

## v1.231.0 Bug Fix (develop branch, henüz release edilmedi)

> "Fixed v2 internal bar aggregation to include the first tick when aggregating from ticks, quotes, or trades in backtests"

v2.0.0rc1'de bar aggregation'da ilk tick dahil edilmiyordu. Develop branch'de düzeltildi; RC2 veya v1.231.0 final'de gelecek.

## v2 rc1 Gerçek API (canlı doğrulanmış)

`nautilus_trader.persistence` modülü, v2rc1'de `write_data` yerine ayrı `write_*` metodları sağlıyor:

```python
from nautilus_trader.persistence import ParquetDataCatalog

catalog = ParquetDataCatalog("./catalog")

# Instrument kayıt
catalog.write_instruments([instrument])

# Bar yaz (idempotent yapmak için önce eski aralığı sil)
catalog.delete_data_range(type_name="bars", instrument_id=str(bar_type))
catalog.write_bars(bars)

# Okuma
bars = catalog.query_bars(identifiers=["BTCUSDT.BYBIT-1-MINUTE-LAST-EXTERNAL"])
```

Önemli detaylar:
- `delete_data_range(type_name="bars", ...)` — `type_name` **lowercase** string; `"Bar"` desteklenmiyor.
- `query_bars(identifiers=[str(bar_type)])` — `bar_type` string DSL (`"SYMBOL.VENUE-STEP-AGG-SRC-ORIGIN"`), `InstrumentId` değil.
- `list_instruments("bars")` — katalogda kayıtlı bar_type ID string listesi döner.
- Disk düzeni: `data/bars/<bar_type>/<ts_start>_<ts_end>.parquet` + `data/instruments/<instrument_id>/<timestamps>.parquet`.

## Timestamp Zorunluluğu: Nanosaniye int64

Katalog ve BacktestNode, `ts_event`/`ts_init` alanlarında **int64 nanosaniye** bekler. `_bars_from_df` helper:

```python
# Yanlış (ms): df.index.astype("int64") → Bybit ms-resolution index ile hatalı sonuç
# Doğru (ns):
idx = df.index.tz_localize(None)  # timezone-aware index için şart
ts_ns = idx.astype("datetime64[ns]").astype("int64").to_numpy()
```

Eğer ms değerler kullanılırsa BacktestNode veriyi `1970-01-01` zamanında çalıştırır ve hiç emir üretmez.

## BacktestNode ile Kullanım Örüntüsü

```python
from nautilus_trader.backtest import (
    BacktestDataConfig, BacktestRunConfig, BacktestVenueConfig, BacktestNode,
)
from nautilus_trader.model import BarSpecification, OmsType, AccountType, BookType
from nautilus_trader.trading import ImportableStrategyConfig

# Katalogda bar olduğunu varsayarak:
run_cfg = BacktestRunConfig(
    engine=engine_cfg,
    venues=[BacktestVenueConfig(name="BYBIT", oms_type=OmsType.NETTING,
                                account_type=AccountType.CASH,
                                book_type=BookType.L1_MBP,
                                starting_balances=["1000000 USDT"])],
    data=[BacktestDataConfig(catalog_path="./catalog",
                             data_type="Bar",
                             instrument_id=inst_id)],
)
node = BacktestNode(configs=[run_cfg])
node.build()
node.add_strategy_from_config(run_cfg.id, importable_strategy_cfg)
results = node.run()  # BacktestResult(stats_general, stats_pnls, stats_returns)
```

Önemli not: `add_strategy_from_config(run_cfg_id, config)` çağrısı `build()` **sonrasında** yapılmalı. Aksi hâlde `RuntimeError: No engine for run config` hatası alınır.

`ImportableStrategyConfig` stratejileri `strategy_path='strategies:MACrossoverStrategy'` ve `config_path='strategies:MACrossoverConfig'` biçiminde yükler; config dict içindeki `Decimal` değerleri string'e dönüşür — strategy `make_qty(str)` yerine `make_qty(float(...))` kullanmalı.

## Ne Zaman Kullanılır

`ParquetDataCatalog`, veri bellek kapasitesini aştığında, birden çok backtest konfigürasyonu paralel yönetileceğinde veya [[backtest_node]] kolaylığı istendiğinde tercih edilir. Tüm veri belleğe sığıyorsa low-level `BacktestEngine` daha basittir.

**Karşılaştırma (backtest.py'de canlı doğrulandı):**

| Özellik | `BacktestEngine` | `BacktestNode` |
|---|---|---|
| Veri kaynağı | pandas DataFrame | `ParquetDataCatalog` |
| Timestamp hassasiyeti | index resolution (ms/us/ns) | int64 nanosaniye zorunlu |
| Sharpe (v2 rc1) | **NaN** (bug) | Doğru hesaplanıyor |
| Win-rate paritesi | Referans | Δ = 0.000000 (tam) |
| Strateji kayıt yöntemi | `engine.add_strategy(...)` | `node.add_strategy_from_config(id, ImportableStrategyConfig)` |

## Bilinen boşluklar

- Trade count `stats_general`'da `"Total Trade Count"` anahtarı v2 rc1 `BacktestNode`'da görünmüyor (sadece `"Long Ratio"`).
- `catalog.query_bars()` `start`/`end` nanosaniye parametreleri test edilmedi — tam zaman aralıklı sorgulama.
- Databento `.dbn.zst` yükleyicisi entegrasyonu (tutorial 404 nedeniyle kaynak `.py`'den okunmalı).
- Order book / L2 / L3 verisi ve `write_order_book_deltas()` yolu test edilmedi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtest_node]]
- [[backtesting_guide]]
- [[custom_data]]
- [[data_wranglers]]
- [[event_sourcing]]
- [[getting_started_roadmap]]
- [[option_greeks_pipeline]]
- [[value_types]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
