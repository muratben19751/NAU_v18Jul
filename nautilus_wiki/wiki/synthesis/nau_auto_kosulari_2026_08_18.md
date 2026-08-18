---
title: AUTO koşuları 2026-08-18 — mühürlü kapı ilk kez koştu
type: synthesis
summary: Beş koşu ve on üç turda kapı düzeltmeleri canlıda sınandı; fallback kompozisyonu bir koşuyu öldürdü ve düzeltildi, WFO ölü ölçütten çalışır hâle geldi, mühürlü holdout ilk kez açıldı ve koşu öncesi tahmin (3,9) gerçekleşenle (4) tuttu.
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/nau_holdout_dogrulama_turu_2026_08_18.md
  - wiki/synthesis/multi_symbol_generalization.md
last_updated: 2026-08-18
---

# AUTO koşuları 2026-08-18

Sabah yazılan beş düzeltme (mühürlü kapının dört açığı + WFO penceresi) aynı gün
canlı koşularla sınandı. Bu sayfa koşuların **ne öğrettiğini** tutar; kuralların
kendisi [[auto_kapi_ve_geri_bildirim]] ve
[[nau_holdout_dogrulama_turu_2026_08_18]] sayfalarında.

Hepsi QQQC.NASDAQ, 3 timeframe (1-HOUR / 4-HOUR / 1-DAY), sürekli mod.

| koşu | model | tur | sonuç |
|---|---|---|---|
| `72029368` | `or:qwen3.8-27b` | 3 | **öldü** — fallback `ValueError` |
| `8aa18365` | `claude-opus-5` | 3 | `winless_limit` — üç gerçek ret |
| `85c6330c` | `or:qwen3.8-27b` | 1+ | elle durduruldu |
| `9016d12a` | `claude-sonnet-5` | 3 | `winless_limit` — **mühürlü kapı koştu** |
| `568f2838` | `claude-haiku-4-5` | 1+ | WFO alfası: 6/6 kârlı ama 1/6 al-tut'u geçiyor |

## 1. Fallback'in kendisi koşuyu öldürdü — ve %40'lık bir yazı-turaydı

`72029368` üçüncü turunda düştü:

    ValueError: proposal missing entry block after cleanup
      agent._fallback_composed → _validate_composed

Zincir: OpenRouter bağlantısı koptu → sistem fallback'e geçti → **fallback
geçersiz bir öneri üretti** → istisna yakalanmadığı için tüm oturum düştü. Son
savunma hattı, savunmaya çalıştığı arızanın üstüne kendi arızasını ekledi.

Sebep elle yazılmış bir kapsam: giriş bloğu seçilirken yalnız `{"atr_stop"}`
dışlanıyordu. Yorum kuralı DOĞRU yazıyordu (*"çıkış-özel bloklar girişe
seçilmesin"*), kod ise o kuralın yazıldığı günkü fotoğrafını uyguluyordu.
Katalog çalışma anında büyüyor ve custom bloklar rollerini META'DA ilan ediyor.

Canlı katalogda ölçüldü: **408 blok — 71 built-in (rolsüz), 175 `entry`, 162
`exit`.** Fallback girişi 407 tip arasından seçtiği için her çağrı **~%40
ihtimalle ölümcüldü**. O koşuda 44 fallback vardı.

Düzeltme iki katmanlı: uygunluk `meta["role"]`den TÜRETİLİYOR, ve doğrulama yine
patlarsa built-in'lerle yeniden deneniyor — ama sessizce değil, `logging.warning`
ile. Canlı doğrulama: `85c6330c` aynı modelle 26 fallback üretti, hiçbiri
patlamadı.

## 2. WFO ölü ölçütten çalışır ölçüte

Sabahki ölçüm: diskteki 178 pencerede işlem dağılımı `{0: 70, 1: 88, 2: 20}` —
hiçbiri eşiğe (5) ulaşmamış, yani ölçüt hiç konuşmamış ama GA maliyeti her
koşuda ödenmiş. Pencere adayın hızından türetilince (`wfo_window_months`):

Ölçülen on aday (dört koşu), pencere adayın hızına göre:

| pencere (eğitim/test) | üretilen | geçerli | koşu |
|---|---:|---:|---|
| 6 / 2 ay (**taban**) | 76 | 75 | `72029368` |
| 6 / 2 ay (**taban**) | 76 | 60 | `8aa18365` |
| 9 / 3 ay | 56 | 45 | `8aa18365` |
| 9 / 3 ay | 56 | 2 | `8aa18365` |
| 12 / 4 ay | 37 | 31 | `72029368` |
| 18 / 6 ay | 24 | 18 | `72029368` |
| 27 / 9 ay | 15 | 9 | `72029368` |
| 30 / 10 ay | 13 | 3 | `8aa18365` |
| 39 / 13 ay | 10 | 7 | `72029368` |
| 48 / 16 ay | 8 | **0** | `9016d12a` |

İki uç öğretici. **Taban korunuyor**: hızlı adayda pencere büyümedi, gerek
yoktu (76 pencerenin 60-75'i geçerli). **En yavaş uçta genişletme bile
yetmedi** — 48/16 ay `WFO_MIN_WINDOWS=8` sınırına dayandı ve sıfır geçerli
pencere verdi; ama orada sistem *"muhtemelen susacak"* diye ÖNDEN uyardı ve
haklı çıktı. Aynı pencere genişliğinin (9/3) iki farklı adayda 45 ve 2 geçerli
pencere vermesi de ayrı bir hatırlatma: pencere boyutu adayın hızından türüyor,
ama geçerlilik yine de adayın o pencerelerde gerçekten işlem yapmasına bağlı.

## 3. Mühürlü kapı ilk kez koştu, tahmin tuttu

`9016d12a` turu 1'de bir aday robustluk zincirini geçti ve mühür açıldı.
Beş koşu / on üç tur boyunca bu ilk kezdi.

    ⚠ Sealed OOS forecast: ...~3.9 entries, below the 5 the gate needs
    ⚠ Sealed OOS (1253d / 862 bar): 4 trade — ÖLÇÜLEMEDİ. 5 giriş gerekiyordu
    ❌ not published: only 4 holdout trades; need 5

**Tahmin 3,9 — gerçek 4.** Eğitim hızından mühürlü pencereyi öngörme aritmetiği
kalibre. Eski sabit eşikle (20) aynı aday 16 giriş eksikle elenir ve sebep
"kapı ulaşılamaz" olurdu; şimdi ret adayın hızı hakkında.

Mühürde `excess −%71`: al-tut 1253 günde +%144 yaparken strateji +%73'te
kalmış — ölçülebilseydi de reddedilecekti.

## 4. Ret sebeplerinin dağılımı: kapı çalışıyor, üretim tıkalı

Gerçek (degraded olmayan) retler:

| sebep | kaç kez |
|---|---|
| Monte Carlo medyan DD (−%42,1 ve −%25,2) | 2 |
| WFO alfası (%47 ve 0/2) — *görünmüyordu* | 2 |
| çok-sembol kesin ret (`✗ Symbol specific`) | 2 |
| mühürlü kapı (4 < 5 giriş) | 1 |

Biri **0,2 puanla** kaçırdı (MC −%25,2 vs sınır −%25,0). Kapı çalışıyor.
Tıkanma üretimde: sonnet'in adayları yavaş (tur 2 medyanı **27 işlem**), ve
sıralamanın ≥20 eşiğini 15 adayın ancak 1'i geçiyor.

## 5. Akran sepetinin bağımsızlığı pencereyle daraldı

Peer penceresi mühre çapalanınca 2021-02 → 2023-02'ye kaydı, yani içine 2022 ayı
piyasası girdi. Etkin bağımsız sembol **2,32 → 1,5-1,6**. Kriz penceresinde
korelasyon fırlıyor, çeşitlilik kayboluyor: `pass_rate = %80 ✓ Generalizable`
etiketi bile ~1,6 bağımsız gözlemin üstünde duruyor. Ayrıntı:
[[multi_symbol_generalization]].

Geçen semboller de öğretici: SPY ve IWM (endeksler) geçiyor, AAPL/MSFT/NVDA
düşüyor. "5 sembolde sınandı" cümlesi bu pencerede fazlasıyla cömert.

## 6. WFO düzeltmesinin en çıplak hâli (`568f2838`, tur 1)

Düzeltme aynı gün canlıda şu satırı verdi:

    → 1/6 valid windows beat buy&hold (saved spec) · 6/6 merely profitable
    ✗ Walk-Forward: alpha in only 1/6 windows (<50%)

**Altı pencerenin altısı kârlı, yalnız biri al-tut'u geçiyor.** Eski kodla bu
satır `WFO: 6/6` yazardı — kusursuz bir skor — ve aday yine elenirdi, gerekçesiz.
Boğa piyasasında "kârlı pencere" ile "alfa üreten pencere" arasındaki farkın
ölçülmüş en keskin örneği.

(Aynı tur bir kusur da gösterdi: gerekçe iki kez basılıyordu, çünkü satır hem
suite'e hem kapıya eklenmişti. Suite'teki kopya kaldırıldı — orada `penalized`
ipucu yok, yani sönümlenmiş-Sharpe bacağında kapıyla ıraksayabilirdi.)

Bu tur ayrıca **kontrol kolu**: yeni ACCEPTANCE CRITERIA prompt'u henüz
yürürlükte değildi (ana süreç, restart yapılmadı). Bazı: 30 aday, 1 nitelenen,
en iyi ×1,44.

## 7. Model profilleri — iki çürütülmüş beklenti

| model | işlem medyanı | iterasyon süresi | Calmar medyanı | n |
|---|---:|---:|---:|---:|
| Opus 5 | 296 | ~1,4 dk | 0,26 | 45 |
| Haiku 4.5 | ~172 | **~5,1 dk** | 0,20 | 37 |
| Sonnet 5 | ~26 | ~1,4 dk | 0,14 | 45 |

İki tahminim de ölçümle çürüdü. **"Küçük model daha basit/yavaş strateji
üretir"**: Haiku, Sonnet'ten on kat fazla işlem yapan stratejiler üretti.
**"Ucuz model daha hızlı tur demek"**: Haiku iterasyon başına dört kat YAVAŞ,
çünkü ürettiği yüksek frekanslı spec'lerin backtest'i pahalı — LLM çağrısı ucuz,
sonucu değil. 4 saatlik tavanda Haiku 2-3 tur, diğerleri 3+ tur veriyor.

Sabit kalan tek şey: her üç modelde de **15 adaydan 1'i** sıralamayı geçti. Kapı
model seçimine duyarsız; darboğaz sabit.

## Kalan açık uç

**Katalog'a hâlâ hiçbir strateji eklenmedi.** Kapı artık dürüst konuşuyor ve
gerekçesini yazıyor; sıradaki soru üretim tarafında — adaylar ya çok az işlem
yapıyor ya tek sembole özel çıkıyor.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_arama_ekonomisi]]
- [[multi_symbol_generalization]]
- [[nau_holdout_dogrulama_turu_2026_08_18]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
