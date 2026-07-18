---
title: Order Flow Pipeline
type: concept
sources:
  - sources/03_strategies_docs.md
last_updated: 2026-07-05
summary: Strateji'den venue'ya emir yolunu Order Emulator, Execution Algorithm ve bypass edilemez Risk Engine aşamalarıyla sıralar.
key_concepts:
  - execution_engine
  - risk_engine
  - strategy_and_actor
  - adapters
  - event_driven_architecture
---

# Order Flow Pipeline

Strategy'den venue'ya emir yolu:

```
Strategy
   │
   ▼
[Order Emulator]        (emulation trigger tanımlıysa)
   │
   ▼
[Execution Algorithm]   (ExecAlgorithmId verilmişse, örn. TWAP)
   │
   ▼
Risk Engine             (her zaman devrede)
   │
   ▼
Adapter → Venue
```

## Notlar
- Batch submission destekli
- Emir modification ve emulation her aşamada geçerli
- Risk Engine bypass edilemez

İlk durak [[order_emulator|Order Emulator]]'dır — emulation trigger tanımlıysa emri sanal olarak izler, aksi halde emir doğrudan [[execution_algorithms|Execution Algorithm]] katmanına akar (`ExecAlgorithmId` varsa TWAP gibi bir yürütme algoritması çalışır). Her iki yol da [[risk_engine|Risk Engine]] üzerinden [[execution_engine|ExecutionEngine]]'e ve oradan [[adapters|adapter]] aracılığıyla venue'ya varır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[execution_algorithms]]
- [[index_backtest_via_equity_proxy]]
- [[order_emulator]]
- [[orders]]
- [[tutorial_backtest_fx_bars]]
- [[tutorial_backtest_low_level]]
- [[tutorial_backtest_orderbook_binance]]
- [[tutorial_book_imbalance_betfair]]
- [[tutorial_gold_book_imbalance_ax]]
- [[tutorial_grid_market_maker_dydx]]
- [[v1_to_v2_migration_lessons]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
