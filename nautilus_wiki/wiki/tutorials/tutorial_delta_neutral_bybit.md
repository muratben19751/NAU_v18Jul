---
title: Bybit Üzerinde Delta-Nötr Opsiyon Stratejisi
type: tutorial
sources:
  - https://nautilustrader.io/docs/nightly/tutorials/delta_neutral_options_bybit/
last_updated: 2026-07-06
summary: Bybit'te strangle + perpetual hedge ile short-vol stratejisi kurmayı; OptionGreeks aboneliği, order_iv/iv_param_key ve reconciliation ile canlı delta-nötr operasyonu anlatır.
key_concepts:
  - strategy_and_actor
  - execution_engine
  - adapters
  - tutorial_options_data_bybit
  - tutorial_delta_neutral_derive
  - event_driven_architecture
---

Bu öğretici, Bybit üzerinde canlı çalışan bir kısa-volatilite (short-vol) opsiyon stratejisinin nasıl kurulacağını gösterir. Strateji, bir strangle pozisyonu açtıktan sonra portföyün delta değerini sürekli olarak lineer (perpetual) enstrümanla hedge ederek nötr tutar; öğretici enstrüman keşfi, Greeks tüketimi, IV tabanlı emir gönderimi ve dinamik yeniden dengeleme adımlarını beş operasyonel evre hâlinde işler.

Kullanılan başlıca API'ler `LiveNode` etrafında toplanır: `BybitDataClientConfig` ve `BybitExecClientConfig`, hem `Option` hem de `Linear` ürün türlerini aynı venue altında etkinleştirir. `DeltaNeutralVol` stratejisi çekirdek işlem mantığını taşır ve `OptionGreeks` aboneliklerinden gelen delta/IV verilerine dayanır. IV tabanlı fiyatlama için standart fiyat alanı yerine `Params` nesnesine `order_iv` anahtarıyla değer eklenir; Bybit adaptörü bunu `orderIv` alanına eşler.

```rust
let mut call_params = Params::new();
call_params.insert("order_iv".to_string(), json!(call_entry_iv.to_string()));
self.submit_order(call_order, None, Some(client_id), Some(call_params))?;
```

Öğretici, farklı venue'lerin bu alanı nasıl adlandırdığına dikkat çeker: Bybit `orderIv`, OKX ise `px_vol` kullanır; `iv_param_key` yapılandırması bu farkı soyutlar. Başlangıçta mevcut emir ve pozisyonların yüklenmesi için `reconciliation(true)` bayrağı açılır; böylece strateji başlamadan önce hesap durumu hidrasyona tabi tutulur.

Tasarım ödünleşmeleri şunlardır: strike seçimi tüm Greeks sorgulanmadan yüzdelik sıralama sezgiselleri ile yapıldığı için optimalliği açılış hızına feda eder. Yeniden hedge kararı olay tetiklemeli güncellemelerle 30 saniyelik periyodik güvenlik zamanlayıcısını birleştirerek tepki süresi ile API yükü arasında denge kurar. Strateji durdurulduğunda pozisyonları otomatik kapatmaz; çıkış mantığı ayrı bir bileşene veya operatöre bırakılır. Bu bilinçli sadeleştirme, öğretici mantığını kirletmeden gerçek dünyada gereken güvenlik katmanlarını okuyucuya bırakır.

**İlgili sayfalar:**
- [[option_greeks_pipeline]]
- [[tutorial_options_data_bybit]]
- [[tutorial_delta_neutral_derive]]
- [[execution_engine]]
- [[strategy_and_actor]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[option_greeks_pipeline]]
- [[tutorial_delta_neutral_derive]]
- [[tutorial_options_data_bybit]]
<!-- BACKLINKS:END -->
