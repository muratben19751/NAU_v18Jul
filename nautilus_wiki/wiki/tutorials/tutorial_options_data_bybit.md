---
title: Bybit Opsiyon Verileri ve Greeks Akışı
type: tutorial
sources:
  - https://nautilustrader.io/docs/nightly/tutorials/options_data_bybit/
last_updated: 2026-07-06
summary: Bybit opsiyon akışını DataActor ile tüket — tekil Greeks abonelikleri veya ATM tabanlı OptionChainSlice snapshot'ları; cache borrowing disiplinine dikkat.
key_concepts:
  - adapters
  - data_engine
  - strategy_and_actor
  - tutorial_delta_neutral_bybit
  - tutorial_delta_neutral_derive
---

Bu Rust tabanlı öğretici, Bybit üzerinden canlı opsiyon piyasa verilerinin nasıl tüketileceğini iki farklı desen üzerinden gösterir: tekil opsiyon sözleşmeleri için Greeks aboneliği ve tüm bir opsiyon serisinin periyodik zincir anlık görüntüleri (chain snapshots) hâlinde toplanması. Amaç, enstrüman keşfi, ATM tabanlı filtreleme ve verimli veri işleme akışını uygulamalı olarak öğretmektir.

Öğreticinin temel yapı taşları şunlardır: `DataActor` sınıfı, `on_option_greeks` ve `on_option_chain` geri çağırma yöntemleriyle veri tüketiminin çekirdeğini oluşturur. `OptionGreeks` olayı delta, gamma, vega, theta, rho, örtük volatilite ve dayanak varlık fiyatını taşır; `OptionChainSlice` ise bir vade serisindeki tüm strike'ları call/put ayrımıyla birleştirir. Vade seçimi `OptionSeriesId` (venue, dayanak, takas para birimi ve son kullanım tarihi) ile yapılır. Aktif strike aralığı ise `StrikeRange::Fixed`, `AtmRelative` veya `AtmPercent` varyantlarıyla kontrol edilir. Tüm bileşenler `LiveNode` altında koşar; `BybitDataClientConfig` üzerinden `product_types` alanına `Option` eklenerek opsiyon akışı etkinleştirilir.

Öğreticinin dikkat çektiği önemli bir nokta önbellek ödünç alma (cache borrowing) disiplinidir: `Rc<RefCell<...>>` yapısı nedeniyle abonelik yöntemleri çağrılmadan önce cache ödünç alma serbest bırakılmalıdır. `AtmRelative` filtresinde ise abonelikler venue tarafından sağlanan forward fiyata dayalı ATM belirlenene kadar ertelenir.

```rust
// Zincir aboneliği: DataEngine tüm strike'ları toplayıp
// tek bir OptionChainSlice yayınlar.
self.subscribe_option_chain(series_id, StrikeRange::AtmPercent(0.10), None, None);
```

Tasarım ödünleşmeleri açıktır: sözleşme başına Greeks abonelikleri hassas kontrol sağlar ama strike'lar arası korelasyonu manuel yönetmeyi gerektirir; buna karşılık zincir anlık görüntüleri yüzey düzeyinde izlemeyi kolaylaştırır ancak sabit aralıklarla toplanır. Ayrıca tüm opsiyon enstrümanlarının başlangıçta yüklenmesi düğüm açılışını yavaşlatır fakat sonraki filtrelemeleri verimli kılar. Venue kaynaklı Greeks, her opsiyon ticker güncellemesiyle birlikte geldiğinden ayrı bir hesaplama katmanına gerek kalmaz.

**İlgili sayfalar:**
- [[option_greeks_pipeline]]
- [[data_engine]]
- [[adapters]]
- [[tutorial_delta_neutral_bybit]]
- [[tutorial_delta_neutral_derive]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_delta_neutral_bybit]]
- [[tutorial_delta_neutral_derive]]
<!-- BACKLINKS:END -->
