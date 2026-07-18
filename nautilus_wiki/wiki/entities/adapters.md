---
title: Adapters
type: entity
sources:
  - sources/01_readme_snapshot.md
last_updated: 2026-07-05
summary: 17+ hazır entegrasyon (Binance, IB, Databento, Betfair vb.) ports-and-adapters mimarisi ile core motoru değiştirmeden REST+WebSocket üzerinden bağlanır.
key_concepts:
  - event_driven_architecture
  - data_engine
  - execution_engine
  - message_bus
  - environment_contexts
---

# Adapters (Entegrasyonlar)

Ports-and-adapters mimarisinde dış sistemlerle konuşan modüller. 17+ hazır entegrasyon:

## Kripto Merkeziyetli
- Binance
- Coinbase
- Kraken
- OKX
- Bybit
- Deribit

## DEX
- dYdX
- Hyperliquid
- Derive
- Lighter

## Geleneksel Piyasa
- Interactive Brokers

## Veri Sağlayıcı
- Databento
- Tardis

## Bahis
- Betfair
- Polymarket

## Genişletme
Yeni bir venue için REST + WebSocket üzerinden adaptör yazılabilir; core motor değiştirilmeden entegre olur.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[data_engine]]
- [[execution_engine]]
- [[howto_get_started_lighter]]
- [[instruments]]
- [[live_node]]
- [[option_greeks_pipeline]]
- [[order_flow_pipeline]]
- [[tutorial_backtest_orderbook_binance]]
- [[tutorial_backtest_orderbook_bybit]]
- [[tutorial_data_catalog_databento]]
- [[tutorial_delta_neutral_derive]]
- [[tutorial_grid_market_maker_bitmex]]
- [[tutorial_grid_market_maker_dydx]]
- [[tutorial_lighter_rwa_composite_mm]]
- [[tutorial_options_data_bybit]]
<!-- BACKLINKS:END -->
