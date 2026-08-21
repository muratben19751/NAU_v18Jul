---
title: Aile içi ayırt edici ölçüt tasarlanamaz — çünkü ölçeceği şey yok
type: synthesis
summary: Aynı sinyal ailesi içinde parametre seçimini haklı çıkaracak bir ölçüt arandı; 29 MA parametrelendirmesi eğitim serisinin iki yarısında ölçüldüğünde sıra korelasyonu Calmar'da −0,06, maxDD'de +0,08 çıktı. İlk yarının en iyisi ikinci yarıda 19/29. Aile medyan Calmar'ı bile kalıcı değil (0,17→0,29, rejime bağlı); kalıcı olan tek şey aile medyan düşüşü (%29→%27). Sonuç: kapı parametreye kredi veremez, risk-ayarlı üstünlüğü aile düzeyinde bile sertifikalayamaz, yalnız düşüşten kaçınmayı raporlayabilir.
key_concepts:
  - kapi_ucdan_uca_dogrulandi_2026_08_21
  - auto_kapi_ve_geri_bildirim
  - wfo_cita_kalibrasyonu_2026_08_20
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/kapi_ucdan_uca_dogrulandi_2026_08_21.md
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/wfo_cita_kalibrasyonu_2026_08_20.md
last_updated: 2026-08-21
---

# Aile içi ayırt edici ölçüt tasarlanamaz

Düşük frekanslı adaylar için eklenen **bar bazlı düşüş kanıtı** ([[kapi_ucdan_uca_dogrulandi_2026_08_21]])
bir soruyu açıkta bırakmıştı: teşhis, kasıtlı olarak kötü seçilmiş bir
parametrelendirmeye de anlamlı p-değeri veriyordu (MA 80/90 → p = 0,021). Yani
**aile içinde ayırt edemiyordu.** İstek buydu: ayırt eden ölçütü tasarla.

## Önce tasarlanabilir mi diye soruldu

Doğru null, aile içi karşılaştırma için "rastgele maske" değil **ailenin
kendisi**dir. Ama aynı veride N parametreden en iyisini seçmek, aşırı-uyumun
tanımıdır — in-sample zirve kanıt değildir. O yüzden asıl soru şu oldu:

> Aile içi sıralama ZAMANDA kalıcı mı? Değilse hangi ölçüt kurulursa kurulsun
> gürültü seçer.

Ölçüm: 29 MA parametrelendirmesi (fast 10-120 × slow 50-300), QQQC 1-DAY eğitim
serisinin iki yarısında ayrı ayrı — 2003-2013 (2,449 bar) ve 2013-2023 (2,450 bar).

## Sonuç: üç katman, üç ayrı cevap

| katman | yarı 1 | yarı 2 | kalıcı mı |
|---|---|---|---|
| parametre **sırası** (Calmar) | — | — | **hayır** — ρ = −0,06 |
| parametre **sırası** (maxDD) | — | — | **hayır** — ρ = +0,08 |
| aile **medyan Calmar** | 0,17 | 0,29 | **hayır** |
| aile **medyan maxDD** | %29 | %27 | **evet** |

Korelasyon tek sayıdır ve yanıltabilir; desen de aynı şeyi söylüyor:

```
yarı1'in en iyi 5'i → yarı2'deki sırası (29 parametre içinde)
  MA  50/100  Calmar 0,29 → 19.
  MA  20/300  Calmar 0,23 →  7.
  MA  10/300  Calmar 0,22 → 22.
  MA  30/100  Calmar 0,22 → 26.
  MA  50/250  Calmar 0,22 → 12.
```

İlk yarının şampiyonu ikinci yarıda ortanın altında.

## Üç çıkarım, üçü de kapının kapsamını belirliyor

1. **Parametre seçimine kredi verilemez.** Sıra korelasyonu sıfırken herhangi
   bir seçici gürültü seçer. Aynı ölçüm WFO'nun pencere başına GA optimizasyonu
   için de bir soru işareti doğuruyor: sıralama kalıcı değilse o optimizasyon
   her pencerede gürültü seçiyor olabilir (ayrı bir soru, bkz. [[wfo_cita_kalibrasyonu_2026_08_20]]).
2. **Risk-ayarlı üstünlük aile düzeyinde bile sertifikalanamaz.** Medyan Calmar
   0,17'den 0,29'a çıkmış; sebep açık — ilk on yıl 2008'i içeriyor, ikincisi
   güçlü boğa. Calmar **rejime bağlı** ve rejim ekstrapole edilemez.
3. **Düşüşten kaçınma sertifikalanabilir.** Hem zamanda kalıcı (%29→%27) hem
   rastgele maskelere karşı anlamlı (p 0,005-0,033).

## Tasarımın cevabı: ölçüt YAZILMAMALI

İstenen ölçüt tasarlanmadı — iyi bir fikir bulunamadığı için değil, **ölçeceği
büyüklük bu veride var olmadığı için.** Bu bir başarısızlık değil, bir ölçüm
sonucu: `exposure_drawdown_evidence` bugünkü dar kapsamıyla (yalnız düşüş,
yalnız teşhis) doğru kapsamdaymış.

Kapsam bir tercih değil ölçüm sonucu olduğu için teste çivilendi:
`tests/test_low_frequency_has_its_own_evidence.py::test_the_diagnostic_deliberately_reports_only_the_stable_property`
— fonksiyonun gövdesi (docstring'siz, AST ile yeniden üretilmiş) Calmar
hesaplarsa test kırılır ve okuyucuyu bu ölçüme yönlendirir.

Genel ders, bu oturumun üçüncü tekrarı: **bir ölçütü tasarlamadan önce ayırt
edeceği farkın var olup olmadığını ölç.** Üç hipotez (ince zaman dilimi daha çok
gözlem verir, havuzlanmış alfa yavaş adayları kurtarır, kapı ulaşılamaz) bu
oturumda aynı disiplinle çürütüldü.

Kod tarafı: `auto/robustness.py::exposure_drawdown_evidence`,
`tests/test_low_frequency_has_its_own_evidence.py`.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[kapi_ucdan_uca_dogrulandi_2026_08_21]]
<!-- BACKLINKS:END -->
