---
title: Event-Driven Architecture
type: concept
sources:
  - sources/02_architecture_docs.md
last_updated: 2026-07-13
summary: Bileşenlerin doğrudan çağrı yerine message_bus üzerinden mesajlaştığı omurgayı; decoupling, replay edilebilirlik ve backtest-live parity sonuçlarıyla açıklar.
key_concepts:
  - message_bus
  - data_engine
  - execution_engine
  - risk_engine
  - strategy_and_actor
---

# Event-Driven Architecture

NautilusTrader'ın omurgası: bileşenler doğrudan çağrılarla değil, [[message_bus]] üzerinden mesajlarla haberleşir.

## Sonuçları
- **Ayrışıklık (decoupling)**: Bileşenler birbirlerini tanımaz, sadece mesaj tiplerini bilir
- **Test edilebilirlik**: Mesaj dizisi replay edilebilir
- **Backtest-live parity**: Aynı mesaj hattı hem simüle hem gerçek modda çalışır

## Domain-Driven Design ile İlişki
Model paketi zengin domain nesneleri tanımlar (order, position, instrument); mesajlar bu nesneler etrafında şekillenir. Mesajların taşıdığı somut olay hiyerarşisi için bkz. [[events]]; durumun olaylardan yeniden inşası için [[event_sourcing]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[event_sourcing]]
- [[events]]
- [[getting_started_roadmap]]
- [[single_threaded_core]]
- [[tutorial_hurst_vpin_kraken]]
- [[tutorial_lighter_rwa_composite_mm]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
