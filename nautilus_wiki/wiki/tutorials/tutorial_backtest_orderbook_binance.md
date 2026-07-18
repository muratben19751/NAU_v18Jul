---
title: Binance Emir Defteri Derinliğiyle Geri Test
type: tutorial
sources:
  - https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/backtest_orderbook_binance.py
last_updated: 2026-07-06
summary: Binance L2 delta CSV'lerini BinanceOrderBookDeltaDataLoader ve OrderBookDeltaDataWrangler ile yükleyip OrderBookImbalance stratejisini L2_MBP defterinde çalıştırır.
key_concepts:
  - order_flow_pipeline
  - data_engine
  - adapters
  - tutorial_backtest_orderbook_bybit
  - tutorial_backtest_low_level
  - strategy_and_actor
---

Bu eğitim, Binance'ten indirilen tarihsel L2 emir defteri delta verisini NautilusTrader'ın geri test motorunda yeniden oynatmayı ve bu veri üzerinde bir mikroyapı stratejisi çalıştırmayı gösterir. Örnek strateji, defterin bir tarafı diğerinden belirgin biçimde kalın olduğunda, kalın tarafa karşı fill-or-kill limit emirleri gönderen `OrderBookImbalance` uygulamasıdır.

Kullanılan temel API'ler: ham Binance CSV dosyalarını (snapshot ve delta güncellemeleri) delta nesnelerine dönüştüren `BinanceOrderBookDeltaDataLoader`; delta'lara enstrüman kimliği ekleyip zaman sırasına göre düzenleyen `OrderBookDeltaDataWrangler`; enstrüman ve delta verilerini zaman aralığına göre tembel yükleyen `ParquetDataCatalog`. Motor tarafında `BacktestNode`, `BacktestEngine` ve tanıdık yapılandırma hiyerarşisi (`BacktestRunConfig`, `BacktestEngineConfig`, `BacktestVenueConfig`, `BacktestDataConfig`) kullanılır. Strateji `ImportableStrategyConfig` aracılığıyla `nautilus_trader.examples.strategies.orderbook_imbalance:OrderBookImbalance` yolundan dinamik olarak yüklenir.

Wrangler örüntüsünde snapshot ve güncelleme dosyaları birleştirilir; sıralama, yayın sırasını koruyacak biçimde ts_init'e göre yapılır:

```python
deltas = wrangler.process(df_snap)
deltas += wrangler.process(df_update)
deltas.sort(key=lambda x: x.ts_init)

engine: BacktestEngine = node.get_engine(config.id)
engine.trader.generate_order_fills_report()
```

Sonuçlara erişmek için `node.get_engine(config.id)` çağrısı gerekir; `BacktestNode` bir dizi çalışmayı yönetirken tekil motor örneği bu yolla açığa çıkarılır.

Tasarım ödünleşimleri öğreticidir. Birincisi, strateji kasıtlı olarak sadedir ve gerçek bir kenar iddia etmez — amaç öğrenmedir. İkincisi, tam dosyalar 110M satırlık güncelleme içerdiği halde eğitim veriyi ~11 dakikaya karşılık gelen 1M satıra sınırlar; bu, geri test süresini makul tutar. Üçüncüsü, `L2_MBP` defter türü seçilir çünkü delta akışı zaten tam derinlik bilgisini taşır ve daha yüksek frekanslı snapshot yeniden inşasının hesaplama yükünden kaçınılır. Bu üç seçim, mikroyapı geri testinde tipik olarak karşılaşılan veri hacmi–doğruluk–hız dengelerini somutlaştırır.

**İlgili sayfalar:**
- [[tutorial_backtest_orderbook_bybit]]
- [[data_engine]]
- [[adapters]]
- [[order_flow_pipeline]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_backtest_orderbook_bybit]]
<!-- BACKLINKS:END -->
