---
title: AX Borsası Üzerinde FX Ortalamaya Dönüş Backtest'i
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/fx_mean_reversion_ax
last_updated: 2026-07-06
summary: Spot tick verisini perpetual proxy'si yapıp INTERNAL bar toplamayla Bollinger mean-reversion backtesti; wrangler + venue modelleme örüntüsü.
key_concepts:
  - data_engine
  - strategy_and_actor
  - backtesting_guide
  - tutorial_gold_book_imbalance_ax
  - index_backtest_via_equity_proxy
  - order_flow_pipeline
---

Bu eğitim, gerçek FX perpetual verisinin doğrudan mevcut olmadığı bir venue için **spot tick verisini proxy olarak kullanarak** Bollinger Bantları tabanlı ortalamaya dönüş (mean reversion) stratejisinin nasıl backtest edileceğini gösterir. Odak noktası, stratejinin karlılığı değil; alternatif veri kaynaklarıyla venue modellemesinin nasıl yapılacağını öğretmektir.

Kullanılan başlıca API'ler ve konfigürasyon sınıfları şunlardır: `BacktestEngine` ile `BacktestEngineConfig`, kimlik/venue tanımları için `TraderId`, `Venue`, `AccountType.MARGIN` ve `OmsType.NETTING`; sermaye için `Money` ve `starting_balances`. Enstrüman tarafında `PerpetualContract` manuel olarak tanımlanır çünkü proxy veri şeması standart bir venue kataloğundan gelmez. Veri hazırlığında `QuoteTickDataWrangler` sınıfı CSV kotalarını Nautilus'un dahili `QuoteTick` nesnelerine çevirir. Strateji tarafında `BBMeanReversion` sınıfı ve onun `BBMeanReversionConfig` yapısı üzerinden bant genişliği ve sinyal eşikleri parametrize edilir.

Eğitimdeki en dikkat çekici (ve dokümantasyonda dağınık olan) örüntü, motor içi bar üretimidir:

```python
bar_type = BarType.from_str("EURUSD-PERP.AX-1-MINUTE-MID-INTERNAL")
# INTERNAL soneki: engine tick akışından barları kendisi kurar
# MID: bid/ask ortasından hesaplanır, spread maruziyetini simüle eder
```

`INTERNAL` soneki sayesinde önceden hazırlanmış bar verisi istenmez; `DataEngine` gelen `QuoteTick` akışından 1 dakikalık MID barlarını dahili olarak toplar. Bu yaklaşım, hem katalog boyutunu küçük tutar hem de tick düzeyi fill simülasyonunu korur.

Eğitimin bilinçli tercihleri açısından öne çıkan iki nokta vardır. Birincisi, `BBMeanReversion` stratejisi "kâsıtlı olarak basit ve edge içermez" biçiminde tanımlanır; amaç sinyal mekaniğinin şeffaf gösterimidir. Sonuç olarak backtest yaklaşık 1.287 USD zarar yazar — bu zarar, spread maliyetleri ve trend rejimlerinde ters yönlü hareketlerin ortalamaya dönüş mantığını cezalandırdığını göstermek için pedagojik olarak korunur. İkincisi, proxy veri kullanımı gerçek AX venue mikroyapısını (özellikle funding, likidite derinliği) simüle etmez; bu, hızlı prototipleme uğruna feda edilen bir doğruluk katmanıdır.

**İlgili sayfalar:**
- [[tutorial_gold_book_imbalance_ax]]
- [[backtesting_guide]]
- [[strategy_and_actor]]
- [[data_engine]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_gold_book_imbalance_ax]]
<!-- BACKLINKS:END -->
