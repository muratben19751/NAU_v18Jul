---
title: Düşük Seviyeli Geri Test API'si
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/getting_started/backtest_low_level
last_updated: 2026-07-06
summary: BacktestEngine ile veri hattı, borsa ve strateji-yürütme algoritması ayrımını elle kurup TWAP tabanlı EMACross örneğiyle bileşen düzeyi denetim gösterir.
key_concepts:
  - backtesting_guide
  - tutorial_backtest_high_level
  - execution_engine
  - order_flow_pipeline
  - strategy_and_actor
  - environment_contexts
---

Bu eğitim, NautilusTrader'ın `BacktestEngine` sınıfını doğrudan kullanarak bileşen düzeyinde denetime sahip bir geri test kurulumunu adım adım gösterir. Amaç, tarihsel verinin yüklenmesinden borsa yapılandırmasına, stratejinin ve yürütme algoritmalarının entegrasyonundan sonuç analizine kadar tüm süreci elle yönetmenin nasıl göründüğünü ortaya koymaktır.

Kullanılan temel API'ler: motor kurulumu için `BacktestEngine` ve `BacktestEngineConfig`; ham CSV verisini `TradeTick` nesnelerine dönüştürmek için `TradeTickDataWrangler`; birlikte gelen örnek verileri ve enstrüman tanımlarını sağlayan `TestDataProvider` ile `TestInstrumentProvider`; referans strateji olarak `EMACrossTWAP` ve buna eşlik eden `TWAPExecAlgorithm` yürütme algoritması. Alan modelleri tarafında `Venue`, `Money`, `BarType` ve `TraderId` gibi sınıflar borsa, hesap bakiyesi, bar türü ve tacir kimliği tanımları için kullanılır.

Eğitimin altını çizdiği önemli örüntü, strateji ile yürütme algoritmasının açıkça ayrılmasıdır. Strateji sinyal üretir; yürütme algoritması ise büyük emirleri TWAP gibi bir mantıkla parçalara ayırarak gönderir:

```python
engine.add_strategy(strategy=EMACrossTWAP(config=strat_config))
engine.add_exec_algorithm(TWAPExecAlgorithm())
engine.add_venue(venue=SIM, oms_type=OmsType.HEDGING, ...)
engine.add_data(ticks)
engine.run()
```

Bu ayrım sayesinde aynı sinyal mantığı farklı yürütme profilleriyle test edilebilir; ya da tersine aynı yürütme algoritması birden fazla stratejide yeniden kullanılabilir. Motor, birden çok veri türünü (özel türler dahil) ve birden fazla borsayı aynı çalışmada barındırabilir.

Tasarım açısından düşük seviyeli API, kolaylık yerine denetim ve esnekliği önceliklendirir. Kullanıcı, veri hattını, borsa parametrelerini ve hesap kurulumunu tek tek yapılandırmak zorundadır; buna karşılık deterministik yeniden oynatma, özel yürütme semantiği ve çoklu-borsa kombinasyonları gibi sofistike senaryolar mümkündür. Üretimde yeniden üretilebilirlik önemliyse `BacktestNode` tabanlı yüksek seviyeli API tercih edilmelidir; ancak öğrenme, hata ayıklama ve motor iç işleyişini keşfetmek için düşük seviyeli yol vazgeçilmezdir.

**İlgili sayfalar:**
- [[backtesting_guide]]
- [[tutorial_backtest_high_level]]
- [[execution_engine]]
- [[order_flow_pipeline]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_backtest_fx_bars]]
- [[tutorial_backtest_high_level]]
- [[tutorial_quickstart]]
<!-- BACKLINKS:END -->
