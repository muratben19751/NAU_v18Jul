---
title: AUTO 360° canlı inceleme ve güvenilirlik iyileştirmeleri
type: synthesis
summary: İki canlı AUTO koşusunda bulunan rol, WFO, holdout, fallback, maliyet ve oturum kaydı kusurları; düzeltme planı ve doğrulama sözleşmeleri.
key_concepts:
  - auto_mission_control
  - auto_kapi_ve_geri_bildirim
  - auto_arama_ekonomisi
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/auto_arama_ekonomisi.md
  - wiki/synthesis/llm_maliyet_kaldiraclari.md
  - wiki/synthesis/nau_performans_denetimi.md
last_updated: 2026-08-05
---

# AUTO 360° canlı inceleme ve güvenilirlik iyileştirmeleri

Bu sayfa 2026-08-05 tarihli iki gerçek AUTO koşusunun (`6432552f`, `bf598e4f`)
kod, performans, mimari, token/maliyet ve backtest-edge incelemesini kaydeder.
[[auto_kapi_ve_geri_bildirim]] önceki kapı düzeltmelerini anlatır; burada canlı
üretimde kalan ve kazanan güvenilirliğini doğrudan bozan kusurlar ele alınır.

## Değişiklik öncesi kanıt tabanı

| ölçüm | `6432552f` | `bf598e4f` |
|---|---:|---:|
| süre | 38 dk 41 sn | 1 sa 14 dk 33 sn |
| tur | 3 etkin tur; bitiş sayacı 4 | 36 |
| token | 162.049 | 105.050 |
| oturum JSONL | 3,43 MB | 46,87 MB |
| kazanan | 1 | 13 |
| degraded/fallback | sınırlı | 234 |

İkinci koşuda 173 backtest sonucu, 84 robustness adayı ve 13 katalog yazımı
oluştu. 14 `passed=true` robustness sonucunun 12'sinde pozitif WFO pencere oranı
%50'nin altındaydı. 13 sealed holdout'un yalnız dördü `measured=true` idi; bunlar
da 2, 5, 5 ve 6 işlem taşıyordu. 173 adayın en az 130'unda blok üretim kökeni ile
atandığı entry/exit rolü uyuşmuyordu; 13 kazananın 11'i bu sınıftaydı.

## Kritik doğruluk kusurları

1. **Exit semantiği fail-open.** `composer.py` exit sonucunu `bool(r)` ile
   yorumladığı için exit bloğunun hatalı `"long"`/`"short"` çıktısı da pozisyonu
   kapatıyordu. Üretim smoke testi bloğu daima `role="entry"` ile çalıştırıp
   `{None,long,short,exit}` kümesinin tamamını kabul ettiği için sözleşme ihlalini
   yakalamıyordu.
2. **Fallback rol kökenini korumuyor.** `agnt_x_*` exit bloğu entry, `agnt_e_*`
   entry bloğu exit olarak kompoze edilebildi. Degraded rastgele üretim, LLM
   kalitesinden bağımsız bu adayları normal adaylarla yarıştırdı.
3. **WFO ekran sözleşmesi kapıyla aynı değil.** Ekran “pozitif pencere ≥%50”
   derken kapı, cezalı Sharpe mevcutsa yalnız onun `>0` olmasına bakıyordu.
   Böylece 0/88, 4/88 ve 9/88 pencere sonuçları bile geçebildi.
4. **Multi-symbol reddi pahalı aşamaları durdurmuyor.** Kesin sembole-özel `✗`
   sonucu sonrasında IS/OOS ve WFO yine çalıştı; gözlenen en az 101,6 saniye WFO
   işi karar sonucunu değiştiremeyeceği halde harcandı.
5. **Katalog yazımı holdout'tan önce.** Kazanan önce kalıcı kataloğa ekleniyor,
   sealed holdout daha sonra yalnız raporlanıyor. Ölçülemeyen veya zayıf holdout
   hiçbir yayımlama kararını engellemiyor.

## Performans ve maliyet kusurları

- OpenRouter kredi hatası (`HTTP 402`) terminal nitelikte olmasına rağmen 234
  degraded fallback üretildi. “Üç aynı hata” devre kesicisi yalnız turu saran
  exception yolunda çalışıyor; yakalanıp fallback'e çevrilen LLM hatalarını görmüyor.
- Sürekli modda `max_hours=0` ve `max_total_tokens=0` maliyeti sınırsız bırakıyor.
  İki koşunun kayıtlı toplamı 267.099 token; ikinci koşuda UI maliyeti `$2.38`
  gösterirken oturum defterinde `cost_usd=null` kaldı.
- İkinci koşuda backtest süreleri toplam 1.465,5 sn, aday robustness toplamı
  2.392,3 sn, WFO alt aşamaları 1.569,1 sn oldu. En yavaş iki 15m backtest
  151,64 ve 111,12 saniyeydi.
- `backtest_result.metrics` içindeki ham `equity_curve_mtm`/realized serileri,
  üst düzey eğri seyreltmesine rağmen JSONL'yi büyütmeye devam etti. Gözlenen hız
  yaklaşık 37,5 MB/saat; geçmiş `agent_sessions` dizini 11,18 GB idi.

## Backtest-edge riskleri

- QQQ verisinin ayarlanmamış olduğu bildirildi; 2011-03-23 tarihinde `%40+`
  tek-bar hareket split/corporate-action şüphesi doğurdu. Bu temizlenmeden büyük
  PnL ve drawdown değerleri güvenilir değildir.
- 51 sonuç sıfır, 87 sonuç 20'den az işlem taşıdı. On dokuz tek-işlem adayının
  on altısında PnL 10.000 doların üzerindeydi; en büyüğü 10.000 dolar başlangıç
  sermayesinde yaklaşık 181.029 dolardı. Bu, veri/ölçek/rol kusurunun edge gibi
  sıralanabildiğini gösterir.
- Aynı 60 günlük sealed holdout'un 13 kazanan için tekrar okunması, sonuç modele
  geri beslenmese bile insan seçimi üzerinden holdout kontaminasyonu ve çoklu
  deneme riski yaratır.
- Oturum başlangıcında açık bir rastgele tohum kaydı yoktu; degraded rastgele
  arama yeniden üretilebilir değildi.

## Uygulama kabul kriterleri

- Entry bloğu yalnız `long|short|None`, exit bloğu yalnız `exit|None` döndürebilir;
  üretim ve runtime aynı sözleşmeyi uygular.
- WFO geçişi için hem cezalı Sharpe `>0` hem geçerli pencerelerin en az %50'sinde
  pozitif PnL gerekir; geçerli pencere yoksa fail-closed olur.
- Kesin multi-symbol başarısızlığı IS/OOS/WFO/MC işini başlatmadan adayı reddeder.
- Katalog yalnız ölçülmüş ve politika eşiğini geçen sealed holdout sonrasında
  yazılır; ölçülemeyen holdout yayımlanmaz.
- Terminal LLM kredi/yetki hataları devreyi açar; degraded fallback kazanan olamaz.
- Sürekli koşularda sonlu token ve süre varsayılanı bulunur; maliyet tek defterden
  hem UI'ye hem oturuma taşınır.
- Oturum logu karar için gereken kompakt metrikleri saklar; ham bar-resolution
  eğrileri tekrar gömmez.
- Bu sözleşmeler hedefli testlerle ve mevcut regresyon paketiyle kilitlenir.

## Durum

## Uygulanan düzeltmeler

### 1. Rol ve sinyal sözleşmesi

- Runtime exit değerlendirmesi `bool(result)` yerine yalnız `result == "exit"`
  kabul ediyor. `"long"` ve `"short"` artık exit olarak yorumlanmıyor.
- Generated-code smoke testi gerçek `role_hint` ile çalışıyor. Entry için yalnız
  `None|long|short`, exit için yalnız `None|exit` geçerli.
- Custom blok metadata'sına `role` yazılıyor. `ComposedStrategySpec.validate` ve
  LLM proposal temizleyicisi, metadata rolüne ters atamayı reddediyor.
- Degraded/fallback spec kimlikleri ayrı izleniyor; robustness geçse bile winner
  havuzuna giremiyor. Terminal `401/402/403`, kredi ve yetki hataları fallback
  üretmeden oturumu `terminal_llm_error` ile bitiriyor.

### 2. Robustness ve yayımlama kapıları

- WFO artık iki koşulu birlikte ister: geçerli pencerelerin en az `%50`'si
  pozitif PnL **ve** mevcutsa dispersion-cezalı OOS Sharpe `>0`. UI paydası da
  aynı `>=3 trade` geçerli pencere kümesini kullanır.
- Multi-symbol etiketi kesin `✗` ise IS/OOS, WFO ve Monte Carlo hiç başlatılmaz;
  sonuç payload'ında aşamalar açıkça `skipped after definitive multi-symbol
  rejection` olarak görünür.
- Sealed holdout katalog yazımının önüne taşındı. Varsayılan yayımlama şartları:
  en az 20 işlem, pozitif PnL, pozitif Sharpe ve buy-and-hold üzerinde pozitif
  excess return. Aynı timeframe holdout'u oturumda yalnız bir kez tüketilebilir.
- Geçemeyen artefakt `winner` değil `finalist_rejected` olayı üretir; katalog ve
  oturum özetleri araştırma sonucunu deploy edilebilir strateji saymaz.

### 3. Backtest gerçekçiliği ve edge ölçümü

- Bilinen `adjusted=false` dış hisse verisi AUTO başlangıcında fail-closed
  reddedilir. Araştırma amaçlı bilinçli override `AGENT_ALLOW_UNADJUSTED=1`.
- Her AUTO spec'i deterministik adverse fill modelini taşır: agresif fill'lerde
  bir tick slippage, `prob_slippage=1`, seed `42`. Ana test, IS/OOS, WFO,
  multi-symbol ve holdout aynı spec'i kullandığı için execution varsayımı aynıdır.
- Her adayda buy-and-hold getirisi ve `excess_pnl_pct` damgalanır. Sıralamanın
  Calmar getirisi, mevcutsa mutlak PnL yerine excess return kullanır. Holdout da
  benchmark'ı geçmeden yayımlanamaz.

### 4. Bütçe, maliyet, tekrar üretilebilirlik ve kayıt boyutu

- Sürekli mod varsayılanları 4 saat ve 250.000 toplam token. Formdan sıfır gelse
  bile backend bu sonlu varsayılanları uygular. Her LLM çağrısından sonra tavan
  yeniden kontrol edilir; sonraki çağrı başlamadan koşu kapanır.
- Maliyet hesabı artık HTTP thread'inin model tahminini değil run brief'inde
  sabitlenen model kimliğini kullanır. Böylece UI ve `token_snapshot` aynı pricing
  modeline bakar; bilinmeyen model iki yerde de `None` kalır.
- Fallback RNG run_id ile thread-local tohumlanır ve `session_start.search_seed`
  alanında kaydedilir.
- `backtest_result.metrics` ve winner metrikleri dahil nested MTM/realized
  eğrileri 400 noktaya seyreltilir. Üst düzey eğriyi seyreltip metrics içindeki
  kopyayı bırakma deliği kapandı.
- Stop iki tur arasındayken pre-increment edilen sayaç artık hayalet tur yazmaz;
  `session_end.total_rounds` gerçekten iş bölümüne giren son turu kullanır.

## Mevcut verinin karantinaya alınması

İki kusurlu koşunun session loglarındaki `spec_id` listesi katalogla birebir
eşleştirildi: 14/14 bulundu. Aktif katalog 850 kayıttan 836 kayda indirildi;
kayıtlar silinmedi:

- tam geri-dönüş yedeği:
  `quarantine/strategy_catalog.before-auto360-quarantine.20260805-142211.json`
- 14 kayıtlık gerekçeli karantina:
  `quarantine/auto360-runs-6432552f-bf598e4f.20260805-142211.json`

Karantina gerekçesi dosyada run kimlikleriyle birlikte taşınır: semantik rol
uyuşmazlığı, WFO kapı bypass'ı ve holdout öncesi katalog yazımı.

## Doğrulama

- Ruff: değişen Python dosyalarında temiz.
- Ana tam paket: **758 passed, 1 skipped, 1 deselected**. Dört test yalnız
  sandbox'ın kullanıcı cache'ine yazmayı engellemesi nedeniyle düştü.
- Bu dört cache-yazma testi normal izinle ayrıca çalıştırıldı: **4 passed**.
- Sandbox worker başlangıç gecikmesiyle iki kez düşen multiprocessing timeout
  testi normal süreç koşulunda ayrıca çalıştırıldı: **1 passed**.
- Toplam doğrulanan: **763 passed, 1 skipped**.
- PM2 `nautilus` restart edildi; yeni süreç online ve `/studio` HTTP `200`.

## Kalan sınırlar

- Bu değişiklikler yanlış-pozitif winner üretimini fail-closed yapar; yeni edge
  bulunduğunu kanıtlamaz. İlk yeni AUTO koşusu adjusted veriyle yapılmalı ve
  promoted winner çıkmazsa bu beklenen, sağlıklı bir sonuçtur.
- Bir tick slippage, bar verisinde deterministik ve tekrar üretilebilir bir alt
  sınırdır; order-book/quote verisi olmadan gerçek market-impact modeli değildir.
- OpenRouter model fiyatı yerel fiyat tablosunda yoksa maliyet değeri uydurulmaz;
  UI ve oturum tutarlı biçimde `None` gösterir. Gerçek fatura için provider usage
  cost alanının ayrıca deftere alınması gerekir.

Durum: **uygulandı, test edildi, servise yüklendi**.

## Son senkronizasyon — 2026-08-05

- Aktif strateji kataloğu yeniden okundu: **836 kayıt**. Karantinaya alınan iki
  koşunun 14 kaydı aktif kataloğa geri dönmemiştir.
- PM2 `nautilus` süreci **online**; süreçte unstable restart görülmedi.
- Canlı `/studio` sağlık isteği HTTP **200** döndürdü.
- `nau-auto-360-canl-review` izleme otomasyonu artık mevcut değildir; inceleme
  kapanmış ve bu sayfa nihai kayıt olarak sabitlenmiştir.
- Bir sonraki kanıt noktası, adjusted veriyle çalıştırılacak yeni AUTO koşusunun
  `session_end`, robustness ve sealed-holdout çıktılarıdır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_kapi_ve_geri_bildirim]]
<!-- BACKLINKS:END -->
