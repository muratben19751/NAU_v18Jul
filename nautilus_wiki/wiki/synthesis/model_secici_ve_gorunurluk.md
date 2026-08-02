---
title: Model seçici ve model görünürlüğü
type: synthesis
summary: Hangi LLM'in koştuğu her ekranda çözülmüş adıyla yazılır; OpenRouter listesi openrouter.ai kataloğundan canlı gelir, varsayılanda ücretsiz uçlarla sınırlıdır ve çekim başarısızsa statik yedeğe düşer — asla uydurma ya da sürpriz-faturalı id'ye.
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - https://openrouter.ai/api/v1/models
key_concepts:
  - strategy_studio
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/tear_sheet_overlay.md
last_updated: 2026-08-02
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

## Açık kalan

Anahtar hâlâ yalnız ortam değişkeninden okunuyor; `ANTHROPIC_API_KEY` için var
olan `~/.nautilus_proxy_key` dosya yedeğinin OpenRouter karşılığı yok. Süreç
ortamsız başlatılırsa aynı sessiz boşluk tekrar eder.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_mission_control]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
