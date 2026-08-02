---
title: Model seçici ve model görünürlüğü
type: synthesis
summary: Hangi LLM'in koştuğu her ekranda çözülmüş adıyla yazılır; OpenRouter listesi openrouter.ai kataloğundan canlı gelir, çekim başarısızsa statik yedeğe düşer — asla uydurma id'ye.
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
