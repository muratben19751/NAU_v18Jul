---
title: Crash-Only Design
type: concept
sources:
  - sources/02_architecture_docs.md
last_updated: 2026-07-05
summary: Tek recovery path olarak startup'ı benimser; state'i message_bus üzerinden dışsallaştırır, fail-fast panic ile sessiz bug'ları eler.
key_concepts:
  - message_bus
  - single_threaded_core
  - event_driven_architecture
  - nautilus_kernel
---

# Crash-Only Design

Sistemin yalnızca tek bir "recovery path"i vardır: startup. Ayrı bir graceful shutdown yolağı bakımı zorlaştırır ve sessiz bug'lar üretir.

## Uygulama Yolları
- State dışsallaştırılır (opsiyonel Redis persistence via [[message_bus]])
- Restart hızlıdır ve idempotenttir
- Beklenmedik durumda fail-fast (panic/error) tercih edilir; sessiz yanlış davranış yerine yeniden başlatma

## Fail-Fast Kuralları
Aşağıdaki durumlar panic veya error döner:
- Arithmetic overflow/underflow
- Deserialization sırasında geçersiz veri
- Type conversion hataları

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[event_sourcing]]
- [[live_node]]
- [[message_bus]]
- [[order_emulator]]
- [[single_threaded_core]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
