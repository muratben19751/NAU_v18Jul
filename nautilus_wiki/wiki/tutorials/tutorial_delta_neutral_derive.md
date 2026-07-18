---
title: Derive Üzerinde Delta-Nötr Opsiyon Stratejisi
type: tutorial
sources:
  - https://nautilustrader.io/docs/nightly/tutorials/delta_neutral_options_derive/
last_updated: 2026-07-06
summary: Derive adaptörüyle strangle+perpetual delta hedge Rust stratejisini kurar; option_pricing yükünden türetilen Greeks ve LiveNode reconciliation akışını gösterir.
key_concepts:
  - adapters
  - strategy_and_actor
  - tutorial_delta_neutral_bybit
  - tutorial_options_data_bybit
  - execution_engine
  - environment_contexts
---

Bu Rust öğreticisi, Derive borsasında canlı çalışan bir kısa-volatilite opsiyon stratejisinin uygulanmasını ele alır. Bybit öğreticisiyle paylaşılan `DeltaNeutralVol` stratejisi burada Derive adaptörüyle birleştirilerek strangle pozisyonlarının açılması ve perpetual bacağıyla delta hedge işleminin yürütülmesi gösterilir. Amaç, aynı strateji çekirdeğinin farklı venue adaptörleri altında nasıl yeniden kullanılabildiğini somutlaştırmaktır.

Temel sınıflar: `LiveNode` veri ve yürütme istemcileriyle strateji yaşam döngüsünü yönetir. `DeriveDataClientConfig` ve `DeriveExecFactoryConfig` testnet/mainnet ortam seçimini ve API kimlik bilgilerini kapsar. Strateji, `InstrumentId` üzerinden ilgili opsiyon bacaklarını çözer ve her bacak için `OptionGreeks` verisine abone olur. Derive adaptörünün kayda değer bir özelliği vardır: Greeks değerleri ayrı bir uç noktadan değil, venue tarafından yayınlanan `option_pricing` yüklerinden türetilir.

```rust
// Her iki bacak için Greeks aboneliği
self.subscribe_option_greeks(call_id, Some(client_id), None);
self.subscribe_option_greeks(put_id, Some(client_id), None);

// Portföy delta hesaplaması
portfolio_delta = call_delta * call_position
                + put_delta  * put_position
                + hedge_position;
```

Reconciliation adımı, strateji başlamadan önce hesap durumunu hidrasyon ile getirir; böylece açık pozisyonlar delta hesabına doğru şekilde katılır. Tasarım ödünleşmeleri Bybit sürümüne benzer: strike seçimi tam zincir aboneliği yerine yüzdelik sezgiselleriyle yapılır, bu da basitlik uğruna hassasiyetten ödün verir. Emniyet için `enter_strangle: false` bayrağı yalnızca hedge modunu etkinleştirir; giriş emirleri gönderilmez ancak mevcut pozisyonlar yönetilmeye devam eder. Piyasa emirleri gönderim öncesi slippage sınırlarıyla imzalanır: bu, gecikmeyi azaltır fakat ticker verisinin taze olmasını gerektirir. Öğretici, aynı strateji sınıfını iki farklı venue'de çalıştırarak Nautilus adaptör soyutlamasının pratik değerini kanıtlar.

**İlgili sayfalar:**
- [[option_greeks_pipeline]]
- [[tutorial_delta_neutral_bybit]]
- [[tutorial_options_data_bybit]]
- [[adapters]]
- [[strategy_and_actor]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[option_greeks_pipeline]]
- [[tutorial_delta_neutral_bybit]]
- [[tutorial_options_data_bybit]]
<!-- BACKLINKS:END -->
