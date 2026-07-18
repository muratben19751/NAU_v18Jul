---
title: Venue Reconciliation
type: concept
status: draft
sources:
  - sources/02_architecture_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/architecture
  - https://nautilustrader.io/docs/latest/concepts/live
last_updated: 2026-07-09
summary: Canlı başlatma/restart'ta venue state ile iç Cache'i hizalama akışı; sadece LiveExecutionEngine uygular çünkü backtest her iki tarafı da kontrol eder.
related:
  - wiki/tutorials/tutorial_delta_neutral_bybit.md
  - wiki/tutorials/tutorial_grid_market_maker_bitmex.md
  - wiki/entities/execution_engine.md
---

# Venue Reconciliation

Venue reconciliation, NautilusTrader'ın canlı başlatma veya yeniden başlatma anında hesap, açık emir ve pozisyon durumunu venue'dan yeniden okuyup iç state ile hizalama sürecidir.

## Kritik: Sadece LiveExecutionEngine uygular

Araştırmayla doğrulanmış önemli bir ayrım (kaynak: docs/latest/concepts/architecture):

> **"Only the LiveExecutionEngine performs reconciliation, since backtesting controls both sides of execution."**

Backtest'te engine hem strateji tarafını hem venue simülatörü tarafını kontrol ettiği için reconciliation anlamsızdır. Bu nedenle `BacktestEngine` ve `BacktestNode`'da reconciliation API'si bulunmaz. Reconciliation tamamen live trading (`TradingNode`) scope'undadır.

## Ne zaman devreye girer

- Canlı çalıştırmalarda [[live_node]] açılış hidrasyonunda: strateji henüz komut üretmeden önce açık emirler ve pozisyonlar çekilir.
- Yeniden başlatma senaryosunda: son bilinen state kaybolduğunda venue "kaynak of truth" olarak kullanılır.

Delta-nötr Bybit öğreticisi, `reconciliation(true)` bayrağının açılmasını gerektirir; böylece strateji başlamadan önce hesap durumu hidrasyona tabi tutulur.

## Lookback penceresi

BitMEX grid piyasa yapıcı öğreticisi kalıcılık için `.with_reconciliation(true)` ile birlikte `.with_reconciliation_lookback_mins(2880)` (48 saat) kullanır.

## ExecutionEngine ile ilişki

[[execution_engine]]'in sorumlulukları arasında "dış venue state'i ile reconciliation" açıkça sıralanır. Reconciliation sırasında okunan state, [[cache]] üzerinden diğer bileşenlere sunulur.

## Bilinen boşluklar
- Adapter-spesifik reconciliation window konfigürasyonu (Bybit vs Binance farklı mı?) test edilmedi.
- Partial fill recovery davranışı belgelenmedi.

Bu stub şunları henüz kapsamıyor:

- v2 sürümünde reconciliation davranışındaki çözülmemiş değişiklikler (log.md'de flaglenmiştir).
- Kısmi fill ve orphan emir gibi uç durumların nasıl uzlaştırıldığı.
- `lookback_mins` için venue-özgü tavsiye edilen değerler ve alt/üst sınırlar.
- Reconciliation başarısızlıklarında hata/geri çekilme (backoff) politikası.
- Adaptör tarafında Rust API sözleşmesi (`with_reconciliation*` yapılandırıcılarının tam imzası).
- Backtest vs. canlı parite açısından reconciliation'ın etkisi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[event_sourcing]]
- [[events]]
- [[live_node]]
- [[positions]]
- [[reports]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
