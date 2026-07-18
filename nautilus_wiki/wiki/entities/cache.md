---
title: Cache
type: entity
sources:
  - sources/02_architecture_docs.md
  - sources/03_strategies_docs.md
last_updated: 2026-07-05
summary: Strategy'nin instrument, hesap, emir, pozisyon ve son piyasa verilerini doğrudan sorgulayabildiği in-memory yüksek performanslı depolama katmanı.
key_concepts:
  - strategy_and_actor
  - execution_engine
  - data_engine
  - message_bus
  - event_driven_architecture
---

# Cache

Yüksek performanslı in-memory storage.

## İçerik
- Instrument tanımları
- Hesap durumları
- Emirler ve pozisyonlar
- Piyasa verisi (son değerler)

## Erişim
Strategy Cache'e doğrudan erişebilir; bu, execution objelerini ve son piyasa durumunu sorgulamayı sağlar.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[custom_data]]
- [[data_wranglers]]
- [[event_sourcing]]
- [[events]]
- [[instruments]]
- [[live_node]]
- [[nautilus_kernel]]
- [[option_greeks_pipeline]]
- [[order_book]]
- [[order_emulator]]
- [[positions]]
- [[reports]]
- [[strategy_and_actor]]
- [[synthetics]]
- [[venue_reconciliation]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
