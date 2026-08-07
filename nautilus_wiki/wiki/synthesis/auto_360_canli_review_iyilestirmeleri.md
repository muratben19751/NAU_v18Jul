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
last_updated: 2026-08-06
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

## İkinci canlı inceleme ve sertleştirme — `7a83089b`

Geçici `AGENT_ALLOW_UNADJUSTED=1` araştırma override'ıyla yapılan koşu, ilk
adayda mutlak kârın edge olmadığını açıkça gösterdi: `10c1e975eca4` için PnL
`+1105,27`, fakat buy-and-hold `%18,6955` karşısında excess `%−18,585`, Sharpe
`0,3615`, 636 işlem ve skor `−6,3717` idi. Komisyon `1272`, fill slippage
telemetrisi ise sıfır görünüyordu. Koşunun gerçek sağlayıcı tüketimi 73.667 input
+ 45.261 output = 118.928 token iken aday olaylarında yalnız 46.552 token
görünmesi, retry/custom-block çağrılarının run telemetrisine eksik yansıdığını
kanıtladı.

Veri incelemesi ayrıca QQQ 1H serisinde 2004 sonundan 2011'e kadar büyük bir
takvim boşluğu buldu; 2005–2010 yılları yoktu. `adjusted=false` işaretiyle
birleşince bu veri ancak araştırma amaçlı okunabilir, edge veya yayımlama kanıtı
olamaz.

### Bu turda uygulanan ek sözleşmeler

- Generated custom block doğrulaması artık yalnız bir smoke girdisinin gördüğü
  çıktıya bakmaz. AST'deki tüm literal `return` dalları taranır: entry yalnız
  `long|short|None`, exit yalnız `exit|None`. Koşullu/ölü dalda kalan yanlış
  `long/short` exit üretimi de fail-closed reddedilir; dinamik signal dönüşleri
  denetlenebilir olmadığı için üretilen bloklarda kabul edilmez. Rol kuralı promptta her
  dal için açıkça tekrar edilir; block-edit yolu mevcut metadata rolünü korur.
- Entry/exit çifti tek registry transaction'ında yazılır. İki üretim ve spec
  doğrulaması tamamlanmadan hiçbir blok kaydedilmez; backtest başlamadan gelen
  stop sinyali yalnız o koşunun henüz çalışmamış bloklarını siler.
- Dış kataloglarda 14 günü aşan aktif-gün boşluğu AUTO başlamadan reddedilir.
  Bilinen unadjusted veri yalnız iki anahtarlı
  `AGENT_ALLOW_UNADJUSTED=1 + AGENT_RESEARCH_MODE=1` override ile açılsa bile
  koşu `research_only` taşır ve
  sealed holdout geçse dahi `strategy_catalog` yayımlaması kesinlikle yasaktır.
- Robustness öncesi ucuz alpha kapısı en az 20 işlem yanında pozitif mutlak PnL,
  pozitif per-trade Sharpe ve pozitif benchmark excess ister. Böylece
  `10c1e975eca4` gibi mutlak kârlı fakat benchmark altında kalan aday, WFO/MC
  maliyeti üretmez. Kesin IS/OOS `✗` sonucu da WFO ve Monte Carlo'yu kısa devre
  eder.
- Her gerçek provider cevabı — kesilip tekrar denenen cevaplar dahil — model,
  amaç, input/output/cache token, `max_tokens`, süre ve durumuyla `llm_usage`
  olayı üretir. Run token sayacı bu tek choke point'ten beslenir; yüksek seviye
  çağrıların ikinci kez sayılması kaldırıldı. 250k tavan provider dönüşünden
  hemen sonra tekrar kontrol edilir.
- LLM çağrıları varsayılan 120 saniyelik istek tavanı taşır. OpenRouter 429
  backoff'u 250 ms aralıklarla stop kontrol eder; Claude CLI `Popen` ile izlenir
  ve stop/timeout'ta child process öldürülür. Böylece Stop düğmesi beş dakikalık
  senkron CLI çağrısını beklemek zorunda değildir.
- Equity intraday annualization sabit 6,5 saat yerine gerçek katalogdaki medyan
  bar/gün sayısını kullanır; QQQ 1H için 1638 yerine `7×252=1764`. Robustness
  `pnl_pct` alanı ana backtest ile aynı fraction sözleşmesine geçirildi.
- Slippage telemetrisi `reported`, `estimated`, `fill_count` ve
  `model_active` alanlarına ayrıldı. Fill raporu sıfır/eksik dönerken model
  açıksa quantity × price-increment üzerinden bir-tick denetim tahmini yazılır;
  sıfır artık sessizce execution-gerçekçiliği kanıtı sayılmaz.
- Tam robustness nesnesi JSONL satırına gömülmez. Gzip JSON artifact olarak
  `{run_id}_artifacts/` altına yazılır; JSONL yalnız karar özeti, bağıl path,
  byte boyutu ve SHA-256 digest taşır. Stop sonrası yanıltıcı “yeni tur
  başlıyor” mesajı da kaldırıldı.

### Doğrulama (ikinci dalga)

- Ruff: değişen dosyaların tamamında temiz.
- İzole tam paket: **769 passed, 3 skipped, 2 deselected**.
- Gerçek kullanıcı custom-block kataloğunu gerektirdiği için izole paketten
  ayrılan iki test ayrıca gerçek katalog üzerinde çalıştırıldı: **2 passed**.
- Tam paket tabanı: **771 passed, 3 skipped**. Son iki güvenlik guard'ı
  (dinamik role-return reddi ve eski tek-anahtarlı override'ın etkisizliği)
  ayrıca hedefli paketlerde geçti: **28/28** ve **57/57**.

### Mimari sınır

Backtest ve robustness zaten öldürülebilir child process'lerde, LLM CLI da bu
turda öldürülebilir child'a alındı; buna karşın AUTO orkestrasyon durumu hâlâ web
sürecinin belleğinde tutuluyor. JSONL + artifact kayıtları restart sonrası tam
adli iz bırakır, fakat yarım koşuyu otomatik devam ettiren harici/durable queue
henüz yoktur. Bu, veri/edge doğruluğu düzeltmelerinden ayrı bir servisleşme
migrasyonudur; süreç restart'ı aktif koşuyu devam ettirmez.

## Adjusted veri sonrasi canli inceleme - `bbbdd6e3`

Massive adjusted QQQ.NASDAQ 1-HOUR kataloguyla yapilan `bbbdd6e3` kosusu veri
kapilarinin duzeldigini, fakat arama ekonomisinde iki yeni acik kaldigini
gosterdi. Katalog 40.068 bar, 2003-09-10 -> 2026-07-01 araligi, `adjusted=true`,
`research_only=false` ve aktif-gun gap'i olmadan yuklendi. Eski unadjusted/gap
uyarilari tekrar etmedi.

Kosunun kanit ozeti:

- 45 dk 38 sn, 10 backtest, 35 gercek LLM denemesi.
- LLM bekleme suresi 2.453,53 sn (toplam surenin yaklasik %89,6'si), backtest
  suresi 280,56 sn.
- 245.347/250.000 token (%98,1); uc yanit `max_tokens` nedeniyle kesildi.
- En uzun provider cagrisi 707,36 sn oldu. Stop sirasindaki 94,2 sn'lik cagri
  cevap donene kadar iptal edilemedi ve 15.921 token daha tuketti.
- Session JSONL 209.418 byte'ta kaldi; nested curve/artifact inceltmesi calisti.
- Winner, robustness, sealed holdout veya katalog yazimi olmadi. Bu sonuc dogru:
  en iyi mutlak aday +1.831,96 dolar (+%18,32) uretirken ayni donemde benchmark
  yaklasik +%1.869,55 idi; excess kesin bicimde negatiftir.
- `pnl_pct` ve `benchmark_return_pct` icin birim uyusmazligi yoktur: ikisi de
  fraction sozlesmesindedir. Gercek karsilastirma kusuru, LLM'in %5/%10/fixed/
  ATR maruziyet secmesine karsilik benchmark'in %100 buy-and-hold olmasiydi.

### Bu inceleme sonrasinda uygulanan ek sertlestirme

- Her gercek provider denemesinden **once** prompt icin muhafazakar input token
  ust siniri ve `max_tokens` output rezervi hesaplanir. Kalan butce bu rezervi
  karsilamiyorsa cagri hic gonderilmez; `llm_budget_rejected` olayi yazilir.
- OpenRouter AUTO cagrisi ayri bir OS process'inde calisir. Varsayilan 120 sn sert
  deadline veya Stop sinyali process'i oldurur. SDK'nin gizli retry katmani AUTO
  icin varsayilan olarak kapatildi (`max_retries=0`); kontrollu 429 backoff'u stop
  kontrol etmeye devam eder.
- Stop ve butce exception'lari strateji fallback'i gibi yutulamaz. Kismi tur
  `x/y iterations completed` olarak kapanir; Stop sonrasinda ranking veya
  robustness baslatilmaz. UI artik "tur sonunda" degil aktif adimin iptal
  edilecegini/bitirilecegini soyler.
- OpenRouter response usage icindeki gercek `cost` run defterine tasinir. UI ve
  `token_snapshot`, provider cost varsa fiyat tablosu tahmini yerine onu kullanir
  ve `cost_source=provider` kaydeder.
- External-equity AUTO adaylarinin sinyal kalitesi ayni sinavda olculur: LLM'in
  sectigi `fixed`, `atr_target`, `vol_target` veya dusuk yuzde yerine tum adaylar
  varsayilan %95 `percent_equity` maruziyetine normalize edilir. Degisim
  `exposure_normalized` olayi ile denetlenebilir; deployment risk sizing'i
  promotion sonrasina kalir.
- Exact aday fingerprint'i daha once kosulan spec'i yeniden calistirmaz. Sifir
  trade ureten blok/rol ailesi parametreleri degistirilerek tekrar denenmez;
  token harcamayan deterministik builtin fallback ile degistirilir ve
  `candidate_deduplicated` yazilir.
- Winnerless continuous circuit breaker varsayilani 25 turdan 3 tura indirildi.
  Ayni edge'siz aile 4 saat/250k butcenin geri kalanini tuketmez.
- FlatFiles one-byte abonelik preflight'i test S3 sozlesmesine eklendi; tum
  worker'lari isitmadan 2 sn timeout testi baslatan Windows timing flake'i tum
  pool worker'larini onceden isitacak bicimde deterministiklestirildi.

### Dogrulama (ucuncu dalga)

- Ruff: `agent.py`, `web/routes/agent_backtest.py` ve yeni hedefli testlerde temiz.
- LLM admission/cancel, provider cost, killable OpenRouter yolu, exposure
  normalization ve candidate fingerprint testleri eklendi.
- Hedefli AUTO/form paketi: **64 passed**.
- Tam regresyon paketi: **801 passed, 1 skipped**.

### Kalan mimari sinir

AUTO orkestrasyon state'i hala web process bellegindedir. Bu tur route/worker
dogruluk kontratini sertlestirdi ve provider islemini oldurulebilir hale getirdi;
PM2 restart sonrasinda yarim kosuyu otomatik devam ettirecek durable queue/checkpoint
migrasyonu ayri bir servis degisikligi olarak kalir. JSONL ve gzip artifact adli
izi korur, fakat calisan state'i yeniden kurmaz.

## Canli dagitim senkronizasyonu - 2026-08-06

- `nautilus` PM2 sureci PID `54468` ile **online**; son kontrollu restart'tan
  sonra 8 saat kesintisiz calisma suresi goruldu.
- Strategy Studio saglik kontrolu `http://127.0.0.1:8111/studio` icin HTTP
  **200** dondu; yanit boyutu 1.765.194 byte idi.
- Gecici arastirma kapilari canli surecte kapali: `AGENT_ALLOW_UNADJUSTED=0` ve
  `AGENT_RESEARCH_MODE=0`. Bilinen ayarlanmamis veri normal AUTO kosusuna veya
  katalog yayimina giremez.
- Son dogrulama tabani **801 passed, 1 skipped**; Ruff ve `git diff --check`
  temizdir.
- `bbbdd6e3` icin winner/robustness/holdout/katalog yazimi olmamasi bir altyapi
  hatasi degil, negatif benchmark excess nedeniyle beklenen fail-closed
  sonucudur. Bir sonraki canli kosuda temel kabul kaniti; sert 120 sn provider
  deadline, cagri-oncesi token rezervi, aday dedup ve esit maruziyet olaylarinin
  session JSONL'de gorulmesidir.

Durum: **kod, test, wiki ve canli servis senkronize**.

## Fable 5 canli kosu sertlestirmesi - 2026-08-06

`d4b86c48` Fable 5 kosusu, veri ve alpha kapilarinin dogru calistigini; ancak
custom-block uretimi, maliyet atfi ve session muhasebesinde kalan aciklari
gosterdi:

- 8 aday ve 2 tamamlanmis turda 23 Fable cagrisi, 230.676/250.000 token ve
  yaklasik 492 sn LLM suresi kaydedildi.
- Dort custom strateji fallback'e dustu: uc exit blogu `long` dondurdu, bir blok
  bilinmeyen block type uretti. Sistem prompt'u rol kontratini yeterince sert
  kilitlemiyor ve ayni semantik hata icin pahali retry yapiliyordu.
- Kosu ucuncu tura yalniz baslamis olmasina ragmen `session_end` 3/3 raporladi;
  tamamlanan tur ile baslatilan tur birbirine karisiyordu.
- CLI'nin raporladigi gercek maliyet tasinmadigi icin Fable maliyeti fiyat
  tablosu tahmini olarak gorunuyordu.
- `pnl_pct`, benchmark ve excess alanlari fraction idi; `_pct` adlari ve yuzde
  isaretsiz UI gosterimi yanlis yorumlanmaya acikti. Adaylarin benchmark altinda
  kalmasi gercek birim hatasi degildi; bu nedenle robustness/winner olmamasi
  dogru fail-closed sonucuydu.

### Uygulanan duzeltmeler

- Entry/exit rol kilidi system prompt seviyesine tasindi. Exit blogundaki dogrudan
  literal `long`/`short` donusleri AST ile yerel olarak `exit`e normalize edilir,
  yeniden statik/runtime dogrulamadan gecirilir ve gereksiz ikinci LLM cagrisi
  yapilmaz.
- Custom-block deneme sayisi en fazla ikiyle sinirlandi; blok basina token tavani
  eklendi. Tavan doldugunda paid retry durur ve mevcut fail-closed fallback yolu
  kullanilir.
- Custom blok ciftinin kaydi transaction haline getirildi. Bloklar staged registry
  uzerinden register edilmeden StrategySpec dogrulanmaz; herhangi bir hata hem
  dosyalari hem registry'yi onceki duruma geri alir.
- Metrik sozlesmesine `benchmark_return_fraction` ve `excess_return_fraction`
  canonical alanlari eklendi. Legacy alanlar geriye uyumluluk icin korunurken UI
  degerleri gercek yuzdeye cevirerek `Benchmark %` ve `Excess %` gosterir.
- CLI `total_cost_usd` degeri artik run muhasebesine tasinir. UI ve session ozeti
  maliyeti `provider reported` veya `estimated` olarak acikca etiketler.
- `started_round`, `completed_rounds` ve `total_rounds` ayrildi. `session_end` ile
  session listesi yalniz tamamlanmis tur sayisini raporlar; aday cikmayan tur da
  makine-okunur `no_eligible_candidate` sonucuyla kapanir.

### Dogrulama (dorduncu dalga)

- AUTO/agent hedefli regresyon paketi: **85 passed**.
- Tam paket: **804 passed, 1 skipped**; kalan dort test sandbox'in gercek Nautilus
  cache dizinine yazma kisitindan etkilenmisti. Ayni dort test gerekli izinde
  ayrica calistirildi: **4 passed**. Etkin toplam: **808 passed, 1 skipped**.
- Ruff, Python compile ve `git diff --check`: temiz.
- Duzeltmelerden sonra `nautilus` kontrollu olarak yeniden baslatildi: PM2
  `online`, PID `57060`, unstable restart `0`; `/studio` HTTP **200** ve yanit
  boyutu 1.765.204 byte. Restart logundaki `D:\NAU_ev` mesaji daha once belgelenen
  kullanilmayan harici-varsayilan yol uyarisi; Massive ingest'in yerel
  `equity_catalog` koku ayrica otomatik eklenir.

Kalan mimari sinir degismedi: AUTO orkestrasyonu web process bellegindedir;
PM2 restart yarim kosuyu otomatik surdurmez. Durable queue/checkpoint migrasyonu
ayri bir servislesme calismasidir.

## Son 360 derece AUTO sertlestirmesi - 2026-08-06

`8f191a64` kosusunun kaniti, sistemin negatif alpha adaylarini robustness ve
katalog asamalarina gecirmedigini gostermistir: 10 backtest / 27 LLM cagrisi
sonunda winner, sealed holdout ve katalog yazimi yoktur. Ancak ayni entry/exit
bloklarinin sirasi degistirilerek tekrar denenebilmesi, uzun legacy oturum
corpusunun liste ekraninda disk baskisi yaratmasi ve denetim gunlugu yazma
hatalarinin sessiz kalmasi kalan iyilestirme noktalarini ortaya cikarmistir.

### Uygulananlar

- Aday fingerprint'i entry ve exit bloklarini rol icinde kanonik siralar. OR/AND
  mantiginda blok sirasi degisse bile ayni strateji bir kez calisir.
- Multi-symbol ve WFO artik yalniz mutlak PnL'ye bakmaz: ayni OOS slice icin
  benchmark/excess hesaplanir; pozitif alpha yoksa pahali devam asamasi reddedilir.
- Custom block LLM cagrilari provider'a gitmeden once blok basina token rezervi
  ile admission kontrolunden gecer; truncated ilk yanitin tokenlari da muhasebeye
  dahildir. Provider bildirdigi USD maliyeti token defterinde kalici saklanir.
- Legacy `agnt_e_*` / `agnt_x_*` bloklari rol metadatasi ile onarilir; prefix ile
  metadata celisirse fail-closed olur. Ters rol runtime sinyali `None` sayilir.
  Kosu sonunda winner bagimliliklari korunur, diger gecici custom bloklar atomik
  olarak temizlenir.
- Dört saatlik duvar-saati tavani LLM callback'inde de denetlenir; yeni provider
  cagrisi veya robustness gecisi sonrasina sarkmaz.
- Session listesi tam gecmis corpusunu sinirsiz paralel taramak yerine varsayilan
  sekiz eszamanli ozet okumayla sinirlanir (`NAUTILUS_SESSION_SUMMARY_CONCURRENCY`).
  Aktif JSONL'nin buyuk curve yukleri artifact'ta kalmaya devam eder.
- JSONL yazimi artik sessizce yutulmaz. Arastirma devam eder fakat canli UI
  `AUDIT DEGRADED` uyarisi verir; `session_end` yazisi flush+fsync ile kalici
  tamamlama siniridir.

### Dogrulama

- Python derleme ve `git diff --check`: temiz.
- AUTO/robustness/token/registry/session hedefli regresyon: **97 passed**.
- Pytest cache dizinine ait bir Windows izin uyarisi haric test hatasi yoktur;
  test gecici dizini proje icinde izole edilerek calistirildi.

## Sonraki AUTO dongusu: modelden bagimsiz canli inceleme sozlesmesi

Bu bolum her yeni run'dan once referanstir. Secilen LLM (Fable, Claude, OpenAI
veya baska bir saglayici) kalite karsilastirmasinin girdisidir; kabul kapilarini
ve maliyet muhasebesini degistirmez.

- Run basinda istenen model, gercek cagrida kullanilan model, fallback modeli ve
  provider maliyeti ayri ayri kaydedilir. Fallback veya model degisimi gizli
  basari sayilmaz.
- Her turda token/cost, LLM-backtest-robustness sureleri, PM2/worker sagligi ve
  session JSONL/artifact boyutu toplanir. 250k token ve 4 saat sinirinda yeni
  provider cagrisi admission ile reddedilmelidir.
- Kod/mimari incelemesi custom block rol kontrati, staged register-cleanup,
  audit-log sagligi, route-worker bellek durumu ve winner'in holdout sonrasinda
  kataloglanmasini kontrol eder.
- Quant incelemesi adjusted/gap/split, lookahead, annualization, komisyon ve
  slippage, exposure, benchmark/excess, az islem, dedup, WFO/MC ve sealed
  holdout kapilarini ayni kurallarla denetler.
- Her kosu sonunda bu sayfaya `run_id`, gercek model/fallback, tur sayisi,
  token/maliyet/sure, aday ve robustness sonucu, winner/holdout/katalog karari,
  kritik hata ve yapilan degisiklik eklenir. Bu kayit sonraki kosunun baslangic
  kontrol listesi olur; onceki fail-closed korumalari gevsetilmez.

## Canli kosu kaydi: `c0efcbe4` - 2026-08-06

- Veri yalniz `QQQ.NASDAQ` 1-HOUR'un 2011-03-23 sonrasi dogrulanmis kesintisiz
  tail segmentinden alindi; 2004-2011 fiziksel boslugu birlestirilmedi.
- Gercek model `moonshotai/kimi-k3` (OpenRouter) oldu; fallback kaydi yoktur.
  Kosu 3 turun sonunda `winless_limit` ile kapandi: 12 aday, winner yok,
  sealed holdout ve strategy catalog yazimi yoktur.
- Adaylarin nominal PnL'leri pozitif gorunse bile hepsi buy-and-hold benchmark'a
  gore negatif excess uretti. Son aday 1.035 islemde +%109,40 net PnL'ye karsin
  -%1136,74 excess urettigi icin alpha kapisinda reddedildi; WFO/MC'ye
  gecmedi.
- Toplam provider kullanimi 121.948 input + 48.287 output token, $0,987662
  provider maliyetidir; 250k token tavani asilmaz.
- Iki OpenRouter custom-block cagrisi 120 sn hard timeout'a dustu. Run retry ile
  toparlansa da bu gecikme ve belirsiz retry maliyeti sonraki kosular icin
  iyilestirme gerektirdigini kanitladi.

### Kosu sonrasi duzeltme

- Custom-block cagrilari varsayilan olarak en fazla 1.800 output token ve 75 sn
  timeout ile sinirlandi (`AGENT_CUSTOM_BLOCK_MAX_TOKENS`,
  `AGENT_CUSTOM_BLOCK_TIMEOUT`). Hard timeout ikinci pahali denemeyi baslatmaz;
  AUTO'nun mevcut role-safe builtin fallback yoluna doner.
- OpenRouter'a thread pin'i varken process varsayilan istemcisi Claude CLI olsa
  bile output-cap telemetrisi artik dogru olarak `provider_enforced` yazar;
  `advisory_cli` yalniz gercek CLI cagrilari icindir.
- Bu duzeltme `tests/test_auto_360_fixes.py` paketiyle 58 test ve Ruff ile
  dogrulandi. Nautilus yeniden baslatildi; `/studio` HTTP 200.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_kapi_ve_geri_bildirim]]
<!-- BACKLINKS:END -->
