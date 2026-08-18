---
title: Ticker kimlik değil, o günün etiketidir
type: concept
summary: Aynı fon/şirket yıllar içinde ad değiştirir (QQQ→QQQQ→QQQ, FB→META); ticker'ı kimlik sanan bir katalog seriyi sessizce ikiye böler ve eksik parça bir backtest'i yanlış yapar — QQQ'daki 2004-2011 deliği al-tut maksimum düşüşünü %53,5 yerine %35,6 gösteriyordu.
sources: []
last_updated: 2026-08-18
---

# Ticker kimlik değil, o günün etiketidir

Bir hisse/fon serisini `QQQ` diye saklamak, o serinin **adını** kimlik yerine
koymaktır. Ad zamanla değişir: QQQ 2004'te QQQQ oldu, 2011'de yine QQQ oldu;
FB 2022'de META oldu. Veri sağlayıcı her dönemi kendi o günkü etiketiyle
verdiği için katalogda **iki ayrı enstrüman** belirir ve aralarındaki ilişki
hiçbir yerde yazmaz.

Zarar sessizdir, çünkü eksik parça bir HATA üretmez — daha kısa bir seri
üretir ve backtest o seriyi mutlulukla koşar.

## Ölçülen zarar (QQQ, 2026-08-07)

QQQ'nun 2004-12 ile 2011-03 arası bu depoda yoktu; o aralık QQQQ etiketiyle
duruyordu. Delik **2008 ayı piyasasının tamamını** kapsıyordu, yani al-tut
karşılaştırması krizin kendisini hiç görmüyordu:

| | maksimum düşüş |
|---|---|
| delikli seri (QQQ tek başına) | %35,6 |
| dikilmiş seri (QQQ+QQQQ) | **%53,5** |

Bir strateji "al-tut'u yendi" derken karşısındaki al-tut'un neyi görmediği
buydu. Kapı ölçüyü doğru hesaplıyordu; ölçtüğü SERİ eksikti.

## Çözüm: dikiş bir enstrümandır, elle birleştirme değil

`stitch_ticker.py` parçaları **1-MINUTE** düzeyinde birleştirip yeni bir
enstrüman olarak (`--target QQQC`) kataloğa yazar; `ingest_equities.build_tf_bars`
TF'leri türetir, manifest yazılır. Sonuç doğrudan indirilmiş bir enstrümandan
ayırt edilemez — yani "her backtest'te hatırlamak" gerekmez.

Bu tercih bilinçli: bir birleştirmeyi ÇAĞRI ANINDA yapmak, unutulabilen bir
adımdır ve unutulduğunda sessizce yanlış sonuç verir. Katalogda duran bir
enstrüman ise unutulamaz.

## Dikiş denetlenir, varsayılmaz

İki parça da bugüne göre split-ayarlı olduğu için seviyelerin devam etmesi
beklenir — ama **beklenti ölçüm değildir**. `stitch_ticker.py` dikişin iki
yanındaki günlük hareketi serinin kendi medyan hareketiyle karşılaştırır ve
`--max-seam-ratio` katını aşarsa koşumu durdurur; aşma, parçalardan birinin
farklı ayarlandığı ya da yanlış eşleştirildiği anlamına gelir.

Ölçüm (QQQ+QQQQ): dikişler +%2,04 ve +%0,54, medyan günlük hareket %0,66 →
oranlar 3,1 ve 0,8. Yani dikiş geçti, ve GEÇTİĞİ ölçülerek söylendi.

Aynı aile: [[us_equity_katalog_veri_butunlugu]] (kataloğun bütünlük
denetimleri), [[index_backtest_via_equity_proxy]] (endeksin kendisi işlem
görmez, vekil enstrüman üzerinden test edilir).

Kod tarafı: `stitch_ticker.py`, `ingest_equities.py`, `download_grouped_daily.py`,
`extract_symbol_ticks.py`.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[us_equity_katalog_veri_butunlugu]]
<!-- BACKLINKS:END -->
