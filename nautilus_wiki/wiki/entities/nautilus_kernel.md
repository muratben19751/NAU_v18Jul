---
title: NautilusKernel
type: entity
sources:
  - sources/02_architecture_docs.md
last_updated: 2026-07-13
summary: Node içindeki alt sistemleri başlatan ve koordine eden merkezi orkestratör; single_threaded_core ile deterministik dispatch ve environment_contexts davranışı sağlar.
key_concepts:
  - message_bus
  - data_engine
  - execution_engine
  - risk_engine
  - cache
  - single_threaded_core
  - environment_contexts
---

# NautilusKernel

Sistemin merkezi orkestrasyon bileşenidir. Node içindeki tüm alt sistemleri başlatır ve yaşam döngülerini koordine eder.

## Sorumluluklar
- Bileşen başlatma sırası
- MessageBus yapılandırması
- Environment-specific davranış (Backtest / Sandbox / Live)
- Paylaşımlı kaynakların (Clock, Cache) yönetimi

## Tek Thread İlkesi
Kernel içindeki mesajlar tek bir thread'de tüketilip dispatch edilir. Bu, deterministik olay sıralaması sağlar ve backtest–live davranış paritesinin temelidir. Bkz. [[single_threaded_core]].

## İlişkili Bileşenler
- [[message_bus]]
- [[data_engine]]
- [[execution_engine]]
- [[risk_engine]]
- [[cache]]

Kernel'in kuruluş parametreleri [[configuration]] nesneleri üzerinden verilir; günlükleme altyapısının kuruluşu için bkz. [[logging]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[configuration]]
- [[event_sourcing]]
- [[howto_get_started_lighter]]
- [[live_node]]
- [[logging]]
- [[plugins]]
- [[single_threaded_core]]
- [[tutorial_backtest_high_level]]
- [[tutorial_quickstart]]
- [[v1_to_v2_migration_lessons]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
