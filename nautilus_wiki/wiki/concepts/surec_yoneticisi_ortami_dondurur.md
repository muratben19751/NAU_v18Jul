---
title: Süreç yöneticisi ortamı dondurur
type: concept
summary: Sunucu pm2 altında koştuğu için ortam değişkenleri süreç BAŞLARKEN dondurulur; kabukta değiştirmek hiçbir şey yapmaz, `pm2 env` yapılandırmadan okuduğu için yanlış güvence verir, ve ayarın yürürlükte olduğunu doğrulamak yürütmeyi doğrulamaz.
sources: []
last_updated: 2026-08-18
---

# Süreç yöneticisi ortamı dondurur

NAU sunucusu 2026-08-14'ten beri pm2 altında koşuyor (`nau-web`,
`ecosystem.config.js`). Bunun doğrudan bir sonucu var ve üç ayrı turda üç ayrı
şekilde ısırdı: **ortam değişkenleri sürecin başladığı anda dondurulur.**
Kabukta `NAUTILUS_X=...` yazmak koşan uygulamayı etkilemez; yeni değer
`pm2 restart <app> --update-env` olmadan içeri girmez.

Bundan üç ayrı ders çıktı ve üçü de farklı bir yanılgıyı kapatıyor.

## 1. `pm2 env` süreci değil YAPILANDIRMAYI okur

2026-08-17'de `pm2 env <id>` çıktısında `PM2_HOME` görünmediği için erişim
kapısının canlıda kapalı olduğunu, uygulamanın tünelden kimlik doğrulamasız
servis ettiğini raporladım. **Yanlıştı.** Doğrudan ölçüm — pm2 altında üç
satırlık geçici bir uygulama — `PM2_HOME`'un çocuk süreçte VAR olduğunu
gösterdi.

İkinci "kanıt" da araç varsayılanıydı: `urllib.urlopen` yönlendirmeyi takip
edip `303 → /login → 200` zincirini tek bir `200` gibi gösterdi. İki kanıt da
aynı türdendi (araca sor), yani tek kanıttı. Kod doğru olduğu için kaldı,
GEREKÇESİ düzeltildi — yanlış gerekçe sonraki okuyucuyu olmayan bir arızayı
aramaya gönderirdi.

## 2. Ayarın yürürlükte olduğunu doğrulamak, yürütmeyi doğrulamaz

2026-08-16: `NAUTILUS_LLM_CALL_TIMEOUT=300` ayarlıydı, `pm2 env` doğruluyordu,
davranış eskiydi. Koşu `0057a0cd`'de iki çağrı
`OpenRouter call exceeded 120s hard deadline` ile düştü — çünkü zamanaşımı o
DALA hiç konmuyordu. Teşhisi veren şey hata metnindeki SAYI oldu: 300 ayarlayıp
120 görmek.

Env doğru + yapılandırma doğru + davranış eski üçlüsü, ayarın hedefe değil
varsayılana baktığının imzasıdır.

## 3. Ölçümü uygulamanın ortamında al

Üretimde model seçici ağdan gelmiyor: `ecosystem.config.js`
`NAUTILUS_OPENROUTER_MODELS: "qwen3.8-27b"` **pin**'ini veriyor ve pin listenin
yerine geçiyor. Aynı kontrol pm2'nin env'i olmayan bir kabukta koşturulunca
openrouter.ai'nin 25 satırlık gerçek listesi görünüyor, sabit "listede yok"
çıkıyor ve **yanlış yöne düzeltilmek isteniyor**.

Test bunu ortamdan bağımsız bağladı: `tests/test_studio_page.py` sabiti env'e
değil `ecosystem.config.js`'teki pin'e karşı doğruluyor.

## Bunun kataloğa yansıması

`nau_config.py` her ortam değişkenini adı/tipi/varsayılanı/okuyucusu ile
listeler ve `tests/test_env_registry_is_complete.py` iki yönlü sürüklenmeyi
kırmızıya çevirir: kodda okunup katalogda yazılmayan da, katalogda yazılıp
kodda hiç okunmayan da testi düşürür. Bir katalog, güncel tutulması bir SÜREÇ
ise çürür; ihlal edildiğinde kırılan bir test ise durur.

İlgili: [[kesilme_ve_degrade_gorunurlugu]], [[model_secici_ve_gorunurluk]],
[[webapp_module_map]] (`serve.py` — süreç sarmalayıcısı, `reload=False`'un
neden bilinçli olduğu).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[model_secici_ve_gorunurluk]]
- [[nau_deepr_mimari_katman_ayrimi]]
<!-- BACKLINKS:END -->
