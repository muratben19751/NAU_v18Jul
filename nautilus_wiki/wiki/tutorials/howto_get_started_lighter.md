---
title: Lighter ile Başlangıç Rehberi (yer tutucu)
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/get_started_with_lighter
last_updated: 2026-07-06
summary: Lighter başlangıç öğreticisi yayınlanana kadar yer tutucu; adaptör bileşenleri, LiveNode kurulumu ve testnet bağlantı beklentilerini özetler.
key_concepts:
  - adapters
  - execution_engine
  - nautilus_kernel
  - tutorial_lighter_rwa_composite_mm
---

Bu sayfa, `get_started_with_lighter` öğreticisi için bir yer tutucudur. 2026-07-06 tarihinde beklenen URL (`https://nautilustrader.io/docs/latest/tutorials/get_started_with_lighter` ve varyantları: `get-started-with-lighter`, `getting_started_lighter`, `lighter_quickstart`, `lighter_getting_started`) NautilusTrader dokümantasyon sitesinde 404 döndürmüştür. `nightly` sürümündeki öğretici indeksi `/docs/nightly/tutorials/` altında bu isimde bir öğretici listelememektedir; yalnızca `lighter_rwa_composite_mm` isimli ileri seviye örnek mevcuttur.

Lighter entegrasyonuyla ilgili resmi bilgi bugün için `Integrations` bölümündeki referans belgesinde toplanmıştır (`/docs/nightly/integrations/lighter/`). Bu belge; adaptörün ana bileşenlerini (`LighterRawHttpClient`, `LighterHttpClient`, `LighterWebSocketClient`, `LighterDataClient`, `LighterExecutionClient` ve ilgili fabrikalar), yapılandırma seçeneklerini (mainnet/testnet ortamı, API anahtarları, zaman aşımı ve kota ayarları), desteklenen emir tiplerini (market, limit, perpetual için koşullu emirler), zaman-geçerlilik ayarlarını, netleme (netting) modunda pozisyon yönetimini, kaldıraç güncellemelerini ve marj işlemlerini kapsar. Ayrıca hesap kademelerine bağlı REST ve işlem kotalarının nasıl ayarlanacağını, "Yürütme istemcisi eksik kimlik bilgilerini reddeder; veri istemcisi kimlik bilgisiz de çalışır." kuralını da açıklar.

Öğreticinin kendisi yayımlandığında bu sayfa, tipik olarak beklenen içeriklerle güncellenmelidir: `LiveNode` üzerinde `LighterDataClientConfig` ve `LighterExecClientConfig` kurulumu, testnet ile ilk bağlantı, temel bir enstrüman aboneliği ve küçük bir emir gönderim örneği. O zamana kadar okuyucuların mevcut `lighter_rwa_composite_mm` öğreticisindeki `LiveNode::builder` kalıbını referans alarak minimum bir tek-istemci düğümü kurmaları önerilir.

Bu yer tutucu, bağlantı çürümesini engellemek için wiki içi çapraz referansları geçerli kılar; içerik gerçek öğretici bulunduğunda yeniden sentezlenmelidir.

**İlgili sayfalar:**
- [[tutorial_lighter_rwa_composite_mm]]
- [[adapters]]
- [[nautilus_kernel]]
- [[execution_engine]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_lighter_rwa_composite_mm]]
<!-- BACKLINKS:END -->
