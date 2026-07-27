---
title: Strategy Studio (görsel strateji kurucu)
type: synthesis
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - sources/02_architecture_docs.md
last_updated: 2026-07-27
summary: /studio/{id} altındaki görsel strateji kurucu; sürümlü şema → derleyici → to_nautilus → composer spec → run_composed_backtest zinciri, çeviremediğini sessizce atmak yerine gerekçesiyle reddeder; sweep pencereli walk-forward ve deflate edilmiş DSR ile skorlanır. Nav'da ve sayfa markasında "Strategy Builder" adını taşır (2026-07-27).
key_concepts:
  - strategy_and_actor
  - backtesting_guide
  - portfolio
  - order_flow_pipeline
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/backtesting_guide.md
  - wiki/entities/portfolio.md
---

# Strategy Studio (görsel strateji kurucu)

`nautilus_web_app`'e 2026-07-25'te birleştirilen ikinci strateji yüzeyi.
Kullanıcı kural bloklarını HTMX ile düzenler; sonuç sürümlü bir şemada saklanır,
derlenir ve mevcut NautilusTrader koşucusuna indirgenir.

Adı benzese de `/studio` ile **aynı şey değildir**: `/studio` Composer+Backtest
sayfasıdır ([[webapp_module_map]], `web/routes/studio.py`), kurucu ise
`/studio/{strategy_id}` altında yaşar. İkisi birbirini gölgelemez (farklı yol
şekilleri), ama ad ortaklığı akılda tutulmalı.

Bu ad çakışması artık ana navigasyonda da görünür (2026-07-26): `base.html`'de
zaten `/studio`'ya giden bir "Strategy Studio" linki vardı; kurucuya
(`/studio/wt-funding-v3`) giden ikinci bir link eklendiğinde aynı etiketi
kullanmak iki farklı sayfaya aynı isimle gitmek anlamına gelirdi. Çözüm: yeni
link ayrı bir etiketle eklendi — **"Strategy Builder"** → `/studio/wt-funding-v3`
— var olan "Strategy Studio" → `/studio` linkine dokunulmadı.

Nav ayrışmıştı ama sayfanın kendisi hâlâ "StrategyStudio" markasını taşıyordu:
kullanıcı soldaki "Strategy Studio" linkine tıklayıp gelince üstte gene aynı adı
görüyordu. 2026-07-27'de kurucunun yüzeydeki adı da nav etiketiyle hizalandı —
logo `Strategy<span>Builder</span>`, `<title>` "Strategy Builder — {ad}"
(`web/templates/studio/page.html`). Kod tarafı bilerek değişmedi: modül adı
`web/routes/strategy_studio.py`, rota öneki `/studio/{id}` ve `strategy_studio/`
paketi tarihsel adlarını korur — yeniden adlandırma yalnızca kullanıcı yüzeyinde.

## Kabuk entegrasyonu (2026-07-27)

Kurucu, birleşmeden bu yana **kendi başına duran bir HTML belgesiydi**: kendi
`<html>`/`<head>`'i, kendi htmx kopyası, kendi `body{height:100vh}` düzeni. Ana
uygulamanın sol navigasyonu ve topbar'ı bu sayfada yoktu — linke tıklayan
kullanıcı kabuğun dışına düşüyordu. Artık diğer sayfalar gibi `base.html`'i
extend ediyor.

İşin zor kısmı düzen değil **CSS izolasyonuydu**. `studio.css` global yazılmıştı:

| çakışan | etkisi (kapsanmasaydı) |
|---|---|
| `:root{--panel,--panel-2,…}` | app.css aynı adlı değişkenleri kullanıyor → tüm kabuk yeniden renklenirdi |
| `body{height:100vh;overflow:hidden}` | `.shell` grid düzenini bozardı |
| `header{…}` / `footer{…}` | eleman seçicileri base.html'in `.topbar`'ını vururdu |
| `.btn`, `.metric`, `.tab`, `.chip`, `.modal`, `.seg` | app.css'te aynı adlar var |

Çözüm: `studio.css`'in **tüm kuralları `.studio-embed` altına kapsandı**
(`:root`/`body` → `.studio-embed`, geri kalan her seçiciye önek). Böylece
özgüllük de doğru tarafa çalışıyor: `.studio-embed .btn` (0,2,0) app.css'in
`.btn`'ini (0,1,0) yener, tersi olmaz. Kabuk tarafında tek eklenti
`body.page-builder` altında `.content`'in 24px padding'ini ve büyümesini
kaldırmak — kaydırma sayfada değil, kurucunun kendi panellerinde kalsın diye.

İki incelik: (1) htmx artık yalnızca `base.html`'den yükleniyor, sayfanın kendi
cdnjs kopyası kaldırıldı — iki kopya çift olay dinleyicisi demekti. (2)
`studio.css` `rem` ile ölçüyor ve eski kök 15px'ti; `rem` ebeveynden değil
kökten çözüldüğü için kapsama bunu taşımaz, bu yüzden sayfa-yerel bir
`<style>:root{font-size:15px}</style>` eklendi — app.css'te hiç `rem`
kullanılmadığı için kabuk bundan etkilenmiyor.

Sayfa içindeki "StrategyBuilder" logosu da kaldırıldı: topbar zaten sayfa adını
yazıyor.

## Katmanlar

```
şema (pydantic, sürümlü)       strategy_studio/schema.py
   ↓ mutations.py              insan ve AI düzenlemeleri AYNI yoldan geçer
derleyici                      strategy_studio/compiler.py → CompiledStrategy
   ↓ to_nautilus()             strategy_studio/backtest.py
composer ComposedStrategySpec  ← mevcut blok kataloğu
   ↓ run_composed_backtest()   backtest.py → BacktestEngine
BacktestMetrics                → sparkline + fold tablosu + deploy kapısı
```

Sweep aynı zincire pencereli girer: `optimizer.py` geometriyi kurar, adaptör
`run(compiled, window=…)` ile örneklemin o kesitini koşar.

`CompiledStrategy` nötr bir ara temsildir: şema/UI/AI tarafı motor
değişikliğinden etkilenmez, motor yüzeyi tek dosyada (`backtest.py`) toplanır.

## Sessiz atlama yasağı

Bu zincirin en önemli tasarım kuralı: **çevrilemeyen hiçbir şey sessizce
düşürülmez.** Ekrandakinden farklı bir stratejinin metriklerini döndürmek, hata
vermekten daha kötüdür. `to_nautilus` tüm gerekçeleri birden toplayıp
`UnsupportedStrategy` fırlatır; kullanıcı koşunun neden başlamadığını tek
seferde görür.

Reddedilenler: rejim dalı · ranked allocation · composer bloğu olmayan
indikatör · motor karşılığı olmayan operatör (`ADX < x`) · enstrümanınkinden
farklı **timeframe**'e sabitlenmiş kural (koşu başına tek bar beslemesi) ·
`risk.max_concurrent > 1` (composer stratejisi tek pozisyon tutar) ·
`risk.time_stop_bars` (zaman bazlı çıkış yok) · `entry match='any'` + filtre
birleşimi (spec'te tüm giriş blokları için tek `entry_logic` var).

## İndikatör köprüsü

Şema indikatörleri `indicators.py`'deki gerçek fonksiyonlara tek tip bir
sözleşmeyle bağlanır: `impl(bars, **schema_params)`. İnce adaptörler şema
parametre adlarını (`len`, `n1`/`n2`) fonksiyonların kendi argümanlarına
(`period`, `channel_len`/`avg_len`) çevirir — şema kelime dağarcığı, fonksiyon
yeniden adlandırılsa bile sabit kalır. 15 kayıttan 8'i bağlı; kalanların
(`macd`, `funding_z`, `oi_z`, `cvd_divergence`, `volume_profile`, `time_stop`,
`session_filter`) uygulaması yok, `impl=None` bırakıldı.

## İki motor anahtarı

| Env | Neyi seçer | Varsayılan |
|---|---|---|
| `STUDIO_BACKTEST=nautilus` | Run butonu — tıklama başına tek koşu | stub |
| `STUDIO_BACKTEST_OPT=nautilus` | optimizer sweep + AI döngüsü denemeleri | stub |

Ayrı olmalarının nedeni maliyet: penceresiz her `run()` çağrısı
`1 + walkforward.folds` motor koşusudur ve optimizer 400 kombinasyona kadar
örnekler. Tek-koşu anahtarını çevirmek bunu tetiklememelidir. Gerçek motor
sweep için seçiliyse `POST /optimize` ayrıca `STUDIO_OPT_MAX_ENGINE_RUNS`
(varsayılan 200) üstünde baştan 422 döner — üst sınır olarak hiçbir adayın
elenmediği en kötü durum (`min(sweep, 400) × (1 + folds)`) kullanılır.

Stub adaptör silinmedi ve **her yerde varsayılandır**: piyasa verisi olmadan tüm
UI döngüsü çalışır, test takımı çevrimdışı kalır (`tests/studio`, 196 geçti +
1 atlandı — 2026-07-26'da tekrar ölçüldü — env flag'siz yeşil).

## Metriklerin kaynağı (ve neden hepsi motordan alınmıyor)

- **Equity eğrisi** — bar seviyeli MTM serisi; saklanırken 260 noktaya
  indirgenir (sparkline 260 px, ham eğri koşu başına ~39 KB JSON'du).
  İstatistikler tam eğriden hesaplanır.
- **Drawdown** — motorun MTM figürü (realized eğrinin göremediği dipleri yakalar).
- **Sharpe** — bilinçli olarak `sharpe_per_trade`. Bar-frekanslı Sharpe her düz
  barı sıfır getiri sayar, piyasada az duran stratejinin paydasını şişirir (aynı
  koşu: 6.02 bar-frekanslı, 0.51 işlem bazlı). Studio strateji **sıralar**
  (optimizer objective, deploy kapısı) — bu yanlılık az işlem yapanı sistematik
  kayırırdı. `/backtest` bar-frekanslıyı gösterir; fark bilinçlidir.
- **`dsr`** — tek koşuda PSR'dır (tek denemeli DSR); deploy kapısı bu yüzden
  iyimser tarafa hata yapar. Optimizer sonuçlarındaki `dsr` ise **gerçekten
  deflate edilmiştir** — aşağıya bakınız. İkisi aynı ölçekte değildir ve
  karşılaştırılmamalıdır; panel deflate edileni `DSR*` diye ayırır.
- **Fold'lar** — ardışık OOS dilimleri; `walkforward.embargo_bars` kadar baş
  kısmı atılır. Her enstrüman aynı şekilde dilimlenip başlıkla aynı biçimde
  harmanlanır. Bu **tek koşunun kendi örneklemi üzerindeki purged k-fold
  sağlamlık tablosudur** — optimizer'ın IS/OOS bölmesiyle karıştırılmamalı;
  `in_sample_months`/`oos_months` buraya değil, optimizer'a aittir.

Bu serinin pozisyon açıkken çökmesine yol açan `Portfolio.equity()` tuzağı ve
ölçülen etkisi [[portfolio]] sayfasındadır.

Çok enstrümanlı stratejide metrikler eşit ağırlıklı equity harmanıdır: her
sleeve kendi sermayesiyle koşar, rebalance yoktur — ortak sermayeli portföy
koşusu **değildir**.

## Walk-forward optimizer

Stub, tam örneklemde grid koşup **aynı örneklemin** metriğine göre sıralıyordu:
seçim de değerlendirme de aynı veride, yani sıralamanın kendisi in-sample'dı.
Yerine iki aşama; ikisi de adaptörün kendi örnekleminden kestiği pencerelerde:

1. **Ankrajlı in-sample eleme** — baştaki `in_sample_months` payında tek koşu.
   `min_trades` (5) altı ya da `-inf` objektif ⇒ aday fold'lara hiç girmez.
   Pahalı aşamayı küçük tutan ucuz filtre.
2. **Purged walk-forward fold'ları** — ankrajdan sonra uç uca dizilmiş `folds`
   adet OOS penceresi, her birinin **önünde** `embargo_bars` purge (sınırı geçen
   pozisyon ya da indikatör durumu önceki pencereyi sızdırmasın).

Purge/embargo gerekçesi ve genel walk-forward çerçevesi [[backtesting_guide]]
sayfasındadır; buradaki fark, bölmenin kullanıcının şemaya yazdığı alanlardan
türetilmesi ve sonucun tek bir uygulanabilir parametre setine indirgenmesi.

Sıralama anahtarı fold objektiflerinin `mean − 0.5·std`'sidir — host
[[webapp_module_map|wfo_optimizer]]'ın `penalized_score` geleneği: tek fold'da
parlayıp gerisinde çöken aday, istikrarlıya kaybeder. `dsr`/`sharpe` fold'ları
işlem sayısıyla sönümlenir (`n/(n+20)`), `max_dd` **sönümlenmez** — negatif bir
sayıyı 0'a çekmek ince geçmişli adayı ödüllendirirdi. Aday, fold'larının
%60'ında geçerli olmak zorundadır (tek ölü fold sağlam adayı düşürmesin).

**Pencere kesir olarak verilir.** `run(compiled, window=Window(start, end,
embargo_bars))` — örneklemin sahibi adaptördür (kaç gün, hangi timeframe), kesri
satıra çevirebilecek tek yer orası; optimizer yalnız geometriyi kurar. Pencereli
koşu kendi fold tablosunu üretmez, çünkü fold'layan zaten çağırandır: aday
başına maliyet `1 + folds` yerine 1 eleme + `folds` fold'da kalır.

**Aylar takvim değil orandır.** `in_sample_months`/`oos_months` yüklenen
örneklemi `1 + folds` pencereye böler; 9/3 ve 3 fold → örneklemin yarısı IS,
kalan yarısı üç OOS penceresi. Literal olmalarını istiyorsan
`NautilusBacktestAdapter.lookback_days`'i (varsayılan 180 gün) büyütmen gerekir.
Panel bu yüzden ayın yanına ürettiği payı da yazar ("9 months · %33 of sample";
şema varsayılanı 9/3/6 fold) — yoksa sayı takvim uzunluğu gibi okunur. `scheme` tek değerlidir; ikinci bir şema **yalnızca**
geometride dallanır.

**Deflasyon.** `OptResult.dsr` artık gerçek DSR: dikilmiş OOS getiri serisinin
PSR'ı, benchmark 0 değil `expected_max_sharpe(σ_trial, N)` — N kombinasyonun
şans eseri üreteceği en iyi Sharpe (Bailey & López de Prado). Tek fonksiyon iki
işi görür: `probabilistic_sharpe(returns, sharpe, benchmark)`, `benchmark=0`
tek koşunun PSR'ı olarak kalır. N tüm denenen kombinasyondur (çoklu-test orada
olur), σ ise ancak Sharpe üretebilmiş adaylardan ölçülebilir; tek adaylı bir
sweep'te yayılım yoktur, deflasyon da yoktur.

**Sıralama anahtarı görünür sayı olmak zorundadır**: panel satır başında artık
objektif skorunu gösterir. Aksi halde başlıkta DSR yazıp skora göre sıralardı,
sıra bozuk okunurdu. Hiçbir aday hayatta kalmazsa `NoViableCandidates` —
reddedilme dökümüyle, başarısız koşu olarak; "0 sonuç" ile "optimizer hiç
koşmadı" ayırt edilemez.

Host'un `wfo_optimizer`'ı **yeniden kullanılmadı**: o, `BLOCK_REGISTRY`
sınırlarından türettiği uzayda GA koşar; studio'nun arama uzayı ise
kullanıcının kural ağacına yazdığı min/step/max'tir — GA sessizce ekrandaki
aralıkların dışını optimize ederdi. Paylaşılmaya değer olan gelenekler
paylaşıldı: `penalized_score`, geçerli-fold oranı, işlem sayısı sönümlemesi.

**Eski sweep sonuçları etiketlenir, sıfır olarak basılmaz.** Walk-forward'dan
önce kaydedilmiş bir optimize koşusunda `score`/`folds_valid`/`trials` yok;
`OptResult(**row)` bunları varsayılan sıfırlarla yüklüyor ve yeni panel düzeni
"DSR 0.000 · 0 folds · 0 trials" basıyordu — kullanıcıya eski sonuç değil,
bozuk panel gibi görünüyor (tooltip bile "best Sharpe **0 trials** would
produce by luck alone" diyordu). Tek karar noktası `OptResult.walk_forward`
(`trials > 0`): eski koşular eski düzende kalır (başta DSR + "in-sample"
rozeti), DSR*/fold/trial satırı hiç basılmaz ve panel koşunun walk-forward'dan
önce olduğunu, parametrelerin yine de uygulanabileceğini söyler. Ders:
**rehydrate ≠ render** — eski JSON'un yüklendiğini test etmek, nasıl
göründüğünü test etmek değildir; regresyon testi eski satırı yeni alanları
soyarak üretir.

## İndikatör kütüphanesi paneli: dekoratiften işlevliye (2026-07-26)

Kullanıcı raporu "sol frame yok oluyor" idi; kök neden çökme değil, bilinçli
bir CSS kuralıydı: `@media (max-width:1100px){ .library{display:none} }`.
Pencere 1100px altına inince panel geri getirilemeden kayboluyordu.

İzi sürerken **daha ağır bir bulgu** çıktı: panel zaten hiçbir şeye bağlı
değildi. `studio.js` içinde tek bir referansı yoktu — tıklama yok, sürükleme
yok, arama kutusu hiçbir şey yapmıyordu. Kural ekleme yalnızca her bloğun
kendi içindeki `<select>` + "＋ Add condition" formundan yapılabiliyordu.
`cursor:grab` imleci hiçbir şeyi tutmuyordu; panel salt dekoratif bir listeydi.

**Ders:** görünür bir UI öğesinin varlığı, bağlı olduğunun kanıtı değildir.
Gizleyen CSS kuralı, panelin işlevsizliğini de gizliyordu — kimse kaybını fark
etmediği için kural yıllarca sorgulanmadı. Bir bileşen "responsive" gerekçesiyle
tamamen gizleniyorsa, önce **hâlâ bir işi var mı** diye bakılmalı.

Panel işlevli hale getirildi. Tasarım kararı: **yeni endpoint açılmadı.**
Sürükle-bırak ve tıkla-ekle, hedef bloğun DOM'da zaten duran
`add-rule-form`'unun `<select>`'ini doldurup htmx ile submit eder — yani
"＋ Add condition" ile **tam olarak aynı** sunucu yolundan, aynı doğrulamadan
geçer. Böylece kütüphane kendi doğrulama/CSRF yüzeyini üretmez; sunucu tarafında
hiçbir şey değişmedi (salt `studio.js` + şablonlarda `data-dropzone` + CSS).

İki incelik: (1) regime bloğu tek `.block-body` içinde **üç** kural listesi
tutar (`regime`, `sub_entry`, `sub_exit`) — tek ortak dropzone hangisine
ekleneceği belirsiz olurdu, üçü ayrı işaretlendi. (2) `data-dropzone` htmx
swap'ıyla dönen HTML'de de bulunmak zorunda; yoksa ilk bırakmadan sonra blok
yenilenince sürükleme sessizce ölürdü (regresyon olarak elle doğrulandı).
Hedef bloğun dropdown'ında olmayan bir indikatör 422 üretecek istek atmak
yerine sessizce yok sayılır. Panel artık gizlenmiyor, daralıyor (170px → 150px).

## HTTP semantiği: 404 kaynak, 422 girdi

HTMX yüzeyinde durum kodu bir UX kararıdır — 2xx dışını HTMX swap etmez, yani
yanlış kod kullanıcıya "buton hiçbir şey yapmadı" olarak görünür.

- **404** — kaynak yok. Olmayan bir `rule_id` dört uçta da aynı yanıtı verir:
  `PATCH /rules/{id}`, `DELETE /rules/{id}`, `PATCH /opt/toggle`,
  `PATCH /opt/range`. Bunu tek noktadan sağlayan `RuleNotFound`,
  `MutationError`'ın alt sınıfıdır — mevcut `except MutationError` işleyicileri
  bozulmadan route'lar ayırt edebilir.
- **422** — girdi geçersiz: sınır dışı parametre, sayısal olmayan değer,
  bilinmeyen indikatör, parametresiz sweep, kapıya takılan deploy.
- **422 + okunabilir mesaj** — beklenmedik motor hatası. `_trial_baseline`
  bilinmeyen hataları bilerek yukarı bırakır (sessizce guardrail kapatmamak
  için), ama `route_ai_suggest` bunu yakalar: 500 dönmek HTMX'te hiçbir şeyi
  swap etmez ve hata görünmez olurdu.

## Deployment: koşulabilir artifact + paper runner

Artifact "bir runner'ın tükettiği JSON" diye tanımlanmıştı ama içinde
stratejinin kendisi yoktu — `compiled` alanı yalnızca **sayımlardı**
(`entry_conditions: 3`, `has_regime: false`). Beşinci entegrasyon noktası bu
yüzden, bağlanacak runner'dan önce, elinde koşulabilir bir belge olmadığı için
tıkalıydı. Artık `to_nautilus`'un indirdiği `ComposedStrategySpec` serileşiyor:
backtest yolunun çalıştırdığı nesnenin aynısı. Spec enstrümansız olduğundan
`instruments` ayrı anahtar; runner eşleştirmeyi oradan yapar. `artifact_schema`
sürümlenir (v1 = sayımlar) — runner bilmediği sürümü reddeder, tahmin etmez.

Bunun getirdiği üç dürüstlük düzeltmesi: indirilemeyen strateji **deploy
edilemiyor** (tıklamanın kendisi 422 + gerekçeler, modal da butonu kilitliyor);
`instruments="all"` iş yapıyor (artifact eskiden hep aktif listeyi yazıp
`config`'te "all" diyordu); ranked allocation artifact'e yazılmıyor —
`to_nautilus` onu zaten reddediyor, yazılması destekleniyormuş gibi okunuyordu.

`PaperRunner` (`STUDIO_RUNNER=paper`) artifact'i **sandbox ortamında gerçek bir
`TradingNode`**'a indiriyor: canlı Bybit piyasa verisi, `SandboxExecutionClient`
dolumları, hiçbir yerde kimlik bilgisi yok. Deployment başına bir node, her biri
kendi thread'i ve kendi event loop'unda.

`environment='live'` burada **reddediliyor**, sessizce paper koşulmuyor: bu
uygulamada borsa kimlik bilgisi yok (yalnız LLM anahtarları) ve deploy kapısı
hâlâ tek-koşunun deflate edilmemiş DSR'ını okuyor — iyimser bir kapının üstüne
gerçek emir yolu kurmak yanlış sıra.

### Ölçümle bulunan dört tuzak

Gerçek node'a bağlanana kadar dördü de görünmezdi; her birinin regresyon testi
var:

1. **Node kurulduğu loop'a bağlanır.** Çağıran thread'de kurup başka thread'in
   loop'unda koşturmak "Started when loop is not running" üretiyor; data
   client'ın `_connect` coroutine'i hiç await edilmiyor ve node 60 sn sonra
   `DataEngine.check_connected() == False` ile düşüyor. Node artık
   `set_event_loop`'tan sonra, kendi thread'inde kuruluyor.
2. **`product_types=None` = BYBIT_ALL_PRODUCTS** (spot + linear + inverse +
   option). Bağlantı hiç tamamlanmıyordu; enstrüman id'lerinin kurulduğu tek
   ürüne sabitlendi.
3. **'running' tek seferlik bir iddiadır.** Node sonradan ölünce satır yeşil
   rozetle kalıyordu. Serve thread'i kendi çıkışını bildiriyor (bilerek yapılan
   stop hariç) ve durum raporu teardown'dan **önce** gidiyor — `dispose()`
   bloklarsa haber yutulmasın.
4. **Durmuş bileşen START'ı reddeder** (`InvalidStateTrigger('STOPPED ->
   START')`) — UI'daki Resume düğmesi hiçbir şey yapmıyordu. Doğru geçiş
   RESUME; ayrıca `reset()`'in aksine strateji durumunu koruyor, indikatörler
   ısınmayı baştan yapmıyor.

Node kaydı süreç-içi olduğu için yeniden başlatma sonrası `running` kalan
satırlar açılışta uzlaştırılıyor (`reconcile_orphans` → `failed` + gerekçe).
Arkasında node olmayan yeşil rozet, başarısızlıktan kötüdür: iyi görünür.

## AI guardrail baseline'ı

`evaluate_trial` denemeyi baseline ile karşılaştırır. İki motor anahtarı farklı
ayarlanabildiği için baseline **denemenin koştuğu motorda** ölçülür
(`_trial_baseline`, (motor, tanım) anahtarıyla LRU önbellekli) — aksi halde
guardrail öneriyi değil motor farkını yargılardı. Baseline'ın *var olma* koşulu
değişmedi: tamamlanmış bir koşu gerekir, yani hiç backtest edilmemiş stratejide
guardrail kapalıdır. Deploy kapısı ise bilerek `latest_run`'ı okur — kullanıcının
kendi tetiklediği gerçek koşuyu yargılamalıdır.

## Durum

Beş INTEGRATION POINT'in beşi de bağlı: `registry.py` (indikatörler, 15'te 8),
`backtest.py` (motor), `optimizer.py` (walk-forward), `ai.py` (LLM istemcisi) ve
`deploy.py` + `runner.py` (sandbox TradingNode). Kalan iş entegrasyon değil,
kapsam: canlı emir yolu bilerek açılmadı (bkz. yukarıdaki `live` reddi).

Üç motor anahtarı da opt-in, hepsi aynı gerekçeyle — varsayılan kurulum ne ağa
çıkar ne de pahalı koşar, test takımı çevrimdışı kalır:
`STUDIO_BACKTEST` (Run), `STUDIO_BACKTEST_OPT` (sweep + AI denemeleri),
`STUDIO_RUNNER=paper` (deployment node'u).

Tohumlanan iki demo stratejisi: `wt-funding-v3` (tasarım maketi — rejim dalı +
`funding_z` içerdiği için yalnız stub'da koşar) ve `rsi-adx-btc` (motorda
koşabilen; `rsi_threshold` + `adx_threshold` girişi, `atr_stop` çıkışı, tek
Bybit enstrümanı). Gerçek koşu (BTCUSDT 1h, 180 gün, 4319 bar): 23 işlem,
net +%1.54, Sharpe 0.51, DSR 0.71, Max DD −%2.99, ~2 sn.

Gerçek motorda walk-forward doğrulaması (rsi-adx-btc, 3 kombinasyon, 3 fold):
12 motor koşusu (3 eleme + 3×3 fold), 6.4 sn. Sıralama sönümlemenin beklendiği
gibi 19 işlemli adayı, 3 işlemli `sharpe=48` tuzağının üstüne koydu.

Paper runner doğrulaması (aynı strateji, canlı Bybit verisi): launch → 2.4 sn'de
node ayakta, 1 enstrüman cache'te, strateji RUNNING · pause → durdu · resume →
tekrar çalışıyor · stop → node yıkıldı, thread kalmadı, sahte `failed` olayı yok.

## Bilinen boşluklar

- Tek koşunun `dsr`'ı hâlâ deflate edilmemiş PSR — deploy kapısı orayı okur.
  Deflate edilen yalnız optimizer sonuçlarıdır.
- Deflasyon yayılım ölçmeyi gerektirir; tek aday skorlayan bir sweep'te
  `dsr` sessizce deflate edilmemiş PSR'a düşer.
- `in_sample_months`/`oos_months` takvim uzunluğu değil oran; literal olmaları
  `lookback_days`'i büyütmeye bağlı ve ikisi bugün birbirine bağlı değil.
- Çok enstrümanlı harman ortak sermayeli portföy koşusu değil.
- `OptResult.sharpe` fold Sharpe'larının düz ortalamasıdır; `min_trades`
  eşiğine yakın fold'larda per-trade Sharpe hâlâ büyük sayılar üretebilir
  (sıralamayı bozmaz — sıralama sönümlenmiş skoru kullanır).
- Deployment node'ları süreç-içi: sunucu yeniden başlarsa koşan her deployment
  ölür ve `failed`'a düşer. Kalıcılık için ayrı bir runner süreci gerekir.
- `kill_switch_daily_pct` artifact'te taşınıyor ama node tarafında **henüz
  uygulanmıyor** — günlük PnL'i izleyip deployment'ı duraklatan monitör yazılmadı.
- Paper runner sandbox dolumlarını raporlamıyor: panel node'un durumunu
  gösteriyor, ürettiği işlemleri/PnL'i değil.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_guvenlik_dayaniklilik_duzeltmeleri]]
- [[portfolio]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
