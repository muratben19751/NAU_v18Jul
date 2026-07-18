---
title: Neden Rust + Python Hibrit?
type: synthesis
sources:
  - sources/01_readme_snapshot.md
  - sources/02_architecture_docs.md
last_updated: 2026-07-05
summary: Rust çekirdek + Python kontrol düzlemi + Cython köprüsünün latency/güvenlik ile hızlı iterasyon trade-off'unu nasıl dengelediğini analiz eder.
key_concepts:
  - single_threaded_core
  - environment_contexts
  - message_bus
  - strategy_and_actor
  - crash_only_design
---

# Rust + Python Hibrit Tasarım — Trade-off Analizi

## Sorun
Trading motorlarında iki karşıt talep vardır:
1. **Performans ve güvenlik** — düşük latency, race'siz eşzamanlılık, sessiz bug yok
2. **Hızlı iterasyon** — strateji araştırması, deneme, notebook, rich ekosistem

Tek dilli çözümler bir tarafı feda eder. C++/Rust motorları esnek strateji katmanı sunmaz; saf Python motorları latency ve güvenlik açığı taşır.

## NautilusTrader'ın Cevabı
- **Rust çekirdek**: [[message_bus|MessageBus]], engines ([[data_engine]] / [[execution_engine]] / [[risk_engine]]), matching, model tipleri
- **Python kontrol düzlemi**: [[strategy_and_actor|Strategy]], konfigürasyon, notebook entegrasyonu
- **Cython köprü katmanı**: Bir kısım sarmalayıcılar

Dil dağılımı: Rust %70.8, Python %22.8, Cython %5.4.

## Trade-off
| Avantaj | Maliyet |
|---|---|
| Deterministik ve hızlı motor | Derleme zamanı build karmaşıklığı (Rust toolchain, clang) |
| Bellek ve tip güvenliği | Wheel dağıtımı platform-spesifik |
| Notebook-friendly strateji katmanı | FFI sınırında dikkat gereken durumlar |

## Sonuç
Backtest–live parity ile birleştiğinde, hibrit yaklaşım "research-to-production" boşluğunu daraltır. [[environment_contexts]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[configuration]]
- [[environment_contexts]]
- [[plugins]]
<!-- BACKLINKS:END -->
