---
title: Execution Algorithms (TWAP)
type: entity
status: draft
sources:
  - sources/03_strategies_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/execution
last_updated: 2026-07-09
summary: ExecAlgorithmId ile devreye giren TWAP tipi bileşenler; TWAP iki zorunlu parametre ister (horizon_secs, interval_secs); v1.229.0'da Python binding eklendi.
related:
  - wiki/concepts/order_flow_pipeline.md
  - wiki/tutorials/tutorial_backtest_low_level.md
  - wiki/entities/execution_engine.md
---

# Execution Algorithms (TWAP)

Strateji tarafından gönderilen bir emirde `ExecAlgorithmId` belirtildiğinde devreye giren, emri venue'ya iletmeden önce belirli bir yürütme mantığına göre parçalayan/dönüştüren bileşen. TWAP (Time-Weighted Average Price) bu ailenin referans örneğidir.

## Pipeline'daki Konumu

Emir akışında yürütme algoritması, Order Emulator'dan sonra ve [[risk_engine]] öncesinde çalışır; yani `Strategy → Order Emulator → Execution Algorithm → Risk Engine → Adapter → Venue` sırasında yer alır. Yalnızca emirde bir `ExecAlgorithmId` verilmişse etkinleşir; verilmezse bu adım atlanır. Bkz. [[order_flow_pipeline]].

## ExecutionEngine ile İlişkisi

[[execution_engine]] emirlerin tam yaşam döngüsünü yönetirken, yürütme algoritması bu yaşam döngüsünün "venue'ya gitmeden önce" aşamasına takılan konfigüre edilebilir bir katmandır. Emir modifikasyonu ve emulation her aşamada geçerlidir; Risk Engine bypass edilemez, dolayısıyla algoritmanın ürettiği alt emirler de risk kontrolünden geçer.


## TWAP Parametreleri (doğrulanmış — v2.0.0rc1 kaynak kodu)

TWAP algoritması, parent emirde `exec_algorithm_params` olarak iki **zorunlu** parametre alır:

```python
exec_algorithm_params={"horizon_secs": 60, "interval_secs": 10}
```

`horizon_secs`: toplam yürütme süresi; `interval_secs`: alt emirler arası süre.

## v1.229.0 Yeni: Python v2 Backtest Binding'leri

> "Added `add_native_exec_algorithm` and `ExecutionAlgorithmConfig` bindings to the Python v2 backtest engine."

## Backtest'te Kayıt

Düşük seviyeli API'de yürütme algoritması motora stratejiden ayrı olarak eklenir:

```python
engine.add_strategy(strategy=EMACrossTWAP(config=strat_config))
engine.add_exec_algorithm(TWAPExecAlgorithm())
```

Bu ayrım, aynı sinyal mantığının farklı yürütme profilleriyle test edilmesine ya da aynı yürütme algoritmasının birden fazla stratejide yeniden kullanılmasına olanak tanır. Referans strateji `EMACrossTWAP`, `TWAPExecAlgorithm` ile birlikte kullanılır.

## Bilinen boşluklar

Bu stub henüz şunları kapsamıyor:
- `TWAPExecAlgorithm` konfigürasyon parametreleri (aralık, horizon, spawn boyutu vb.)
- TWAP dışındaki yürütme algoritmalarının (varsa) envanteri
- Özel yürütme algoritması yazma API'si ve gerekli base class
- `ExecAlgorithmId` üretimi ve emirlere iliştirme mekaniği
- Live vs backtest yürütme algoritma davranışındaki farklar
- Spawn edilen alt emirlerin fill/state raporlamasının parent emre nasıl bağlandığı

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[execution_engine]]
- [[order_flow_pipeline]]
<!-- BACKLINKS:END -->
