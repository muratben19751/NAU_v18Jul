---
title: Kapı uçtan uca doğrulandı — doğru geçiriyor, doğru reddediyor
type: synthesis
summary: Al-tut'u risk-ayarlı geçen bir strateji ilk kez bulundu (MA 50/100 — %313 getiri / %27 düşüş, Calmar 0,28 vs 0,22) ve kapı onu gevşek modda GEÇİRDİ, sıkı modda REDDETTİ; ret doğru çünkü dört ölçütten yalnız ikisi ölçülebiliyordu. Kalan sınır yapısal: bu seride al-tut'u geçen yaklaşım 19 yılda 13-20 işlem yapıyor, doğrulama makinesi ise pencere başına 5 işlem istiyor.
key_concepts:
  - auto_kapi_ve_geri_bildirim
  - wfo_cita_kalibrasyonu_2026_08_20
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/wfo_cita_kalibrasyonu_2026_08_20.md
last_updated: 2026-08-21
---

# Kapı uçtan uca doğrulandı

Uzun bir arama boyunca hiçbir aday kapıyı geçemedi ve akla gelen açıklama
"stratejiler zayıf"tı. Bu tur, o açıklamayı ölçümle sınadı ve **iki ayrı
sebebi** ayırdı.

## 1. Kapıda gerçek bir tanım kayması vardı

Üç kapı da "aday al-tut'u geçmeli" diyordu. Sıralama ve çok-sembol kapıları bunu
`app_constants.benchmark_rejection` ile çözüyordu — `risk_adjusted` modunda ölçü
**Calmar üstünlüğü + pozitif CAGR**, alfanın pozitif olması şart değil. WFO bu
fonksiyonu hiç çağırmıyordu; 2026-08-16'da terk edilen pozitif-alfa kuralını
uyguluyordu. Ölçüm (14 aday, 358 pencere): çıtayı geçen aday **0/14 → 2/14**.

Genel ders: bir sistem bir tanımı güncellediğinde, o tanımı KENDİ kopyasıyla
uygulayan alt sistemler güncellenmez — ve aynı cümle iki farklı şeyi ölçmeye
başlar. Ortak fonksiyonun docstring'i zaten "Kural burada TEK kopya" diyordu;
niyet yazılıydı ama denetlenmiyordu. Artık bir kaynak testi çiviliyor.

## 2. Aramanın baktığı yerde kenar yoktu — baktığı yer dışında vardı

Altı klasik sinyal ailesi aynı pencerelerden geçirildi (aile başına 6 ayar):
hepsi **%27-33** bandında toplandı; rastgele taban %23, yapısal tavan %58.
İkili kesişimler daha kötü çıktı ya da AND yüzünden hiç ölçülemedi.

Tek sınanmamış aile **uzun-MA rejim filtresi**ydi — çünkü seyrek işlem yaptığı
için kapının pencere başına 5 işlem şartını hiç karşılamıyordu. Kendi
iddiasının olduğu yerde (tüm seri, doğru pozisyon boyutuyla) ölçüldü:

| | getiri | max düşüş | Calmar |
|---|---|---|---|
| al-tut | %781 | %53 | 0,22 |
| MA 50/200 (günlük) | %341 | %27 | **0,29** |
| MA 50/100 (günlük) | %313 | %27 | **0,28** |
| MA 50/800 (saatlik) | %268 | %25 | **0,27** |

**12 ayarın 9'u al-tut'a Calmar üstünlüğü kuruyor.** Getirinin yarısını alıp
düşüşün yarısından azını yaşıyorlar — kapının `risk_adjusted` modunun tam olarak
aradığı profil.

### Pozisyon boyutu tuzağı (ikinci kez)

İlk ölçüm bu aileyi "getirisi %10-23" diye gösterdi ve neredeyse elenecekti.
Sebep spec'te sabit **10 hisse** kullanmamdı: al-tut %100 yatırımlıyken strateji
hesabın küçük bir kısmıyla işlem yapıyordu. `percent_equity` ile yeniden
ölçülünce getiri %313'e çıktı. Aynı tuzak [[auto_arama_ekonomisi]]'nde zaten
kayıtlıydı — wiki okumak burada doğrudan sonucu değiştirdi.

## 3. Kapının kararı: gevşek GEÇTİ, sıkı REDDETTİ — ve ikisi de doğru

MA 50/100 (20 işlem) uçtan uca koşturuldu, **5 tekrar, 5'i birebir aynı**:

```
değerlendirilen=2 · düşen=0 · gevşek=GEÇTİ · sıkı=GEÇMEDİ · MC medyan DD −%19,1
```

Sıkı mod en az 3 ölçüt ister; ölçülebilen 2 (WFO: "pencere başına 5 işlem yok",
çok-sembol: "yetersiz veri"). **Kapı ölçemediğini sertifikalamıyor** — bu bir
kusur değil, doğru kalibrasyon. Aynı turda eklenen "geçiş cümlesi kendi
kapsamını söyler" mesajı tam bu vakayı görünür kılmak içindi.

En iyi varyant (MA 50/200, Calmar 0,29) ise sıralama kapısına bile giremedi:
**13 < `_MIN_TRADES` (20)**.

## Açık sorunun ilk yarısı cevaplandı: bar bazlı kanıt yolu

Yukarıdaki "düşük frekanslı stratejilere ayrı doğrulama yolu mu gerekir"
sorusunun **teknik** yarısı ölçüldü ve cevabı EVET: yol kurulabilir.

`auto.robustness.exposure_drawdown_evidence(bars_df, trades)` — gözlenen
maksimum düşüşü, **aynı süreyi piyasada geçiren** rastgele maskelerin
dağılımına karşı sınar. Null, maskeyi dairesel KAYDIRIR: maruziyet süresini ve
her iki serinin otokorelasyonunu korur, yalnız hizalamayı bozar. Böylece
*"bu maske özel mi, yoksa bu kadar zaman piyasada kalmak zaten yeter mi"*
ayrışır. (Naif bir t-testi otokorelasyon yüzünden anlamlılığı abartırdı.)

Ölçüm (QQQC 19 yıl, 4.899 bar):

| MA | maxDD | rastgele, aynı maruziyet | p |
|---|---|---|---|
| 5/250 | %25 | %51 | **0,005** |
| 50/100 | %28 | %49 | **0,019** |
| 50/200 | %28 | %49 | **0,033** |
| 20/100 | %28 | %47 | 0,139 |

**İşlem sayısı kısıtı burada hiç doğmuyor**: 13 işlemlik bir aday da 4.899 bar
üzerinden yargılanabiliyor. Kapının bugünkü tıkanıklığı ("20 işlemi 10
pencereye bölemiyorum") bar bazlı ölçümde yok.

### Ama KARAR VERMİYOR — ve bu ölçüme dayalı bir tercih

Aynı test, bilerek seçilmiş KÖTÜ parametrelendirmelerde de anlamlı çıkıyor
(MA 80/90 → p=0,021). Yani test *"MA ailesi düşüşten kaçar"* diyor —
trend takibinin bilinen özelliği — ama *"bu ayar iyidir"* demiyor. **Aile
içinde ayırt etmiyor**, dolayısıyla kapıyı açsaydı zayıf ayarları da
geçirirdi. Bu, `WFO_MIN_VALID_WINDOWS` ve kazanılabilirlik ölçüsünde uygulanan
ilkenin aynısı: kalibre edilmemiş bir ölçüt raporlar, karar vermez.

Calmar bacağı da ölçüldü ve **eklenmedi** (p 0,08-0,44): düşüşten kaçmak
getiriden feragat ettiriyor, ikisi netleşince üstünlük gürültüye karışıyor.

### Kalan açık soru artık teknik değil

Bu teşhis bir gün karar verecekse, önce **aile içinde ayırt eden** bir ölçüte
ihtiyacı var — ve o henüz yok. Karar gerektiren soru buraya taşındı.

### Yol boyunca iki ölçüm hatası (ikisi de tekrar edilebilir)

1. **"Calmar üstünlüğü sadece az yatırımdan geliyor"** sanıldı. Kontrol
   çürüttü: sabit maruziyet %100'den %30'a inerken Calmar **0,20-0,22'de
   SABİT** kalıyor — CAGR ve düşüş orantılı küçülüyor, oran değişmiyor.
2. **Ortalama bar getirisiyle ölçüldü** — yanlış istatistik. Strateji
   ortalamada daha KÖTÜ barlarda (içeri 5,15 bp / dışarı 5,95 bp) ama
   kümelenmiş düşüşlerden kaçarak maxDD'yi eziyor. **İddia neyse istatistik
   o olmalı.**

## Kalan sınır — ve o bir kusur değil

Bu seride al-tut'u geçen yaklaşım **19 yılda 13-20 işlem** yapıyor; doğrulama
makinesi istatistiksel geçerlilik için pencere başına 5, toplam 20 işlem
istiyor. İkisi kesişmiyor. Kapı doğru şeyi arıyor, doğru ölçüyor ve haklı olarak
"yetersiz kanıt" diyor — ama aradığı şeyin bu veri derinliğinde kanıtlanması
mümkün değil.

Bu, eşiği düşürme gerekçesi DEĞİL: 13 işlemle kurulan bir Calmar üstünlüğü
gerçekten de şans olabilir. Karar gerektiren asıl soru şu: **düşük frekanslı
stratejiler için ayrı bir doğrulama yolu mu gerekir** (daha uzun seri, daha çok
enstrüman, ya da işlem yerine ZAMAN bazlı pencereleme) — yoksa arama bilinçli
olarak yüksek frekanslı alt-uzayla mı sınırlanır?

Bu sorunun bir yarısı ölçümle kapandı: aile İÇİNDE ayırt eden bir ölçüt
tasarlanamaz, çünkü aile içi sıralama zamanda kalıcı değil —
bkz. [[aile_ici_ayirt_edicilik_2026_08_21]].

Kod tarafı: `auto/robustness.py`, `app_constants.benchmark_rejection`,
`web/routes/agent_backtest.py` (`_MIN_TRADES`, `_robustness_tally`).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[aile_ici_ayirt_edicilik_2026_08_21]]
- [[auto_kapi_ve_geri_bildirim]]
<!-- BACKLINKS:END -->
