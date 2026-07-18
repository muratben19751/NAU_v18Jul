---
title: MessageBus
type: entity
sources:
  - sources/02_architecture_docs.md
last_updated: 2026-07-05
summary: Pub/sub, request/response ve command/event desenleriyle bileşen iletişimini taşıyan omurga; opsiyonel Redis kalıcılığıyla crash-only tasarıma uyumlu.
key_concepts:
  - crash_only_design
  - event_driven_architecture
  - data_engine
  - execution_engine
  - strategy_and_actor
  - risk_engine
---

# MessageBus

Bileşenler arası iletişimin omurgası.

## Desteklenen Desenler
- **Publish/Subscribe** — Konu bazlı yayın
- **Request/Response** — Senkron sorgu-yanıt
- **Command/Event** — Komut ve olay ayrımı

## State Persistence
Opsiyonel olarak (ör. Redis) mesaj/state kalıcılığı yapılandırılabilir. Crash-only tasarımla uyumlu: state dışsallaştırılır, restart hızlıdır. Bkz. [[crash_only_design]].

## Kullanım Konumları
- DataEngine → Actor/Strategy'e veri yayını
- Strategy → ExecutionEngine'e komut
- ExecutionEngine → Portfolio/Risk'e olay

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[crash_only_design]]
- [[custom_data]]
- [[data_engine]]
- [[data_wranglers]]
- [[event_driven_architecture]]
- [[event_sourcing]]
- [[events]]
- [[live_node]]
- [[nautilus_kernel]]
- [[option_greeks_pipeline]]
- [[order_emulator]]
- [[rust_python_hybrid]]
- [[single_threaded_core]]
- [[v1_to_v2_migration_lessons]]
<!-- BACKLINKS:END -->
