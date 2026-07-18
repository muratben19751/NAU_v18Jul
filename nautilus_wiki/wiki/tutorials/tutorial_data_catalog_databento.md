---
title: Databento Veri Kataloğu Eğitimi (Ulaşılamadı)
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/databento_data_catalog
last_updated: 2026-07-06
summary: Databento eğitimi 404 nedeniyle alınamadı; ParquetDataCatalog, DatabentoDataLoader ve catalog.write_data akışlarını kapsayan placeholder olarak bekletiliyor.
key_concepts:
  - adapters
  - data_engine
  - backtesting_guide
  - tutorial_gold_book_imbalance_ax
  - tutorial_loading_external_data
---

**Not:** Bu eğitim sayfası 2026-07-06 tarihinde `https://nautilustrader.io/docs/latest/tutorials/databento_data_catalog` adresinden alınmaya çalışıldı ancak URL 404 döndürdü. Alternatif varyasyonlar denendi (`databento-data-catalog`, `data_catalog_databento`, `backtest_databento_data_catalog`, `how_to/data_catalog_databento`) fakat hepsi başarısız oldu.

Resmi eğitim listesi incelendiğinde ilgili içeriğin muhtemelen `docs/how_to/data_catalog_databento.py` altında bir Jupyter/Python dosyası olarak barındırıldığı görüldü; ancak bu dosya web sürümü olarak yayınlanmıyor. Bu placeholder dosyası, URL'nin gelecekte yeniden yayınlanması durumunda düzenlenmek üzere korunmuştur.

**Beklenen içerik (tahmini):** `ParquetDataCatalog` veya `DataCatalog` sınıfının kullanımı, `DatabentoDataLoader` ile `.dbn.zst` arşivlerinin kataloğa yazılması, `catalog.write_data()` ve `catalog.query()` API'leri, `BacktestEngine` üzerinde katalog verisinin geri okunması. Bu konuların hem `[[tutorial_gold_book_imbalance_ax]]` (Databento yükleyici doğrudan kullanılıyor) hem de `[[backtesting_guide]]` sayfaları ile örtüştüğü tahmin edilmektedir.

**Öneri:** Konu erişilebilir olduğunda `ParquetDataCatalog` merkezli ayrı bir sentez sayfası açılmalıdır; katalog kalıcılığı, şema evrimi ve `bar_types` sorgulamaları Nautilus'ta yeterince belgelenmemiş bir alan olarak değerlendirilmiştir.

**İlgili sayfalar:**
- [[tutorial_gold_book_imbalance_ax]]
- [[backtesting_guide]]
- [[data_engine]]
- [[adapters]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_gold_book_imbalance_ax]]
<!-- BACKLINKS:END -->
