---
title: Hızlı Başlangıç Rehberi
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/getting_started/quickstart
last_updated: 2026-07-06
summary: EMA çaprazlaması örneğiyle BacktestEngine kurulumu, Strategy yaşam döngüsü ve register_indicator_for_bars ile gösterge-veri akışı bağlamayı beş dakikada tanıtır.
key_concepts:
  - strategy_and_actor
  - nautilus_kernel
  - event_driven_architecture
  - tutorial_backtest_low_level
  - tutorial_backtest_high_level
  - getting_started_roadmap
---

Bu eğitim, NautilusTrader ile uçtan uca bir geri test iş akışını beş dakikadan kısa sürede kurmayı gösterir. Örnek senaryo olarak EMA (üstel hareketli ortalama) çaprazlaması stratejisi kullanılır; böylece kullanıcı hem strateji yaşam döngüsünü hem de motorun temel yapı taşlarını somut biçimde deneyimler.

Odaklanılan API bileşenleri şunlardır: `Strategy` temel sınıfı ve yaşam döngüsü kancaları (`on_start`, `on_bar`, `on_stop`), yapılandırma için `StrategyConfig`, sinyal üretimi için `ExponentialMovingAverage` göstergesi ve simülasyonu yürüten `BacktestEngine` ile eşleniği `BacktestEngineConfig`. Enstrüman ve borsa (venue) tanımları doğrudan motora eklenir; ham DataFrame verilerinin `Bar` nesnelerine dönüştürülmesi ise **v1** için `BarDataWrangler.process(df)` çağrısıyla, **v2 (2.0.0rc1+)** için `Bar(...)` doğrudan yapıcısıyla yapılır ([[data_wranglers]] ve [[v1_to_v2_migration_lessons]]).

Eğitimin öne çıkardığı önemli örüntü, göstergelerin motorun veri akışına otomatik bağlanmasıdır:

```python
self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)

if self.portfolio.is_flat(instrument_id):
    order = self.order_factory.market(instrument_id, OrderSide.BUY, quantity)
    self.submit_order(order)
```

Burada `register_indicator_for_bars` çağrısı sayesinde her yeni bar geldiğinde gösterge kendini günceller; stratejinin `on_bar` içinde manuel besleme yapmasına gerek kalmaz. Emir gönderimi ise `order_factory` üzerinden yapılır ve portföy düz mü sorgusu ile pozisyon durumu şeffafça denetlenir.

Tasarım açısından bu eğitim, erişilebilirliği karmaşıklığa tercih eder: dış veri sağlayıcı yerine sentetik veya bundled test verisi kullanır, tek dosyalık yapılandırma ile motor kurulumunu minimize eder ve kullanıcıyı otomatik pozisyon yönetimi yerine açık durum kontrolüne yönlendirir. Bu şeffaf yaklaşım öğrenmeyi kolaylaştırır ancak üretim ortamında `BacktestNode` tabanlı yüksek seviyeli API'ye geçilmesi önerilir; çünkü yüksek seviyeli akış, canlı işlem düğümüne (`TradingNode`) doğrudan taşınabilirlik sağlar.

Bu eğitim, Nautilus'ün olay güdümlü mimarisiyle strateji yaşam döngüsünün nasıl kesiştiğini kavramak isteyen yeni başlayanlar için başlangıç noktasıdır. Sonrasında düşük ve yüksek seviyeli geri test API'lerine derinlemesine bakmak, kalıcı veri kataloğu (Parquet) kullanımını öğrenmek ve gerçek borsa adaptörleriyle çalışmak doğal ilerleme yolunu oluşturur.

**İlgili sayfalar:**
- [[getting_started_roadmap]]
- [[nautilus_kernel]]
- [[strategy_and_actor]]
- [[tutorial_backtest_low_level]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[getting_started_roadmap]]
<!-- BACKLINKS:END -->
