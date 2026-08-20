---
title: Model seçici ve model görünürlüğü
type: synthesis
summary: Hangi LLM'in koştuğu her ekranda çözülmüş adıyla yazılır; OpenRouter listesi openrouter.ai kataloğundan canlı gelir, varsayılanda ücretsiz uçlarla sınırlıdır ve çekim başarısızsa statik yedeğe düşer — asla uydurma ya da sürpriz-faturalı id'ye.
sources:
  - sources/08_hibrit_kosu_olcumleri_2026_08_16.md
  - sources/07_yerel_llm_hibrit_olcumu_2026_08_15.md
  - https://github.com/nautechsystems/nautilus_trader
  - https://openrouter.ai/api/v1/models
key_concepts:
  - strategy_studio
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/tear_sheet_overlay.md
  - wiki/synthesis/kesilme_ve_degrade_gorunurlugu.md
last_updated: 2026-08-20
---

# Model seçici ve model görünürlüğü

2026-08-02. İki şikâyet aynı gün geldi: *"Strategy Studio'da da AUTO'da da model
adını yazsın"* ve *"openrouter.ai'dan gelen LLM'ler yok."* İkisinin de kökü aynı
— arayüz, çalışan sistemin gerçeğini göstermiyordu.

## Teşhis: kod doğru, süreç eski

`agent.selectable_models()` etkileşimli kabuktan çağrıldığında OpenRouter
satırlarını üretiyordu; canlı `/studio` sayfasında ise yalnız dört Claude satırı
vardı. Fark koddan değil süreçten geliyordu: uvicorn, `OPENROUTER_API_KEY`
kullanıcı ortamında tanımlanmadan **önce** başlatılmıştı ve ortamın o anki
kopyasını taşıyordu.

Bunu "özellik yok" hâline getiren şey ise bir tasarım tercihiydi: liste anahtar
yokken **sessizce boşalıyordu**. Sessiz gizleme, "yapılandırma yok", "süreç
göremiyor" ve "çağrı başarısız" durumlarını tek bir görüntüye indirger.

> Teşhisi veren adım testleri koşmak değil, **canlı sayfayı `curl`'lemek** oldu.
> Testler kodu ölçer; env'i süreç başlangıcında donmuş bir sunucuyu ölçmez.

## Canlı katalog kararı

Sabit üçlü (`deepseek/deepseek-chat`, `google/gemini-2.5-flash`,
`openai/gpt-4o-mini`) yerine `https://openrouter.ai/api/v1/models` okunuyor.
Yalnız **metin girip metin çıkaran** modeller alınır (görüntü/ses-only uçlar bu
uygulamanın promptlarını çalıştıramaz). Ölçüm: 341 model.

Üç ayrıntı kararla sabitlendi:

1. **Cache iki yönlü** — başarı 1 saat, **başarısızlık 60 saniye**. İkincisi
   olmadan kapalı/engelli bir uç nokta her sayfa render'ına bir ağ zaman aşımı
   ekler; `selectable_models()` sayfa render'ında çağrılıyor.
2. **Yedek uydurmaz** — çekim başarısızsa liste eski statik üçlüye düşer, boş
   veya tahmini id listesine değil.
3. **`NAUTILUS_OPENROUTER_MODELS` ağa hiç çıkmaz** — hem listeyi sabitlemek hem
   testleri ağdan bağımsız tutmak için.

Yan etki: 341 satırlık düz bir `<select>` kullanılamaz; picker `Claude` ve
`OpenRouter · openrouter.ai (N)` optgroup'larına ayrıldı.

## Ücretsiz filtresi (varsayılan)

337 satırın 17'si ücretsiz. Kullanıcı listeyi bunlarla sınırlamak isteyince
filtre **fiyat alanından** kuruldu, id desenine bakılarak değil: `pricing.prompt`
ve `pricing.completion` ikisi de `0` ise uç ücretsizdir. `:free` son eki iyi bir
sezgidir ama tam değildir — `openrouter/free` (Free Models Router) o eki taşımaz
ama ücretsizdir; tersi de mümkün olduğu için desen eşleme fatura riski taşır.

Üç karar bu filtreyi taşıyor:

1. **Bayrak cache'e girer, filtre render'da uygulanır** — katalog artık
   `(id, ad, ücretsiz mi)` tutar. `NAUTILUS_OPENROUTER_FREE_ONLY` çevrildiğinde
   yeni bir ağ turu gerekmez, liste anında değişir.
2. **Bilinmeyen fiyat = paralı** — eksik ya da ayrıştırılamayan `pricing` alanı
   ücretsiz **sayılmaz**. Şüphe listeye yazılır, faturaya değil.
3. **Yedek de ücretsiz olmalı** — free-only modda katalog çekilemezse eski statik
   üçlü (`deepseek/deepseek-chat`, …) kullanılamaz: hepsi paralı. Ayrı bir
   `_DEFAULT_OPENROUTER_FREE_MODELS` yedeği var. Aksi hâlde "ücretsiz" yazan bir
   grup sessizce fatura yazardı — bu, sayfanın geri kalanındaki dürüstlük
   kuralının (uydurma id yok, uydurma fiyat yok) fatura tarafındaki karşılığı.

`NAUTILUS_OPENROUTER_MODELS` pin'i filtreden **muaf**: açıkça yazılmış bir id,
kullanıcının bilinçli tercihidir.

### Ücretsizler + seçili paralılar

"Hepsi ücretsiz" ile "hepsi açık" arasında üçüncü bir istek çıktı: *ücretsizler +
Kimi K3*. Pin (`NAUTILUS_OPENROUTER_MODELS`) bunu karşılamıyor, çünkü listenin
**yerine** geçer ve ağa çıkmaz — bir id eklemek için 17 ücretsizi elle yazmak
gerekirdi. Ayrı bir anahtar eklendi:

| Anahtar | Anlamı |
|---|---|
| `NAUTILUS_OPENROUTER_MODELS` | Liste **bu** olsun; ağa çıkma (test izolasyonu) |
| `NAUTILUS_OPENROUTER_EXTRA_MODELS` | Ücretsizlere **ek olarak** bunlar da gelsin |

İkisi ayrı tutuldu çünkü pin'in "ağa hiç çıkmaz" özelliği testlerin ağdan
bağımsızlığını taşıyor; onu toplamalı yapmak o garantiyi bozardı.

Paralı ek **iki ayrı yerde** işaretlenir, çünkü iki farklı yüzey var:

- **Etikette** (`OR · MoonshotAI: Kimi K3 · paralı`) — `agent_backtest.html` gibi
  optgroup'suz düz `<select>`lerde tek ayrım işareti budur.
- **Ayrı optgroup'ta** (`OpenRouter · elle eklenen — PARALI (1)`) — kokpitte.
  "ücretsiz (17)" başlıklı bir grubun içinde paralı bir satır dursaydı **başlık
  yalan söylerdi**; sayım da yanlış olurdu.

Katalogda bulunmayan bir ek id sessizce düşürülmez: ham id'siyle ve "paralı"
işaretiyle listelenir. Kullanıcının yazdığı bir id'yi yok saymak, bu sayfanın
açılışındaki sessiz-gizleme hatasının aynısı olurdu.

Optgroup başlığı durumu yazar (`… — ücretsiz (17)` / `… — tümü (337)`); şablon
bayrağı `web/routes/studio.py :: _llm_or_free_only` üzerinden alır. Sessiz bir
filtre, sayfanın açılışındaki "sessiz gizleme" hatasının tekrarı olurdu.

Açık kalan: modalite filtresi `text` **içeren** çıktıyı kabul ettiği için
`google/lyria-3-*` (müzik, `text+audio`) ücretsiz listesinde görünüyor — 337
içinde göze batmıyordu, 17 içinde batıyor.

## Adı sunucuda çözmek

`model_label()` / `model_id()`: boş seçim (`""` = uygulama varsayılanı) gerçek
modele çözülür, `or:` pini `OR · <id>` etiketlenir, kredi fallback'i devredeyse
**o** yazılır.

Çözümün sunucuda yapılması zorunlu, çünkü çözülmüş değer seçimden sapabilir:
ortam değişkeni, süreç-geneli degrade bayrağı ve koşu-başına thread pin'i aynı
anda konuşur. Şablonda "boşsa varsayılanı yaz" hilesi bu sapmaları göremez ve
**yanlış** ad yazar — hiç yazmamaktan kötüdür.

Rozet üç yerde, üç farklı doğruyla:

| Yüzey | Kaynak | Ne gösterir |
|---|---|---|
| AUTO — BRIEF rayı | `web/mission.py :: mission_view` | koşunun modeli |
| AUTO — üst bar | picker + HTMX OOB swap | idle'da seçim, koşarken koşan model |
| SIMPLE / PRO | `web/routes/studio.py :: llm_badge` | uygulama varsayılanı |

Üst bardaki çakışmada **canlı olan kazanır**: istemci tarafı (`mcModel`) seçiciyi
yansıtır, kokpit fragment'i her yoklamada aynı slota OOB yazarak üzerine biner.
Ters öncelik, biten bir koşunun modelini saatlerce ekranda tutardı.

Aynı turda Compose'daki sabit `"Claude Fable 5 looks at past backtest results…"`
cümlesi de etiketten beslenir hâle geldi — varsayılan değiştiğinde (ya da kredi
fallback'i devreye girdiğinde) yalan söylüyordu.

## Listelenebilir ≠ çalıştırılabilir

Aynı günün son şikâyeti: *"AUTO'da seçtiğim LLM'i kullanmıyor."* Picker 18
OpenRouter satırı gösteriyordu, seçim koşuya doğru gidiyordu (`brief.model`,
thread pin, rozet — hepsi doğru), ama **hiçbiri çalışamıyordu**. Koşu kaydı
(`ae9abbe9`, 19:05 UTC) sebebi bir satırda yazıyordu:

```
Claude request failed: RuntimeError: OpenRouter backend requires the `openai`
package. Install with: pip install openai — falling back to builtin
```

`openai` paketi `pyproject.toml`'da **zorunlu bağımlılık olarak yazılıydı** ama
sürecin yorumlayıcısında kurulu değildi. Bildirilmiş bağımlılık kurulmuş
bağımlılık değildir; ve yalnız tek bir kod yolunda (lazy import) kullanılan bir
paketin eksikliği, o yol denenene kadar hiçbir yerde patlamaz.

Asıl kusur eksik paket değil, **eksikliğin nasıl göründüğü**. Her LLM çağrısı
düştü, çağıranların graceful fallback'i devreye girdi ve koşu şunları üretti:

```
"Random desc_e1_295c1327/agnt_e_6a6ab8e4_7"
"Fallback random composition (Claude unavailable). · fallback (RuntimeError)"
```

Kokpitte faz **"✓ Generating strategy"** yazıyordu. Yani AUTO, kullanıcının
seçtiği modele hiç ulaşamadan beş tur rastgele kompozisyon backtest'ledi ve bunu
başarılı bir koşu gibi gösterdi. Uzun otonom koşuyu ayakta tutmak için tasarlanan
graceful degradation, **yapılandırma hatasında** yanlış ilaç: kurtarılacak bir
koşu yok, olmayan bir koşu var.

İki değişiklik:

1. **Kapıda ret.** `agent.model_unavailable_reason(model)` — ağa çıkmadan yalnız
   yapılandırmayı yoklar (anahtar var mı, istemci kurulabiliyor mu) ve
   `POST /agent/run` koşuyu **başlatmadan** 400 + sebep döndürür. Sınır dar
   tutuldu: yalnız `or:` yoklanır, Claude yolu uygulamanın varsayılanıdır.
2. **Reddin ekrana ulaşması.** htmx 1.9 varsayılanda 4xx'te swap etmez; rotanın
   *zaten var olan* hata gövdeleri (geçersiz tarih, eksik enstrüman, 50-koşu
   limiti) AUTO ekranına hiç ulaşmıyordu — START'a basmak hiçbir şey yapmamış
   gibi görünüyordu. `htmx:beforeSwap` içinde 4xx `shouldSwap = true` yapıldı,
   **yalnız `/agent/run` için** (kendi kendini sonlandıran `/agent/progress`
   yoklamasının 4xx gövdesi kokpitin yerine geçmemeli).

İkincisi olmadan birincisi sessiz bozulmayı sessiz redde çevirirdi — kullanıcı
açısından fark yok.

## 429: kalıcı arıza değil, zamanlama arızası

Paket düzeltmesinden hemen sonra çağrılar sağlayıcıya gerçekten ulaştı ve bu kez
`429 — Rate limit exceeded: free-models-per-min` döndü: 4 dakikada 11 fallback,
yine rastgele kompozisyonlar, yine `✓` fazlar. Yani ilk arıza kapandı, aynı
**sessiz bozulma yolu** ikinci bir sebeple açık kaldı.

Ölçüm, "kredi yükle" refleksini çürüttü: hesapta $10 kredi vardı, harcama $0,
`is_free_tier: false` — günlük kota (1000/gün) zaten yüksekti ve takılan o
değildi. Ücretsiz uçların **dakika başına** sınırı krediden bağımsız. Ajan
döngüsü tanımı gereği seri ve hızlı çağırır; ücretsiz katman tanımı gereği bunu
reddeder. Bu yüzden ücretsiz uç seçmek, degradasyon yolunu istisna olmaktan
çıkarıp **varsayılan** yapıyordu.

İki hamle:

1. **Ucuz-ama-paralı uç.** `deepseek/deepseek-v4-flash-0731` ($0.09/$0.18 per
   Mtok, 1M bağlam) izin listesine eklendi — Kimi K3'ten girişte 33×, çıkışta
   83× ucuz. Ödenen şey token değil, **oran sınırı**.
2. **`_or_create_with_backoff`.** 429 kalıcı değil zamanlama arızasıdır;
   yeniden denemeden çağıranın fallback'ine düşmek koşuyu çöpe çevirir. Plan
   `(5, 15, 45)` sn — toplamı 65 sn, çünkü sınır **dakika** başına: bir dakikalık
   pencereyi kapsamayan bir plan hiçbir işe yaramaz. (openai SDK'sının varsayılan
   iki denemesi ~0.5/1 sn ile geri çekildiği için tam olarak bu yüzden
   yetersizdi.) `Retry-After` varsa tahminimizi ezer; toplam bekleme
   `NAUTILUS_OPENROUTER_429_MAX_WAIT` (varsayılan 75 sn) ile sınırlı — bir
   dakikayı kapatacak kadar uzun, STOP yanıtını kilitlemeyecek kadar kısa.
   Yalnız 429 yeniden denenir; başka hata beklemeden çağırana düşer.

Not: aynı akışta iki **farklı kaynaklı** 429 görüldü — OpenRouter'ın kendi
`free-models-per-min`'i ve üst sağlayıcının `Provider returned error`'ı. Aynı
durum kodu, farklı merci.

## Açık kalan

Anahtar hâlâ yalnız ortam değişkeninden okunuyor; `ANTHROPIC_API_KEY` için var
olan `~/.nautilus_proxy_key` dosya yedeğinin OpenRouter karşılığı yok. Süreç
ortamsız başlatılırsa aynı sessiz boşluk tekrar eder.

Geri çekilme 429'u kapatır ama **sessiz bozulmanın kendisini** kapatmaz: bütçe
tükendiğinde ya da 429 dışı bir hata geldiğinde koşu hâlâ rastgele kompozisyona
düşüyor ve faz `✓` ile kapanıyor. Kalan iş, arızayı gizlemeyi bitirmek: fallback
sayacını koşu durumunda tutmak ve fazı `✓` yerine **"degraded"** işaretlemek —
koşuyu öldürmeden, ama başarı gibi göstermeden. Bugün iki farklı sebep aynı
sessiz yola çıktı; üçüncüsü de çıkacak.

> **Çıktı — ve kapandı (2026-08-03).** Üçüncü sebep ertesi gün geldi: `max_tokens`
> tavanı kimi-k3'ün üslubuna küçük gelip JSON'u kesiyor, hata `JSONDecodeError`
> kılığında modeli suçluyordu; koşu `362adcd1` 5/5 iterasyonu fallback'le üretip
> bütün fazları `✓` kapattı. Tavanlar büyütüldü, kesilme `TruncatedResponse`
> tipini kazandı ve yukarıdaki reçete (sayaç + `degraded` fazı + kokpit rozeti)
> uygulandı: [[kesilme_ve_degrade_gorunurlugu]].

## Uç (endpoint) varsayılanı: resmi API — proxy bir tercih (2026-08-11)

DeepR entegrasyon turu [YÜKSEK]: `llm_dispatch._build_client()` içinde
`ANTHROPIC_BASE_URL`'in varsayılanı sabit bir yerel proxy'ydi
(`http://localhost:6655`, bir Hyperspace kurulumundan kalma). Yalnız
`ANTHROPIC_API_KEY` veren temiz bir kurulumda — README'nin "doğrudan API"
dediği durumda — her çağrı bu makinede koşmayan bir porta gidip
`ConnectionError` ile düşüyordu. Hata "kredi tükendi" olmadığı için
`_is_credit_exhausted` tetiklenmiyor, çağıranların graceful fallback'i devreye
giriyor ve koşu "Random … (Claude unavailable)" kompozisyonlarıyla NORMAL
görünerek sürüyordu — bu sayfanın konusu olan sessiz bozulmanın aynısı, ama
model seçiminde değil ULAŞILABİLİRLİKTE.

Üç parça düzeltildi: (1) varsayılan artık SDK'nın resmi ucu, proxy yalnız
`ANTHROPIC_BASE_URL` açıkça verilince; (2) proxy ayarlıyken bir
`APIConnectionError`, adıyla anılan bir hataya çevriliyor
(`LLMEndpointUnreachable`: "LLM proxy yanıt vermiyor: `<url>`") — jenerik bir
bağlantı hatası kullanıcıya bakacağı yeri göstermiyordu; (3)
`strategy_studio/ai.py`'deki ikinci LLM entegrasyonu aynı değişkeni okuyor.
Eskiden aynı `ANTHROPIC_API_KEY` iki farklı uca gidiyordu (biri yerel proxy,
öbürü `api.anthropic.com`), yani bir proxy anahtarı üçüncü tarafa
gönderilebiliyordu. Testler: `tests/test_llm_endpoint_default.py`.

## Amaç-başına model (hibrit koşu) — 2026-08-15

Model pini KOŞU başınaydı (`set_thread_model`): bir turu bir uca bağlıyordun ve o
turun her çağrısı oraya gidiyordu. Yerel bir uç (llama-server + Qwen3.8-27B)
ölçüldüğünde bunun tek düğme olmasının pahalı olduğu görüldü:

| yol | yerel uçta sonuç |
|---|---|
| `composed` ×10 | **10/10** |
| `custom_block` ×8 | **4/8** (75 s deadline'da 2/8) |

Sebep modelin genel kalitesi değil: `custom_block` yolunun kendi tavanı
(`AGENT_CUSTOM_BLOCK_MAX_TOKENS`, varsayılan **ve** üst sınırı 1800) terse bir
modele göre kalibre; düşünen bir model onu her çağrıda aşıyor, kesilme retry
doğuruyor, retry de `AGENT_CUSTOM_BLOCK_TIMEOUT`'u yiyor. Üstelik blok kodu
ayrıca codegate'in AST + rol sözleşmesinden geçmek zorunda.

Çözüm düğmeyi bölmek: `NAUTILUS_MODEL_BY_PURPOSE`, biçim
`custom_block=claude-fable-5,narrative=or:qwen3.8-27b`. Anahtarlar
`_create_message(_purpose=...)` etiketleri. `llm_client.model_for_purpose(purpose)`
eşleme yoksa `current_model()` ile birebir aynı cevabı verir, yani çağrı
yollarında onun yerine geçebilir (`llm_dispatch` iki yerde kullanıyor:
sağlayıcıya giden model ve öğrenilen tavanın anahtarı).

Kredi kuralı `current_model()` ile aynı: kredi tükenmesi tercihi ezer ama yalnız
kendi fatura alanında — `or:` başka bir hesap, Claude kredisinin bitmesi onu
etkilemez. Testler: `tests/test_model_by_purpose.py`.

### Görünürlük boşluğu kapandı (aynı gün)

Eşleme ilk eklendiğinde muhasebe doğruydu (token defteri çağrı başına gerçek
modeli yazar, `_ledger_record(resp, called_model, purpose)`) ama EKRAN değildi:
rozet koşunun PİNİNİ gösteriyor, eşlenmiş amaç başka uca gidiyordu — yani bu
sayfanın kendi iddiası kısmen yanlış hâle gelmişti.

Kapatıldı: `llm_client.hybrid_note()` eşlemeyi okunur adlarla verir
("custom_block → Fable 5") ve üç yüzeyin üçü de onu taşır — SIMPLE/PRO rozeti
(`routes/studio.llm_badge()`), AUTO kokpiti (`mission.mission_view()`, hem rozet
hem brief satırı) ve sidebar ENGINE kartı (`templating._engine_model_label()`).

Ana etiket DEĞİŞMEZ: koşan model kendi adıyla görünür, yanına ` +hibrit` eklenir,
hangi amacın nereye gittiği `title`'da durur. Hibrit pini gizlemez, tamamlar.

Stilli span değil düz metin — `studio.html`'deki `mcModel()` picker değişiminde
slotu `textContent` ile ezdiği için oraya konan bir `<span>` ilk seçimde yok
olurdu; aynı ek JS tarafında da üretilir, eşleme metni şablona `|tojson` ile
geçer. Testler: `tests/test_model_badge_is_hybrid_aware.py` (8) — üç yüzey ayrı
ayrı, hem eşlemeli hem eşlemesiz. Tarayıcıda da doğrulandı.

## Üretimde doğrulandı (koşu 14ff96e7, 2026-08-15)

Hibrit ilk gerçek AUTO koşusunda **19 LLM çağrısının 19'unu** doğru uca yolladı:
`composed` ×8 + `idea` ×4 → yerel `qwen3.8-27b`, `custom_block` ×7 →
`claude-fable-5`. 7 strateji önerildi, hiçbiri `degraded` değil; hata/timeout 0.

Aynı koşu kardeş boşluğu da gösterdi ve o da kapatıldı: maliyet satırı bütün turu
koşunun pinlenmiş modeline atfediyordu. Artık model bazında kırılıyor
(`_run_cost`); tek harcayan varsa adı, birden fazlaysa "hibrit (N model)" yazılır.
Bkz. [[llm_maliyet_kaldiraclari]].

## Zamanaşımı ayarı yerel uca HİÇ ulaşmıyordu (2026-08-15, düzeltildi)

`_create_message_once` zamanaşımını `if not isinstance(client, _ClaudeCLIClient)`
koşuluyla enjekte ediyordu. Gerekçe doğruydu (CLI'ın kendi 300 s tavanı var, kısa
bir değerle ezme) ama karar VARSAYILAN istemciye bakıyordu. Uygulamanın varsayılan
backend'i Claude CLI iken `or:` pinli bir çağrı yine OpenRouter'a/yerel uca gider
ve o dalda kwargs'a zamanaşımı hiç konmadığı için `_run_openrouter_killable` kendi
120 s'lik varsayılanına düşüyordu.

Sonuç: `NAUTILUS_LLM_CALL_TIMEOUT=300` yerel uç için SESSİZCE ÖLÜYDÜ. Env
doğruydu, `pm2 env` doğruluyordu, davranış eskiydi — koşu `0057a0cd`'de iki çağrı
`OpenRouter call exceeded 120s hard deadline` ile düştü.

**Teşhisi veren şey hata metnindeki SAYI oldu:** 300 ayarlayıp 120 görmek. Bir
ayarın yürürlükte olduğunu doğrulamak (env var mı) yürütmeyi doğrulamaz.

Düzeltme koşulu hedefe bağladı: `model.startswith("or:") or not isinstance(...)`,
model karardan ÖNCE çözülür. Ölçüm ortamı bunu göstermemişti çünkü probe'lar
doğrudan OpenRouter istemcisi kuruyordu — kusur yalnız "varsayılan CLI + or: pin"
bileşiminde görünür. Testler: `tests/test_call_timeout_reaches_openrouter.py`.

## Brief'in açılış modeli pin'e bağlıdır — ölçümü ortamsız alma (2026-08-16)

AUTO brief'i MODEL kutusunu `AUTO_DEFAULT_MODEL` ile açar; `_mc_default_model`
bu id picker'da YOKSA sessizce `""`e (Claude) düşer. Geri düşme kasıtlı — işareti
olmayan bir kutu, kullanıcının görmediği bir uçla START'a basardı — ama sessiz
olduğu için sabitin doğruluğu ancak picker'ın **o ortamdaki** içeriğiyle birlikte
anlamlıdır.

Üretimde picker ağdan gelmiyor: `ecosystem.config.js`
`NAUTILUS_OPENROUTER_MODELS: "qwen3.8-27b"` **pin**'ini veriyor, pin listenin
yerine geçiyor ve satır `or:<pin>` olarak — yani **satıcı öneki olmadan** —
üretiliyor. Doğru sabit bu yüzden `or:qwen3.8-27b`; `or:qwen/qwen3.8-27b` (canlı
katalogdaki gerçek id) pinli listede bulunmaz ve kutuyu boşaltır.

Tuzak şuydu: aynı kontrol pm2'nin env'i olmayan bir kabukta koşturulunca gerçek
openrouter.ai'nin 25 satırlık ücretsiz listesi görünür, sabit "listede yok" çıkar
ve sabit yanlış yöne "düzeltilmek" istenir. **Model seçicisiyle ilgili her ölçüm,
uygulamanın koştuğu ortam değişkenleriyle alınmalıdır.**

Test bunu ortamdan bağımsız bağlar: `tests/test_studio_page.py` sabiti env'e değil
`ecosystem.config.js`'teki pin'e karşı doğrular, ayrıca geri düşmenin iki yönünü
de (listede var → seçili, yok → `""`) sabitler.

## `or:` öneki İSTEMCİYİ seçer, hedefi değil — ve hata mesajı bunu gizler (2026-08-19)

Üç ayrı AUTO koşusu `or:qwen3.8-27b` ile başladı ve üçünde de her LLM çağrısı
düştü:

    _OpenRouterProcessError: APIConnectionError: Connection error.

Cümlede "OpenRouter" geçtiği için teşhis üç koşu boyunca yanlış yere baktı
(sağlayıcı? ağ? "bu model kötü, ürettiği stratejiler zayıf"?). Gerçek yapılandırma
zaten yereldi:

    OPENROUTER_BASE_URL = http://127.0.0.1:8080/v1
    OPENROUTER_API_KEY  = local

`or:` öneki yalnız **hangi istemci kodunun** (OpenAI-uyumlu) kullanılacağını
seçiyor; HEDEF `OPENROUTER_BASE_URL`'de ve o yerel bir uç. Arıza tek satırla
görülebiliyordu: `curl http://127.0.0.1:8080/v1/models` → bağlantı reddedildi,
port kapalı. Yerel model sunucusu hiç ayakta değildi.

Ölçülen bedel: o üç koşuda sırasıyla 26, 44 ve 66 kez rastgele kompozisyona
düşüldü; üretilen her aday `research-only` damgası yiyip yayımlanamaz oldu. Yani
model hiç konuşmadı ama saatlerce koştuğu sanıldı — ve dağılım analizinde
"qwen'in adayları" diye raporlanan sayılar aslında RASTGELE arama sonuçlarıydı.

**Ayırt edici soru:** hata sınıfının adı çağrının HEDEFİNİ mi, kullanılan
İSTEMCİYİ mi anlatıyor? Sarmalayıcı sınıflar (`_XProcessError`) neredeyse her
zaman sarmalayanın adını taşır. Bağlantı hatasında önce **gerçek `base_url`'ü
yazdır**, sonra o adrese elle bir istek at.

Aynı ailenin başka yüzü bu sayfada zaten var (zamanaşımı ayarı yerel uca hiç
ulaşmıyordu, 2026-08-15): orada ayar doğruydu ve YÜRÜTME eskiydi; burada ayar
doğru ve HEDEF ayakta değil. İkisinde de "ayarı doğrula" yetmedi.

## Yerel uç Ollama'ya taşındı — ve pin'in ikinci tuzağı (2026-08-19/20)

`OPENROUTER_BASE_URL` artık `127.0.0.1:11434/v1` (Ollama), pin ise
`qwen2.5-coder:14b,qwen2.5-coder:32b,gemma4:26b`. Taşıma sırasında bu sayfanın
anlattığı tuzakların ikisi de tekrar ısırdı, biri yeni:

* **`pm2 restart <ad>` ecosystem dosyasını okumaz** — saklanan ortamı yeniden
  kullanır. Süreç 30 dk önce yeniden başlamış olmasına rağmen hâlâ `:8080`
  gösteriyordu. Gereken: `pm2 delete <ad> && pm2 start ecosystem.config.js
  --only <ad>` (bkz. [[surec_yoneticisi_ortami_dondurur]]).
* **Yerel sunucuyu "yeniden başlattım" demek başlatmış olmak değil.** İkinci
  `ollama serve` portu dolu bulup sessizce ölmüş, eski süreç (aynı PID, aynı
  başlangıç saati) hizmet vermeye devam etmişti. Doğrulama komuta değil
  **portu dinleyen PID'e** bakmalı.
* **YENİ: pin modeli seçer, bağlamı seçmez.** Model listede görünüp 200
  döndürse bile sunucunun varsayılan bağlam penceresi prompt'tan küçükse istek
  sessizce kırpılır. Ölçüm ve parmak izi:
  [[nau_yerel_model_secimi_2026_08_20]].

Model seçicinin görünürlük sözleşmesi de burada bir kez daha işe yaradı:
`AUTO_DEFAULT_MODEL` pin listesinde bulunmak zorunda olduğu için
(`tests/test_studio_page.py`), pin'e 14B eklenirken 32B listede tutuldu ve test
yeşil kaldı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_arama_ekonomisi]]
- [[auto_mission_control]]
- [[kesilme_ve_degrade_gorunurlugu]]
- [[llm_maliyet_kaldiraclari]]
- [[nau_auto_kosulari_2026_08_18]]
- [[surec_yoneticisi_ortami_dondurur]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
