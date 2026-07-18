---
title: Kraken Futures Üzerinde Hurst/VPIN Yönlü Stratejisi (Rust)
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/hurst_vpin_kraken
last_updated: 2026-07-06
summary: Rust motorunda dolar-bar (VALUE) sampling ile Hurst üstsü ve VPIN sinyallerini hizalayan çok-hatlı Kraken vadeli backtest kurgusu ve Tardis precision tuzağı.
key_concepts:
  - strategy_and_actor
  - backtesting_guide
  - event_driven_architecture
  - precision_modes
  - rust_python_hybrid
  - order_flow_pipeline
---

Bu Rust-tabanlı eğitim, rejim tespiti (Hurst üstsü) ile akış toksisitesi metriğini (VPIN — Volume-Synchronized Probability of Informed Trading) birleştiren yönlü bir strateji üzerinden Kraken kripto vadeli sözleşmelerinde uçtan uca bir backtest kurgusu gösterir. Öğretilen değer, sinyal hesabından çok Rust motorunda **dolar-bar (value-aggregated) sampling** ile birden çok veri boru hattının senkronize kullanımıdır.

Kullanılan ana bileşenler: `BacktestEngine` ve `BacktestEngineConfig`; enstrüman için `CryptoPerpetual` builder; venue için `SimulatedVenueConfig` (`OmsType::Netting`, `AccountType::Margin`); veri için `nautilus_tardis::csv::load::{load_trades, load_quotes}` ve `Data::Trade`/`Data::Quote` varyantları; strateji tarafında `HurstVpinDirectional` sınıfı ve config builder'ı.

Dolar-barlar için `VALUE` toplama tipi kullanılır — sabit süre ya da sabit tick sayısı yerine sabit nominal değer eşiği:

```
BarType::from("PF_XBTUSD.KRAKEN-2000000-VALUE-LAST-INTERNAL")
```

Bu string, 2.000.000 USD nominal biriktiği anda bar kapanacağı ve tick akışından motor içinde (`INTERNAL`) toplanacağı anlamına gelir. Value-aggregated barlar özellikle VPIN gibi hacim-senkronize metrikler için uygundur.

Strateji üç eşzamanlı hattı yürütür: (1) **Trade hattı** — `TradeTick::aggressor_side` alanına göre agresif alım/satım hacmini dolar-bucket bazında biriktirir; (2) **Bar hattı** — kapanışta yuvarlanan pencere üzerinde Hurst üstsü (R/S regresyonu) ve VPIN hesaplar; (3) **Quote hattı** — her iki sinyal hizalandığında market IOC emirleriyle giriş yapar ve pozisyon zaman aşımını yönetir.

Kritik ama gizli bir ayrıntı: Tardis yükleyicisi hassasiyeti ilk birkaç kayıttan çıkarır, bu da matching-engine reddine yol açar. Enstrümanın `price_precision` ve `size_precision` değerleri **açıkça** yükleyiciye verilmelidir.

Tasarım ödünleşimleri: (1) Dolar-bar hem Hurst penceresini hem VPIN bucket'larını beslediği için sinyal senkronizasyonu sağlar ama iki metriğin bağımsız kalibrasyonu feda edilir. (2) Hurst > 0.55 ve VPIN > 0.30 çift-koşulu spesifikliği artırır ama entry sayısını çok azaltır (14 günde ~2 işlem). (3) 128-bar Hurst + 50-bucket VPIN warmup, büyük dolar-barlarda çok günlük veri gerektirir.

**İlgili sayfalar:**
- [[tutorial_gold_book_imbalance_ax]]
- [[strategy_and_actor]]
- [[backtesting_guide]]
- [[event_driven_architecture]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
<!-- BACKLINKS:END -->
