---
source: https://nautilustrader.io/docs/latest/concepts/architecture
retrieved: 2026-07-05
type: docs_snapshot
immutable: true
---

# Architecture (Docs) Snapshot

## Tasarım Felsefesi
Domain-Driven Design, event-driven mimari, publish/subscribe messaging, ports & adapters, crash-only design. Öncelik sırası: güvenilirlik → performans → modülerlik → test edilebilirlik → sürdürülebilirlik → deploy edilebilirlik.

## Ana Bileşenler

### NautilusKernel
Merkezi orkestrasyon. Bileşenleri başlatır, messaging altyapısını yapılandırır, environment-specific davranışları yönetir, paylaşımlı kaynakları koordine eder.

### MessageBus
Bileşenler arası iletişim omurgası. Pub/Sub, Request/Response, Command/Event mesajlaşma; opsiyonel state persistence.

### Cache
Yüksek performanslı in-memory storage; instrument, hesap, emir ve pozisyonları tutar.

### DataEngine
Piyasa verisini (quote, trade, bar, order book, custom) dış kaynaklardan alıp abonelere yönlendirir.

### ExecutionEngine
Emir yaşam döngüsünü yönetir: adaptör routing, order/position state, risk koordinasyonu, fill handling, external state reconciliation.

### RiskEngine
Pre-trade validation, pozisyon izleme, gerçek zamanlı risk hesaplama, konfigüre edilebilir risk kuralları.

## Paket Organizasyonu

**Core/Düşük seviye**: core, common, model, network, serialization

**Bileşen paketleri**: accounting, adapters, analysis, cache, data, execution, indicators, persistence, portfolio, risk, trading

**Sistem uygulamaları**: backtest, live, system (kernel)

## Environment Contexts
- **Backtest**: Tarihsel veri + simüle venue
- **Sandbox**: Gerçek zaman veri + simüle venue
- **Live**: Gerçek zaman veri + gerçek venue (paper veya canlı)

## Temel İlkeler
- **Single-threaded core**: Kernel içi mesajlar tek thread'de dispatch → deterministik sıralama, backtest-live parity
- **Crash-only design**: Startup ve crash recovery aynı yol → dışsallaştırılmış state, hızlı restart
- **Fail-fast**: Arithmetic over/underflow, deserialization hataları, tip dönüşüm hataları → panic ya da error; sessiz veri bozulması yok
