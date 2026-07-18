---
title: ExecutionEngine
type: entity
sources:
  - sources/02_architecture_docs.md
  - sources/03_strategies_docs.md
last_updated: 2026-07-13
summary: Emir yaşam döngüsünü order_emulator, execution_algorithm ve risk_engine üzerinden adaptöre yönlendirip fill ve reconciliation süreçlerini yürüten motor.
key_concepts:
  - risk_engine
  - adapters
  - order_flow_pipeline
  - strategy_and_actor
  - event_driven_architecture
---

# ExecutionEngine

Emirlerin tam yaşam döngüsünü yönetir.

## Sorumluluklar
- Komutları uygun adaptöre yönlendirme
- Order ve position state takibi
- Risk yönetimi ile koordinasyon
- Fill (kısmi/tam) işleme
- Dış venue state'i ile reconciliation

## Emir Akışı
Strategy tarafından gönderilen emir konfigürasyona göre:
1. [[order_emulator|Order Emulator]] (emulation trigger varsa)
2. [[execution_algorithms|Execution Algorithm]] (`ExecAlgorithmId` verilmişse, örn. TWAP)
3. [[risk_engine|Risk Engine]] (varsayılan)
4. Venue [[adapters|adaptörü]]

## Desteklenen Emir Nitelikleri
IOC, FOK, GTC, GTD, DAY, AT_THE_OPEN, AT_THE_CLOSE (v1.x); contingency: OCO, OUO, OTO.

## İlgili Sayfalar
- [[orders]] — emir tipleri kataloğu ve yaşam döngüsü durumları
- [[positions]] — fill'lerin pozisyona dönüşümü (netting/hedging)
- [[reports]] — order fills / positions / account raporları

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[events]]
- [[execution_algorithms]]
- [[howto_get_started_lighter]]
- [[index_backtest_via_equity_proxy]]
- [[live_node]]
- [[nautilus_kernel]]
- [[order_book]]
- [[order_emulator]]
- [[order_flow_pipeline]]
- [[orders]]
- [[positions]]
- [[reports]]
- [[risk_engine]]
- [[rust_python_hybrid]]
- [[tutorial_backtest_fx_bars]]
- [[tutorial_backtest_low_level]]
- [[tutorial_delta_neutral_bybit]]
- [[tutorial_grid_market_maker_bitmex]]
- [[tutorial_grid_market_maker_dydx]]
- [[tutorial_lighter_rwa_composite_mm]]
- [[venue_reconciliation]]
<!-- BACKLINKS:END -->
