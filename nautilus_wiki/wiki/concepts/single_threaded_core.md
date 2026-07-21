---
title: Single-Threaded Core
type: concept
sources:
  - sources/02_architecture_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/architecture
last_updated: 2026-07-09
summary: Kernel'in mesajları tek thread'de dispatch etmesinin deterministik sıralama, backtest-live parity ve blocking iş yasağı gerekçelerini açıklar.
key_concepts:
  - nautilus_kernel
  - message_bus
  - crash_only_design
  - event_driven_architecture
  - strategy_and_actor
---

# Single-Threaded Core

Node içindeki [[nautilus_kernel]] mesajları tek bir thread'de tüketip dispatch eder. Resmi dokümantasyon bunu açıkça şöyle belirtir: **"Within a node, the kernel consumes and dispatches messages on a single thread."** (kaynak: docs/latest/concepts/architecture)

## Neden
- **Deterministik sıralama**: Aynı input → aynı output; concurrency race'i yok
- **Backtest-live parity**: Simülasyondaki sıralama canlıda da geçerli — "NautilusTrader deploys backtested strategies to live markets with no code changes" (kaynak: docs/latest/concepts/live)
- **Basit muhakeme**: Kilit/senkronizasyon karmaşası yok

## Background Services — Ayrı Thread'lerde

Tek thread sadece kernel dispatch'i kapsar. Ağ/persistence/adapter gibi arka plan servisleri **ayrı thread veya async runtime** üzerinde çalışır ve sonuçlarını [[message_bus]] üzerinden kernel'e geri gönderir. Bu ayrım sayesinde I/O bekleme kernel'i bloke etmez.

## [[message_bus]] ile Entegrasyon

[[message_bus]] iki iletişim modeli destekler (doğrulanmış):
- **Publish/Subscribe** — event broadcasting; bir bileşen publish eder, tüm subscriber'lar alır
- **Request/Response** — acknowledgment gerektiren senkron-benzeri işlemler

Tüm bileşenler (DataEngine, ExecutionEngine, RiskEngine, Strategy) birbirlerine doğrudan çağrı değil, MessageBus mesajlarıyla haberleşir.

## Sınırlamalar
CPU-yoğun tek işlem çekirdeği durdurur. Uzun süreli hesaplamalar Actor içinde blocking yapılmamalı; ağır iş harici sürece taşınmalı.

## Related
- [[crash_only_design]]
- [[message_bus]]
- [[event_driven_architecture]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[getting_started_roadmap]]
- [[nau_performans_denetimi]]
- [[nautilus_kernel]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
