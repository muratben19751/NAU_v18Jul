---
title: FX USDJPY Bar Verisiyle Geri Test
type: tutorial
sources:
  - https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/backtest_fx_bars.py
last_updated: 2026-07-06
summary: USDJPY üzerinde bid/ask bar'lardan QuoteTickDataWrangler ile sentetik tick üretip FXRolloverInterestModule, FillModel ve MARGIN/HEDGING hesabıyla gerçekçi FX geri testi kurar.
key_concepts:
  - backtesting_guide
  - tutorial_backtest_low_level
  - execution_engine
  - order_flow_pipeline
  - environment_contexts
---

Bu eğitim, USD/JPY paritesi üzerinde 28 günlük bir zaman aralığında EMA çaprazlaması stratejisiyle gerçekçi bir FX geri testini kurmayı gösterir. Amaç sadece stratejiyi çalıştırmak değil; margin hesabı, çoklu para birimi bakiyesi, günlük carry ücretleri ve fill modeli gibi FX'e özgü mekanikleri motorda doğru şekilde modellemektir.

Kullanılan temel bileşenler: simülasyon çekirdeği olarak `BacktestEngine` ve `BacktestEngineConfig`; günlük faiz/carry ücretlerini işlemek için `FXRolloverInterestModule` ve `FXRolloverInterestConfig`; slippage ve kısmi doldurma olasılığını modelleyen `FillModel`; bid/ask bar verisinden sentetik quote tick üretmek için `QuoteTickDataWrangler`. Yapılandırma tarafında `RiskEngineConfig(bypass=True)` ile pre-trade risk kontrolleri devre dışı bırakılır, `OmsType.HEDGING` eşzamanlı long/short pozisyonlara izin verir ve `AccountType.MARGIN` çoklu para birimli başlangıç bakiyesini destekler.

Bar verisinden quote tick sentezi eğitimin en özgün yönüdür; iki ayrı bid ve ask CSV'si birleştirilerek gerçekçi bir tick akışı üretilir:

```python
ticks = wrangler.process_bar_data(
    bid_data=provider.read_csv_bars("fxcm/usdjpy-m1-bid-2013.csv"),
    ask_data=provider.read_csv_bars("fxcm/usdjpy-m1-ask-2013.csv"),
)

engine.trader.generate_account_report(SIM)
engine.trader.generate_order_fills_report()
```

`trader` üzerinden çağrılan rapor üretim yöntemleri hesap durumunu, emir dolumlarını ve pozisyon geçmişini incelemek için standart çıktılardır.

Tasarım açısından eğitim, öğretici netliği kârlılığa tercih eder. EMACross stratejisi kasıtlı olarak kenarsızdır ve gürültülü 5 dakikalık barlarda "whipsaw" zararları üretir (raporlanan sonuç yaklaşık 209.000 JPY kayıp). Bu, gerçekçi bir geri testin — carry maliyetleri, fill modeli ve rollover ücretleri dahil — kâğıt üzerinde iyi görünen stratejilerin zayıflıklarını nasıl ortaya çıkardığını gösterir. Sonraki adım olarak rejim filtreleri, daha yavaş sinyaller veya volatiliteye duyarlı boyutlandırma önerilir. FX'e özgü mekanikleri modellemenin, aynı stratejiyi hisse veya kripto gibi diğer varlık sınıflarında test etmekten temelde farklı olduğu ders çıkarılır.

**İlgili sayfalar:**
- [[tutorial_backtest_low_level]]
- [[backtesting_guide]]
- [[execution_engine]]
- [[order_flow_pipeline]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
<!-- BACKLINKS:END -->
