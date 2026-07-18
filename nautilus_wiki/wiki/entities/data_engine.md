---
title: DataEngine
type: entity
sources:
  - sources/02_architecture_docs.md
  - sources/04_backtesting_docs.md
last_updated: 2026-07-13
summary: Adapters'tan gelen tick/bar/order book verisini message_bus üzerinden Actor/Strategy'e yönlendirir; book_type granülaritesi kaynak veriyle eşleşmelidir.
key_concepts:
  - adapters
  - message_bus
  - strategy_and_actor
  - event_driven_architecture
---

# DataEngine

Piyasa verisini dış kaynaklardan alır ve abonelere yönlendirir.

## İşlediği Veri Türleri
- Quote tick
- Trade tick
- Bar (OHLCV)
- Order book (L2 market-by-price, L3 market-by-order) — bkz. [[order_book]]
- Özel (custom) veri türleri — bkz. [[custom_data]]

Verinin ait olduğu enstrüman tanımları ve tip hiyerarşisi için bkz. [[instruments]].

## Granülarite Kuralı
Venue'nun `book_type` konfigürasyonu, sağlanan verinin seviyesiyle eşleşmelidir. Düşük seviye veriden yüksek seviye üretilemez (ör. bar'dan L2 kitap türetilemez).

## İlişkiler
- Kaynak: [[adapters]]
- Tüketici: Actor/Strategy → [[message_bus]] üzerinden

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
- [[bar_aggregation_and_type_syntax]]
- [[continuous_futures]]
- [[custom_data]]
- [[data_wranglers]]
- [[dst]]
- [[events]]
- [[instruments]]
- [[nautilus_kernel]]
- [[option_greeks_pipeline]]
- [[order_book]]
- [[rust_python_hybrid]]
- [[synthetics]]
- [[tutorial_backtest_orderbook_binance]]
- [[tutorial_backtest_orderbook_bybit]]
- [[tutorial_book_imbalance_betfair]]
- [[tutorial_data_catalog_databento]]
- [[tutorial_fx_mean_reversion_ax]]
- [[tutorial_gold_book_imbalance_ax]]
- [[tutorial_loading_external_data]]
- [[tutorial_options_data_bybit]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
