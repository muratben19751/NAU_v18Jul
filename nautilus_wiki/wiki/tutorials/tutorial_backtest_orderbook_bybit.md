---
title: Bybit Emir Defteri Derinliğiyle Geri Test
type: tutorial
sources:
  - https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/backtest_orderbook_bybit.py
last_updated: 2026-07-06
summary: Bybit L2 delta ZIP arşivlerini BybitOrderBookDeltaDataLoader ile yükleyip OrderBookImbalance stratejisini BacktestNode üzerinden çalıştırmayı gösterir.
key_concepts:
  - adapters
  - data_engine
  - backtesting_guide
  - tutorial_backtest_orderbook_binance
  - order_flow_pipeline
  - strategy_and_actor
---

Bu eğitim, Bybit borsasından alınan L2 emir defteri delta verisini geri test motorunda yeniden oynatarak `OrderBookImbalance` stratejisini çalıştırmayı gösterir. Binance karşılığına benzer bir yapıya sahip olsa da veri yükleyicisi ve dosya formatı Bybit'e özeldir; günlük ZIP arşivlerinin doğrudan DataFrame'e okunabilmesi bu adaptörün belirleyici özelliğidir.

Öne çıkan API'ler: günlük Bybit ZIP dosyalarını okuyan `BybitOrderBookDeltaDataLoader`; delta akışını enstrüman kimliğiyle etiketleyen `OrderBookDeltaDataWrangler`; kalıcı veri deposu olarak `ParquetDataCatalog`. Motor orkestrasyonu `BacktestNode` üzerinden yapılır; yapılandırma için `BacktestEngineConfig`, `BacktestVenueConfig` ve `BacktestDataConfig` kullanılır. Strateji, `ImportableStrategyConfig` aracılığıyla satır içi yapılandırma ile enjekte edilir:

```python
ImportableStrategyConfig(
    strategy_path="nautilus_trader.examples.strategies.orderbook_imbalance:OrderBookImbalance",
    config={
        "instrument_id": instrument.id,
        "book_type": book_type,
        "max_trade_size": Decimal("1.000"),
    },
)

engine: BacktestEngine = node.get_engine(config.id)
engine.trader.generate_order_fills_report()
engine.trader.generate_positions_report()
```

`max_trade_size` gibi parametrelerin doğrudan sözlük olarak geçilebilmesi, strateji sınıfını değiştirmeden farklı senaryoları programatik olarak taramayı kolaylaştırır. Sonuç raporları — doldurma raporu ve pozisyon raporu — motor örneğine `get_engine` ile erişildikten sonra üretilir.

Tasarım ödünleşimleri Binance eğitimindekine paraleldir ancak Bybit'e özgü nüanslar barındırır. Eğitim, kavramsal netlik için veri kümesini `nrows = 1_000_000` ile kısıtlar; tam günlük dosyaları işlemek uzun sürer ve makine kaynaklarını zorlar. `OrderBookImbalance`'ın açıkça bir öğretici strateji olduğu ve gerçek bir kenar taşımadığı vurgulanır; mikroyapı sinyallerini üretim düzeyinde kullanmak için ek filtreler, ücret modelleri ve gecikme simülasyonları gerekir. İki eğitimin (Binance/Bybit) yan yana okunması, Nautilus'ün adaptör soyutlamasının farklı veri kaynaklarını nasıl homojenleştirdiğini — aynı wrangler ve motor arayüzü, farklı yükleyici — somut olarak sergiler.

**İlgili sayfalar:**
- [[tutorial_backtest_orderbook_binance]]
- [[adapters]]
- [[data_engine]]
- [[backtesting_guide]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_backtest_orderbook_binance]]
<!-- BACKLINKS:END -->
