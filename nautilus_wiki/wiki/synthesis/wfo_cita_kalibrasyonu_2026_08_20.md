---
title: WFO çıtası ölçüldü — eşik doğruydu, paydası yoktu
type: synthesis
summary: Pencerelerin ≥%50'si al-tut'u geçmeli" kuralı 3.000 rastgele stratejiyle sınandı: rastgelenin medyanı %23, %50'ye ulaşan oranı %1-3, yani eşik iyi kalibre. Asıl açık paydaydı — kaç pencereden hesaplandığına sınır yoktu ve 1/2 = %50 ile bir aday gerçekten geçmişti.
key_concepts:
  - auto_kapi_ve_geri_bildirim
  - auto_arama_ekonomisi
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/auto_arama_ekonomisi.md
last_updated: 2026-08-20
---

# WFO çıtası ölçüldü

Beş AUTO koşusu üst üste aynı yerde takıldı: robustluk zincirine giren adaylar
IS/OOS'tan, Monte Carlo'dan ve çok-sembolden geçiyor, **yalnız Walk-Forward'dan**
düşüyordu. Oranlar %14-37 bandındaydı, çıta %50.

Soru şuydu: **çıta mı yüksek, adaylar mı zayıf?** İkisi çok farklı sonuç doğurur
— birincisinde kapı yanlış kalibre edilmiştir, ikincisinde arama devam etmelidir.
Tartışmak yerine ölçüldü.

## Yapısal ön ölçüm: al-tut kaç pencerede kaybediyor?

`excess = strateji_getirisi − al-tut_getirisi`. Piyasada zamanın bir kısmında
duran bir strateji ortalamada al-tut'un bir KESRİNİ kazanır; dolayısıyla al-tut'u
ancak al-tut'un **negatif olduğu** pencerelerde geçebilir.

QQQC 22 yıllık eğitim serisi (4.899 günlük bar, 2003-09-10 → 2023-02-24):

| pencere ayarı | pencere sayısı | al-tut ≤ 0 olan oran | al-tut medyanı |
|---|---|---|---|
| 33/11 (yavaş aday) | 18 | **%17** | +%13,6 |
| 12/4/4 | 55 | **%25** | +%5,4 |
| 6/2/3 | 76 | **%29** | +%2,7 |

Uzun pencere çıtayı SERTLEŞTİRİYOR — boğa sürüklenmesi uzun pencerede daha çok
birikiyor. Bu, pencereyi adayın hızından türeten düzeltmenin (bkz.
[[auto_kapi_ve_geri_bildirim]]) istenmeyen bir yan etkisi: yavaş aday hem uzun
pencereye giriyor hem orada daha zor bir çıtayla karşılaşıyor.

## Asıl ölçüm: 3.000 rastgele strateji aynı pencerelerden geçirildi

Kapı `test_metrics_naive` okuduğu için (GA ile optimize EDİLMEMİŞ spec) ölçüm
GA'sız yapılabildi: rastgele spec'i her test penceresinde bir kez koştur, kaç
pencerede pozitif alfa ürettiğini say.

İki taban ayrı ölçüldü, çünkü `_fallback_composed` sanıldığı gibi "rastgele"
değil — ayrıntı [[auto_arama_ekonomisi]]'nde.

**Sonuç (strateji başına ayrıntı kaydedilerek, iki zaman dilimi):**

| en az geçerli pencere | 1-HOUR medyan | 1-HOUR p95 | 1-HOUR %50'yi geçen | 1-DAY %50'yi geçen |
|---|---|---|---|---|
| ≥1 (o günkü hâl) | %24 | %67 | %10 | **%29** |
| ≥5 | %23 | %44 | %4 | %14 |
| ≥10 | %23 | %42 | %3 | %6 |
| ≥15 | %23 | %39 | %2 | %1 |
| ≥20 | %23 | %38 | %2 | %1 |

İki şey aynı anda okunmalı:

* **Medyan filtreden bağımsız: %23.** Her iki zaman diliminde de aynı.
  Beceriksizliğin gerçek seviyesi budur.
* **Kuyruk tamamen örneklem eseri.** p95 %67'den %38'e, "%50'yi geçen" oranı
  %29'dan %1'e iner. Yüksek yüzdelikler beceri değil, az-pencereli stratejilerin
  gürültüsüydü — 3 pencerede 3 kazanç da %100 görünüyor.

## Karar: eşik %50'de kaldı

Sağlam örneklemde rastgele stratejilerin yalnız **%1-3'ü** %50'yi tutturuyor.
Yani eşik, boş dağılımın **p97-99'u** — bir terfi kapısı için makul bir yer.
Düşürmek için ölçümde sebep yok, ve düşürmek "kapıyı sonuca uydurmak" olurdu.

Peki adaylar nerede duruyor? Payda sınırını geçen 12 adayın alfa oranları:

```
10%, 23%, 24%, 24%, 25%, 33%, 35%, 38%, 40%, 41%, 42%, 47%   → medyan %34
```

**Aday medyanı %34, rastgele medyanı %23.** Yani adaylar şanstan belirgin
biçimde iyi — medyan aday, rastgele dağılımın p90'ı civarında; en iyi aday
(%47, 28/60) p95-p99 bandında. Ama hiçbiri %50'yi görmüyor.

Doğru okuma bu: **stratejiler beceri gösteriyor, yetecek kadar değil.** Kapı ne
haksız ne de formalite — boş dağılımın p97-99'una konmuş bir çıta ve en iyi
adayımız oraya kıl payı yetişemiyor. (İlk yazımda bu bant "%14-37, rastgeleden
farksız" diye geçmişti; sayı doğrulanınca düzeltildi — hesaba yetersiz paydalı
adaylar da karışmıştı.)

## Düzeltme: oranın PAYDASINA sınır

Açık, eşikte değil paydadaydı:

```python
ratio = alpha_positive / len(valid)   # len(valid) icin ALT SINIR YOK
ok = ratio >= 0.5
```

`len(valid) == 2`, `alpha == 1` → %50 → **geçti**. Ekranda `1/2` yazıyor ve
`5/10` ile aynı ağırlıkta görünüyor.

**Bu teorik değil, gerçekleşmiş:** diskteki 32 adayın tamamı eski ve yeni kuralla
yeniden değerlendirildi; kararı değişen tek aday koşu `4f7849df`'nin ilk adayı —
**1/2 pencere ile WFO'dan geçmişti.**

`WFO_MIN_VALID_WINDOWS = 10` (`NAUTILUS_WFO_MIN_WINDOWS`). 15 daha güvenli
olurdu (%1-2 yanlış geçiş) ama gerçek adayların çoğunun 7-17 penceresi var;
10, yanlış geçişi %3-6'ya indirirken adayları ölçülebilir bırakıyor.

### Sınırın altı RET, "atlama" değil

Kapıda `measured=False` `_skip()`e düşüyor ve `failed` **artmıyor**; sıkı modda
3 ölçüt yeterli olduğu için yetersiz-pencereli bir aday kalan IS/OOS +
çok-sembol + Monte Carlo ile **terfi edebilirdi**. Yani "ölçülemedi" diye
işaretlemek kapıyı GEVŞETİRDİ.

Bu yüzden yetersiz payda `measured=True` + `ok=False` döner: ekranda gerçek oran
görünür, aday geçmez, ve gerekçe iki reddi ayırır —

```
only 7 valid windows — 10 needed to judge (not a performance rejection)
alpha in only 3/13 windows (<50%)
```

## Ölçmenin kendisinde çıkan üç tuzak

1. **İlk taban ölçümü çöp çıktı**: 600 stratejinin 21'i ölçülebildi ve "p95 =
   %100" gibi rakamlar üretti. Sebep, günlük barda rastgele stratejilerin
   pencere başına 5 işleme ulaşamaması. Yüzdelikler değil, **ölçülebilirlik
   oranı** okunmalıydı.
2. **Backtest tek başına kıyası damgalamıyor.** WFO yolu `stamp_buy_hold_benchmark`
   çağırıyor (`backtest_robustness.py:514`); ölçüm betiği çağırmayınca tüm alanlar
   `None` geldi ve "hiç strateji ölçülemedi" gibi göründü.
3. **Özet kaydetmek yetmedi.** İlk koşu yalnız yüzdelikleri sakladı, strateji
   başına pencere sayısını değil — bu yüzden "p95 neden %100" sorusu ancak
   ikinci, ayrıntı kaydeden koşuyla cevaplanabildi.

Kod tarafı: `auto/robustness.py` (`wfo_verdict`, `WFO_MIN_VALID_WINDOWS`),
`tests/test_wfo_line_shows_the_deciding_number.py`.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_arama_ekonomisi]]
- [[auto_kapi_ve_geri_bildirim]]
<!-- BACKLINKS:END -->
