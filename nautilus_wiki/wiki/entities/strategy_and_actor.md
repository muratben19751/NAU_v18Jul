---
title: Strategy & Actor
type: entity
sources:
  - sources/03_strategies_docs.md
last_updated: 2026-07-10
summary: Actor'ün veri/event/state temeli üzerine Strategy'nin order management eklediği hiyerarşiyi ve Clock, Cache, Portfolio servis erişimini özetler.
key_concepts:
  - cache
  - event_driven_architecture
  - order_flow_pipeline
  - message_bus
---

# Strategy ve Actor

## Actor
Temel bileşen. Veri alır, event handle eder, state yönetir. Trading'e özgü işlev içermez.

## Strategy
Actor'ü extend eder. Order management yeteneği ekler.

Erişilebilir servisler:
- **Clock** — timestamp ve timer
- **[[cache|Cache]]** — piyasa verisi ve execution nesneleri
- **[[portfolio|Portfolio]]** — hesap ve P&L bilgisi

## Desteklenen Strateji Patternleri
- Directional
- Momentum
- Rebalancing
- Pairs
- Market making

## StrategyConfig ve ImportableStrategyConfig Uyumu

`ImportableStrategyConfig`, `config` alanını **dict olarak** (tüm değerler `str`) iletir. Bu nedenle `StrategyConfig` (veya custom alt sınıf) `__init__` metodunda `str → BarType / InstrumentId` dönüşümü yapılmazsa runtime hatası alınır.

**Zorunlu pattern:**

```python
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import BarType
from nautilus_trader.config import StrategyConfig

class MyConfig(StrategyConfig):
    def __init__(self, instrument_id, bar_type, **kwargs):
        super().__init__(**kwargs)
        self.instrument_id = (
            InstrumentId.from_str(instrument_id)
            if isinstance(instrument_id, str)
            else instrument_id
        )
        self.bar_type = (
            BarType.from_str(bar_type)
            if isinstance(bar_type, str)
            else bar_type
        )
```

Bu pattern sayesinde Config hem doğrudan Python kodu içinde (`BarType` objesiyle) hem de `ImportableStrategyConfig` aracılığıyla (`str` ile) kullanılabilir — aynı sınıf her iki yolda da çalışır.

**Önemli:** `BacktestNode` ile `ImportableStrategyConfig` kullanırken bu dönüşüm olmadan `TypeError: argument 'instrument_id': 'str' object cannot be interpreted as InstrumentId` alınır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtest_node]]
- [[backtesting_guide]]
- [[custom_data]]
- [[events]]
- [[getting_started_roadmap]]
- [[live_node]]
- [[option_greeks_pipeline]]
- [[portfolio]]
- [[rust_python_hybrid]]
- [[tutorial_book_imbalance_betfair]]
- [[tutorial_delta_neutral_bybit]]
- [[tutorial_delta_neutral_derive]]
- [[tutorial_fx_mean_reversion_ax]]
- [[tutorial_hurst_vpin_kraken]]
- [[tutorial_quickstart]]
- [[v1_to_v2_migration_lessons]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
