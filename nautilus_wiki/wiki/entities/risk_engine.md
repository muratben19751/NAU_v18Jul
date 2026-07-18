---
title: RiskEngine
type: entity
sources:
  - sources/02_architecture_docs.md
last_updated: 2026-07-05
summary: Strategy ile ExecutionEngine arası pre-trade validation, pozisyon izleme ve limit kurallarını uygulayan risk kontrol katmanını özetler.
key_concepts:
  - execution_engine
  - order_flow_pipeline
  - strategy_and_actor
  - event_driven_architecture
---

# RiskEngine

Emir hattı üzerinde risk kontrol katmanı.

## İşlevler
- Pre-trade validation (limit kontrolü, sembol izinleri)
- Pozisyon izleme
- Gerçek zamanlı risk hesaplama
- Konfigüre edilebilir kural/limit seti

## Konumu
Varsayılan olarak Strategy → ExecutionEngine yolunda; execution algorithm ve emulator opsiyoneldir. Bkz. [[execution_engine]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[execution_algorithms]]
- [[execution_engine]]
- [[instruments]]
- [[nautilus_kernel]]
- [[order_emulator]]
- [[order_flow_pipeline]]
- [[rust_python_hybrid]]
- [[tutorial_grid_market_maker_bitmex]]
<!-- BACKLINKS:END -->
