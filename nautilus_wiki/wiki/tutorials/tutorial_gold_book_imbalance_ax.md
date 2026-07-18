---
title: AX Altın Perpetual'ında Emir Defteri Dengesizliği Stratejisi
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/gold_book_imbalance_ax
last_updated: 2026-07-06
summary: CME altın futures kotasyonlarını AX perpetual sembolüne yeniden eşleyerek L1 order book imbalance sinyalini FOK limit emirlerle backtestler.
key_concepts:
  - order_flow_pipeline
  - data_engine
  - tutorial_data_catalog_databento
  - tutorial_fx_mean_reversion_ax
  - adapters
  - backtesting_guide
---

Bu eğitim, emir defterinin bir tarafında biriken hacim dengesizliğine (order book imbalance) dayalı bir mikroyapı stratejisinin CME altın futures verisini proxy olarak kullanarak AX Exchange altın perpetual'ı üzerinde nasıl backtest edileceğini gösterir. Temel hipotez: defterin ince tarafındaki likidite baskısı, fiyatı o yöne çekmeye eğilimlidir; strateji, dengesizlik eşiği aşıldığında **fill-or-kill limit emirleri** ile pozisyon açar.

Kullanılan başlıca sınıflar: `DatabentoDataLoader` (`.dbn.zst` arşivlerini `QuoteTick` nesnelerine çevirir ve hedef sembole yeniden eşler), `PerpetualContract` (hassasiyet, komisyon ve teminat kurallarıyla altın perpetual tanımı), pre-built `OrderBookImbalance` stratejisi (L1 defter verisi tüketir) ve `BacktestEngine`. Defter simülasyonu için `BookType.L1_MBP` seçilir — bu, sadece en iyi bid/ask seviyesini modelleyen hafif bir kotasyon-güdümlü kipdir.

Eğitimin merkezindeki tasarım hilesi, bir venue'nun verisini başka bir venue sembolüne yeniden eşlemektir:

```python
loader = DatabentoDataLoader()
quotes = loader.from_dbn_file(
    path="gc_gold_quotes.dbn.zst",
    instrument_id=InstrumentId.from_str("XAU-PERP.AX"),
)
```

Bu ID yeniden yazma, CME futures kotasyonlarının motor içinde AX perpetual sembolü olarak tüketilmesini sağlar — böylece strateji ve venue konfigürasyonu üretim yolundaymış gibi aynı kalır.

Tasarım açısından belirgin ödünleşimler vardır. Birincisi, `mbp-1` (top-of-book) verisi tercih edilmiştir: bu, tam derinlik (`mbp-10` veya MBO) yerine dengesizlik metriğinin kabaca hesaplanmasına razı olmak anlamına gelir ancak veri boyutu ve simülasyon hızı açısından belirgin kazanç sağlar. İkincisi, FOK limit emirleri seçilerek "kısmi doldurma → istenmeyen envanter" riski elimine edilir; karşılığında dengesizlik sinyaline rağmen fill oranı düşer. Üçüncüsü, proxy enstrüman yaklaşımı likidite karakteristiğini korurken tam mikroyapı sadakatinden feragat eder — özellikle funding etkileri modellenmez.

**İlgili sayfalar:**
- [[tutorial_data_catalog_databento]]
- [[tutorial_fx_mean_reversion_ax]]
- [[order_flow_pipeline]]
- [[data_engine]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_data_catalog_databento]]
- [[tutorial_fx_mean_reversion_ax]]
- [[tutorial_hurst_vpin_kraken]]
<!-- BACKLINKS:END -->
