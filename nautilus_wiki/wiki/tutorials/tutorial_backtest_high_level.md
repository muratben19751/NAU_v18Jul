---
title: Yüksek Seviyeli Geri Test API'si
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/getting_started/backtest_high_level
last_updated: 2026-07-06
summary: BacktestNode, ParquetDataCatalog ve ImportableStrategyConfig ile yapılandırma güdümlü, canlı işleme taşınabilir bir geri test hattı kurmayı gösterir.
key_concepts:
  - backtesting_guide
  - tutorial_backtest_low_level
  - tutorial_loading_external_data
  - nautilus_kernel
  - strategy_and_actor
  - environment_contexts
---

Bu eğitim, yapılandırma güdümlü ve üretime yakın bir geri test iş akışını `BacktestNode` etrafında kurmayı öğretir. Ham tarihsel veriden kalıcı bir katalog deposuna, oradan simülasyon çalıştırmaya kadar tüm hat, veri düğümünü canlı işlem düğümüne (`TradingNode`) doğrudan taşıyabilecek biçimde tasarlanır.

Kullanılan temel bileşenler: iş akışını yürüten `BacktestNode`; kalıcı veri deposu olarak `ParquetDataCatalog`; ham CSV'yi `QuoteTick` nesnelerine dönüştüren `QuoteTickDataWrangler`; ve yapılandırma katmanları — `BacktestRunConfig`, `BacktestEngineConfig`, `BacktestVenueConfig`, `BacktestDataConfig`. Strateji, sınıf yolu ve parametreleriyle birlikte `ImportableStrategyConfig` içinde bildirilir; bu, strateji nesnesinin çalışma zamanında dinamik olarak yüklenmesine izin verir.

Tipik veri hattı şu adımlardan oluşur: CSV yükle → DataFrame'e dönüştür → wrangler ile `QuoteTick`'lere çevir → `ParquetDataCatalog`'a yaz. Geri test öncesinde katalog sorgulanarak veri aralığı doğrulanır:

```python
catalog.write_data(ticks)
start = dt_to_unix_nanos(pd.Timestamp("2020-01-03", tz="UTC"))
ticks = catalog.quote_ticks(instrument_ids=[EURUSD.id], start=start, end=end)

run_config = BacktestRunConfig(
    engine=engine_config, data=[data_config], venues=[venue_config],
)
```

Eğitim, `BacktestRunConfig`'in "kısmi (partialable)" olduğunu, yani kademeli olarak inşa edilebileceğini vurgular; büyük konfigürasyonları parametre değişimlerine göre klonlamak kolaylaşır. Ayrıca aynı yapılandırma nesnesinin çoklu çalışmalarda (parameter sweep) yeniden kullanılabilmesi, tekrarlanabilirlik için önemli bir tasarım kararıdır.

Temel tasarım ödünleşimi şudur: yüksek seviyeli API, doğrudan bileşen erişimini feragat ederek yeniden üretilebilirlik ve taşınabilirlik sağlar. Burada yazılan stratejiler, aktörler ve yürütme algoritmaları, refaktör gerektirmeden canlı işleme geçirilebilir. Bu, ekip ölçeğinde çalışan ve geri testten üretime tekrarlanabilir bir hat isteyen projeler için önerilen yoldur. Deneysel keşif veya bileşen düzeyinde hata ayıklama gerekiyorsa düşük seviyeli API daha uygun kalır; ancak nihayetinde her iki API de aynı çekirdek motoru paylaştığı için geçiş maliyeti düşüktür.

**İlgili sayfalar:**
- [[backtesting_guide]]
- [[tutorial_backtest_low_level]]
- [[nautilus_kernel]]
- [[tutorial_loading_external_data]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_backtest_low_level]]
- [[tutorial_loading_external_data]]
<!-- BACKLINKS:END -->
