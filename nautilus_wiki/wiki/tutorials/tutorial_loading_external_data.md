---
title: Harici Veri Kaynaklarının Yüklenmesi
type: tutorial
sources:
  - https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/how_to/loading_external_data.py
last_updated: 2026-07-06
summary: Adaptörsüz CSV kaynaklarını QuoteTickDataWrangler ile dönüştürüp ParquetDataCatalog'a yazarak backtest'lerde yeniden kullanılabilir veri gölü kurar.
key_concepts:
  - data_engine
  - backtesting_guide
  - tutorial_backtest_high_level
  - getting_started_roadmap
  - adapters
---

Bu eğitim, yerel bir Nautilus adaptörü bulunmayan tarihsel veri sağlayıcılarından (örneğin histdata.com) alınan CSV dosyalarını geri test iş akışına entegre etmenin standart yolunu gösterir. Amaç, ham dosyaları tekrarlanabilir biçimde Nautilus veri modeline dönüştürmek ve bir Parquet kataloğuna kalıcı olarak yazarak sonraki koşumlarda yeniden işlemeyi ortadan kaldırmaktır.

Kullanılan bileşenler iki kategoriye ayrılır. Veri işleme tarafında: ham CSV'yi pandas DataFrame'e okuyan `CSVTickDataLoader`; DataFrame'i `QuoteTick` nesnelerine dönüştüren `QuoteTickDataWrangler`; test amaçlı enstrüman tanımlarını üreten `TestInstrumentProvider`. Depolama tarafında: enstrümanları ve tick'leri verimli sorgulanabilir Parquet formatında saklayan `ParquetDataCatalog`. Geri test yapılandırması ise tanıdık hiyerarşiyi izler: `BacktestNode`, `BacktestRunConfig`, `BacktestEngineConfig`, `BacktestVenueConfig`, `BacktestDataConfig` ve harici stratejileri referans veren `ImportableStrategyConfig`.

Wrangler örüntüsü, enstrüman nesnesi ile başlatılıp DataFrame'i işleme sokar; sonrasında katalog nanosaniye hassasiyetli zaman damgalarıyla sorgulanır:

```python
wrangler = QuoteTickDataWrangler(EURUSD)
ticks = wrangler.process(df)
catalog.write_data(ticks)

start = dt_to_unix_nanos(pd.Timestamp("2020-01-03", tz="UTC"))
end = dt_to_unix_nanos(pd.Timestamp("2020-01-04", tz="UTC"))
ticks = catalog.quote_ticks(instrument_ids=[EURUSD.id], start=start, end=end)
```

Zaman damgalarının nanosaniye tamsayısına dönüştürülmesi Nautilus çekirdeğinin iç temsiliyle uyumludur; `dt_to_unix_nanos` yardımcı fonksiyonu bu dönüşümü tek satırda yapar.

Temel tasarım kararı, sorumlulukların ayrılmasıdır: veri dönüştürme (wrangling) aşaması katalog yazımından bağımsız olarak bir kez yapılır. Böylece aynı işlenmiş tick verisi birden çok geri testte, farklı stratejiler ve parametre kombinasyonlarıyla yeniden kullanılır; her koşuda ham CSV'yi tekrar ayrıştırma maliyeti ortadan kalkar. Bu yaklaşım, ekibin standart bir veri gölü kurmasını ve yeni araştırma iterasyonlarını hızlandırmasını sağlar. Ayrıca aynı `ParquetDataCatalog` API'si, canlı işlem düğümüyle paylaşıldığı için, geri testte kullanılan enstrüman kayıtları ve veri şemaları üretime doğrudan taşınabilir.

**İlgili sayfalar:**
- [[tutorial_backtest_high_level]]
- [[data_engine]]
- [[backtesting_guide]]
- [[getting_started_roadmap]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_backtest_high_level]]
- [[v1_to_v2_migration_lessons]]
<!-- BACKLINKS:END -->
