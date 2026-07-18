---
title: Betfair Emir Defteri Dengesizliği Backtest'i
type: tutorial
sources:
  - https://nautilustrader.io/docs/nightly/tutorials/backtest_book_imbalance_betfair/
last_updated: 2026-07-06
summary: Betfair MCM akışında Rust tabanlı BookImbalanceActor ile bid/ask dengesizliğini hesaplayıp DataActor ve managed=false abonelikle salt-okunur backtest çalıştırır.
key_concepts:
  - backtesting_guide
  - strategy_and_actor
  - order_flow_pipeline
  - data_engine
  - adapters
  - rust_python_hybrid
---

Bu Rust öğreticisi, Betfair spor bahis borsasının tarihsel akış verileri üzerinde bir piyasa mikroyapı stratejisinin nasıl backtest edileceğini gösterir. Öğretici, gzip sıkıştırılmış Betfair MCM (Market Change Message) dosyalarının yüklenmesini, emir defteri delta güncellemelerinin işlenmesini ve bid/ask hacim dengesizliği metriğinin hesaplanmasını uçtan uca kapsar.

Kullanılan başlıca bileşenler: `BetfairDataLoader`, MCM dosyalarını `Instrument`, `Deltas`, `Trade`, `InstrumentClose` gibi Nautilus varyantlarına dönüştürür. `BacktestEngine` çekirdek simülasyon döngüsünü Rust'ta doğrudan işler; `SimulatedVenueConfig` ise Betfair için nakit hesap türü ve L2 defter yapısı gibi ayarları belirtir. Sinyal hesabı, `BookImbalanceActor` içinde yayınlanır ve `DataActor` çekirdeğine bir yer tutucu (`DataActorCore`), `nautilus_actor!` makrosu ve `DataActor` trait implementasyonu ile bağlanır.

Dengesizlik formülü açık bir şekilde şudur:

```rust
// imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
let imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol);
```

Abonelik kurulumunda `managed: false` bayrağı önemli bir ayrıntıdır: bu, DataEngine'in cache içinde tam bir emir defteri kopyası tutmamasını sağlar; eşleşme motoru yine kendi içinde defteri yönetir. Aktör yalnızca delta akışını topladığından bu, gereksiz kopyalamayı ortadan kaldırır.

Tasarım ödünleşmeleri iki eksende görülür. Birincisi, piyasa durum geçişleri (Suspended, Closed) varsayılan olarak yeniden oynatılmaz; bu, emir yerleştirilmediği için hızı artırır ama gerçek işlem stratejileri için ek durum yönlendirmesi gerektirir. İkincisi, Rust yolunun saniyede yaklaşık üç milyon veri noktasını işleyebilmesi Python/Cython yollarına kıyasla yaklaşık altı kat hız kazandırır; bunun karşılığında geliştirici Rust ergonomisiyle çalışmayı kabul eder. Öğretici, strateji yerine salt-okunur bir aktör kullanarak sinyal hesaplamasını yürütme karmaşıklığından yalıtır.

**İlgili sayfalar:**
- [[backtesting_guide]]
- [[strategy_and_actor]]
- [[order_flow_pipeline]]
- [[data_engine]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
<!-- BACKLINKS:END -->
