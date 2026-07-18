---
title: dYdX v4 Üzerinde Zincir-İçi Grid Piyasa Yapıcılık
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/grid_market_maker_dydx
last_updated: 2026-07-06
summary: dYdX v4 short-term order modeli üzerinde envanter-skew'lu grid piyasa yapıcı — GTD süre dolumu, geometrik grid ve requote eşiği ile on-chain maliyet optimizasyonu.
key_concepts:
  - adapters
  - execution_engine
  - order_flow_pipeline
  - tutorial_grid_market_maker_bitmex
  - strategy_and_actor
  - rust_python_hybrid
---

Bu eğitim, dYdX v4'ün **short-term order** (kısa vadeli emir) modeline uyarlanmış bir grid piyasa yapıcının Nautilus üzerinden nasıl kurulacağını anlatır. Bu emirler blok yüksekliği ile süresi dolarak zincir-üstü iptal (gas maliyetli işlem) gerektirmez — bu, on-chain venue'ya özgü kritik bir maliyet optimizasyonudur. Ek olarak Avellaneda–Stoikov'dan esinlenen envanter-bazlı grid kaydırma (skew) uygulanır.

Ana bileşenler: `GridMarketMaker` stratejisi ve `GridMarketMakerConfig` (parametreler: `max_position`, `grid_step_bps`, `skew_factor`, `expire_time_secs`, `requote_threshold_bps`); Rust `LiveNode` çalışma zamanı; `DydxDataClientFactory` ve `DydxExecutionClientFactory` istemci fabrikaları. Strateji `QuoteTick` aboneliği üzerinden `on_quote()` içinde tetiklenir.

Geometrik grid fiyatlandırması ve envanter kaydırması iç içedir:

```rust
let skew_f64 = self.config.skew_factor * net_position;
let buy_f64  = mid_f64 * (1.0 - pct).powi(level as i32) - skew_f64;
let sell_f64 = mid_f64 * (1.0 + pct).powi(level as i32) - skew_f64;
```

Net pozisyon uzunsa tüm seviyeler aşağı, kısaysa yukarı kaydırılır — böylece piyasa yapıcının maruziyeti ortalamada sıfıra çekilir. Short-term emirlerin süre dolumu ise doğrudan `TimeInForce::Gtd` ile nanoseconds-precision ifade edilir:

```rust
let expire_ns = now_ns + secs * 1_000_000_000;
(Some(TimeInForce::Gtd), Some(expire_ns))
```

Emir miktarı çözümlemesi sıralı bir fallback zinciriyle çalışır: önce config değeri, sonra `instrument.min_quantity`, en son `1.0`. Bu, farklı enstrümanlar arasında portatif konfigürasyon sağlar.

Öne çıkan tasarım ödünleşimleri: (1) **Süre dolumu vs açık iptal**: on-chain süre dolumu ücretsizdir fakat strateji tarafı beklenmeyen iptalleri `pending_self_cancels` mekanizmasıyla izlemek zorundadır. (2) **Requote hassasiyeti**: düşük `requote_threshold_bps` daha taze kotasyon ama daha fazla işlem hacmi demektir; yüksek eşik ise bayat kotasyon riski taşır. (3) **Skew agresifliği**: aşırı `skew_factor` grid'i mid'den tamamen uzaklaştırıp fill'i sıfırlayabilir — envanter kontrolü ile likidite sunumu arasında kaba bir denge gerekir.

**İlgili sayfalar:**
- [[tutorial_grid_market_maker_bitmex]]
- [[execution_engine]]
- [[adapters]]
- [[order_flow_pipeline]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_grid_market_maker_bitmex]]
<!-- BACKLINKS:END -->
