---
title: Lighter RWA Üzerinde Kompozit Piyasa Yapıcı
type: tutorial
sources:
  - https://nautilustrader.io/docs/nightly/tutorials/lighter_rwa_composite_mm/
last_updated: 2026-07-06
summary: Databento hisse sinyalini Lighter RWA perp yürütmesiyle birleştiren kompozit market maker; LiveNode üzerinde çoklu istemci ve skew tabanlı kotasyon anlatır.
key_concepts:
  - howto_get_started_lighter
  - adapters
  - execution_engine
  - event_driven_architecture
  - strategy_and_actor
  - environment_contexts
---

Bu öğretici, iki bağımsız piyasa beslemesini tek bir stratejide birleştiren bir piyasa yapıcı (market maker) kurgusunu gösterir: Databento üzerinden gelen ABD hisse senedi verisi (`NVDA.EQUS`) sinyal görevi görürken, Lighter borsasındaki gerçek dünya varlığı (RWA) türetilmiş sözleşme (`NVDA-PERP.LIGHTER`) emir yürütme yeri olarak kullanılır. Amaç, olay güdümlü çekirdek altında birden fazla veri kaynağı ile tek bir yürütme kanalını nasıl bir arada koşturacağınızı öğretmektir.

Ana yapı taşları Rust tarafındadır. `LiveNode`, üç ayrı istemciyi (Databento veri, Lighter veri, Lighter yürütme) tek bir düğümde bir araya getirir. `CompositeMarketMaker` stratejisi hem hedef enstrümanı hem de sinyal enstrümanını girdisi olarak alır; `CompositeMarketMakerConfig` üzerinden `signal_skew_factor` ve `inventory_skew_factor` gibi parametrelerle davranışı ayarlanır. Fabrikalar (`DatabentoDataClientFactory`, `LighterDataClientFactory`, `LighterExecutionClientFactory`) `LiveNode::builder` üzerinden kaydedilir; harici bir `publishers.json` dosyası venue-veri kümesi eşlemesini sağlar.

```rust
let mut node = LiveNode::builder(trader_id, Environment::Live)?
    .add_data_client(None, Box::new(DatabentoDataClientFactory::new()),
                     Box::new(databento_config))?
    .add_data_client(None, Box::new(LighterDataClientFactory::new()),
                     Box::new(lighter_data_config))?
    .add_exec_client(None, Box::new(LighterExecutionClientFactory::new()),
                     Box::new(lighter_exec_config))?
    .build()?;
```

Kotasyon mantığında sinyal artığı `(databento_mid / baseline) - 1.0` şeklinde normalize edilir ve nihai fiyat `signal_skew_factor * residual - inventory_skew_factor * net_position` ile kaydırılır. Kotasyonlar sürekli değil, yalnızca eşik geçildiğinde yenilenir; bu, emir dalgalanmasını azaltır.

Tasarım ödünleşmeleri: strateji bilinçli olarak otomatik sinyal-yaşı denetimi veya oturum kapısı içermez; operatörden bir aktör ile Databento akışı bayatladığında kotasyonları iptal etmesi beklenir. Ayrıca Lighter kesintisiz işlem görürken Databento ABD borsa saatlerine bağlıdır; bu senkron olmayan takvim, gizlenmek yerine strateji dışına taşınmıştır. Bu açıklık ilkesi, üretim ortamı için ek koruma katmanlarının okuyucuya net biçimde ait olduğunu gösterir.

**İlgili sayfalar:**
- [[howto_get_started_lighter]]
- [[adapters]]
- [[execution_engine]]
- [[event_driven_architecture]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[howto_get_started_lighter]]
<!-- BACKLINKS:END -->
