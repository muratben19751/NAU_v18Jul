---
title: Backtesting Guide — Hangi API'yi Seçmeli?
type: synthesis
sources:
  - sources/04_backtesting_docs.md
  - sources/02_architecture_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/backtesting
last_updated: 2026-07-13
summary: BacktestEngine ve BacktestNode arasında seçim rehberi; ts_init look-ahead bias kuralı; ParquetDataCatalog dual-backend; book_type-veri granülarite eşleşmesi.
key_concepts:
  - backtest_node
  - parquet_data_catalog
  - data_engine
  - environment_contexts
  - bar_aggregation_and_type_syntax
---

# Backtesting Rehberi

## API Seçimi — Resmi Öneri

Resmi dokümantasyon açık bir tavsiye veriyor (doğrulanmış):

> **"Use BacktestNode for config-driven backtesting with the Parquet data catalog. This is the recommended path for production workflows because the strategies, actors, and execution algorithms you build here carry forward to live trading with TradingNode."**

| | BacktestEngine | BacktestNode |
|---|---|---|
| Karmaşıklık | Low-level, manual | High-level, config-driven |
| Veri kaynağı | DataFrame direkt | ParquetDataCatalog |
| Paralel sweep | Manuel | BacktestRunConfig kopyalanabilir |
| TradingNode uyumu | Strateji kodu taşınabilir | Tam uyumlu |
| Kullanım yeri | Hızlı deney | Üretim iş akışı |

**[[backtest_node|BacktestNode]] (high-level) seç eğer:**
- [[parquet_data_catalog|ParquetDataCatalog]] kullanmak istersen
- Veri bellekten büyükse
- Birden çok konfigürasyon sweep yapacaksan
- Üretim-grade live trading hedefliyorsan

**BacktestEngine (low-level) seç eğer:**
- Veri bellekte tutulabiliyorsa
- Hızlı deney ve CSV/binary ham formatı tercih ediyorsan
- Bileşen üzerinde manuel kontrol istiyorsan

## Kritik: ts_init = Bar Kapanış Zamanı (Look-Ahead Bias)

> **"Nautilus strictly expects the initialization timestamp (ts_init) of each bar to represent its closing time to prevent look-ahead bias."**

Eğer veriniz open-timestamped ise `ts_init_delta` ile kaydır. Bu kural [[parquet_data_catalog]] içinde de geçerli.

## Performans İpuçları
1. Çok-instrument yüklemede her `add_data()` sonrası sıralama yapma; sonda tek sefer sırala **veya** tümünü toplayıp tek batch ekle.
2. Bar verisinde adaptive high/low sıralamayı aç; aynı bar içinde TP ve SL varsa fill doğruluğu artar.

## Veri Granülaritesi Uyumu
Venue `book_type` ↔ veri seviyesi eşleşmesi:

| book_type | Gereken veri |
|---|---|
| L3 (MBO) | L3 order book |
| L2 (MBP) | L2 quote / market depth |
| L1 | Quote tick |
| Trade-only | Trade tick |
| Bar | Bar (OHLCV) |

Düşük seviyeden yüksek üretilemez.

## BacktestDataConfig — v2.0.0rc1 Kritik Kısıtlamalar

### bar_spec değil, bar_types kullan

`BacktestDataConfig.bar_spec` parametresi v2.0.0rc1'de **çalışmıyor** — `TypeError` verir. Bunun yerine `bar_types` liste parametresini kullan:

```python
BacktestDataConfig(
    catalog_path=str(catalog.path),
    data_type=Bar,
    instrument_ids=["BTCUSDT.BYBIT"],
    bar_types=["BTCUSDT.BYBIT-1-MINUTE-LAST-EXTERNAL"],  # ✓ çalışıyor
    # bar_spec=BarSpecification(...)  # ✗ TypeError alınır
)
```

### start_time / end_time: nanosecond int zorunlu

`datetime` nesnesi geçirilirse `TypeError` alınır. Nanosecond int olarak ilet:

```python
import pandas as pd

start_ns = pd.Timestamp("2024-01-01", tz="UTC").value   # nanosecond int
end_ns   = pd.Timestamp("2024-01-08", tz="UTC").value

BacktestDataConfig(
    ...,
    start_time=start_ns,
    end_time=end_ns,
)
```

### ComposedStrategy ImportableStrategyConfig uyumu

`ComposedStrategyConfig.__init__` `str → BarType / InstrumentId` otomatik dönüşümü yapar; bu sayede `ImportableStrategyConfig` (dict/str değerler ile) ile sorunsuz çalışır. Bu pattern tüm custom `StrategyConfig` sınıfları için önerilir (bkz. [[strategy_and_actor]]).

## İlgili Sayfalar
- [[environment_contexts]]
- [[data_engine]]
- [[bar_aggregation_and_type_syntax]] — BarType DSL ve aggregation modları
- [[visualization]] — backtest sonuçlarının görselleştirilmesi
- [[tutorial_backtest_fx_bars]] — bar-based FX backtest örneği
- [[tutorial_book_imbalance_betfair]] — L2 book imbalance backtest
- [[tutorial_hurst_vpin_kraken]] — dollar-bar + toxicity göstergesi

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[accounting]]
- [[backtest_node]]
- [[environment_contexts]]
- [[getting_started_roadmap]]
- [[index_backtest_via_equity_proxy]]
- [[reports]]
- [[tutorial_backtest_fx_bars]]
- [[tutorial_backtest_high_level]]
- [[tutorial_backtest_low_level]]
- [[tutorial_backtest_orderbook_bybit]]
- [[tutorial_book_imbalance_betfair]]
- [[tutorial_data_catalog_databento]]
- [[tutorial_fx_mean_reversion_ax]]
- [[tutorial_hurst_vpin_kraken]]
- [[tutorial_loading_external_data]]
- [[visualization]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
