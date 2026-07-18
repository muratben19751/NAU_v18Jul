---
title: Environment Contexts (Backtest / Sandbox / Live)
type: concept
sources:
  - sources/02_architecture_docs.md
  - sources/04_backtesting_docs.md
last_updated: 2026-07-05
summary: Backtest/Sandbox/Live üçlüsünde aynı kernel ve strateji kodunu koruyarak research-to-live parity sağlar; yalnızca venue simülatörü değişir.
key_concepts:
  - nautilus_kernel
  - backtesting_guide
  - event_driven_architecture
  - execution_engine
  - single_threaded_core
---

# Environment Contexts

Aynı kernel üç farklı ortamda çalışır; strateji kodu değişmez.

| Context | Veri | Venue |
|---|---|---|
| **Backtest** | Tarihsel | Simüle |
| **Sandbox** | Gerçek zaman | Simüle |
| **Live** | Gerçek zaman | Gerçek (paper veya canlı hesap) |

## Neden Önemli
"Research-to-live parity" — araştırma ve canlıda birebir aynı motor ve emir akışı; venue simülatörü değişir yalnızca. Bu, klasik "backtest'te çalışıyor, canlıda çalışmıyor" hatasını azaltır. Aynı motorun her contexte yeniden kullanılabilir olmasının temel sebebi Rust çekirdeğin dilden-bağımsız uçtan uca deterministik dispatch sunması — bkz. [[rust_python_hybrid]].

## Bar Verisiyle Backtest
OHLC sırayla işlenir. Take-profit ve stop-loss aynı bar içindeyse adaptive high/low sıralama daha gerçekçi fill üretir. Bkz. [[backtesting_guide]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
- [[getting_started_roadmap]]
- [[live_node]]
- [[rust_python_hybrid]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
