---
title: Aile içi ayırt edici ölçüt tasarlanamaz — çünkü ölçeceği şey yok
type: synthesis
summary: Aynı sinyal ailesi içinde parametre seçimini haklı çıkaracak bir ölçüt arandı; üç enstrümanda (QQQC/SPY/IBM, ~30 MA parametrelendirmesi, 2003-2013 vs 2013-2023) sıra korelasyonunun altı ölçümünde de güven aralığı pozitif tarafta durmadı, üçünde tamamen negatif (IBM Calmar ρ = −0,50). Seçmenin bedeli de ölçüldü: şampiyonu seçmek, aile medyanını almaya kıyasla −0,03/−0,00/−0,02 Calmar — yani sıfır. Ayırt edici ölçüt yazılmadı; yerine geçen dürüst tasarım söndürücü: adayın beklentisi kendi sayısı değil ailesinin medyanı.
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

## Sonuç, üç enstrümanda tekrarlandı

Tek seride kurulan bir iddia bu projede yeterli sayılmıyor; ölçüm boşluksuz
1-DAY serisi olan üç enstrümanda tekrarlandı (aynı pencereler, 2003-2013 /
2013-2023). Parantez içi %95 bootstrap güven aralığı:

| enstrüman | n | Calmar ρ | maxDD ρ |
|---|---|---|---|
| QQQC | 29 | −0,06 [−0,44, +0,33] | +0,08 [−0,30, +0,43] |
| SPY | 31 | +0,01 [−0,34, +0,36] | **−0,34** [−0,60, −0,02] |
| IBM | 32 | **−0,50** [−0,71, −0,18] | **−0,44** [−0,71, −0,07] |

**Altı ölçümün hiçbirinde güven aralığı pozitif tarafta durmuyor; üçünde
tamamen NEGATİF tarafta.** Yani in-sample en iyiyi seçmek yalnız işe yaramıyor
değil — bu serilerde hafifçe ters yönde bilgi taşıyor (ortalamaya dönüş).

IBM en keskin örnek: ilk yarının şampiyonu MA 50/100 (Calmar 0,49) ikinci
yarıda 32 parametre arasında **29.** ve Calmar'ı **negatif** (−0,15).

## Üç katman, üç ayrı cevap

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

## Seçimin bedeli ölçüldü: sıfır

Asıl soru "sıralama kalıcı mı" değil, "seçmek bir şey KAZANDIRIYOR mu"dur. İlk
yarının şampiyonunu seçmek ile hiç seçmeyip **aile medyanını** almak, ikinci
yarıda karşılaştırıldı:

| enstrüman | seçilen | yarı2 Calmar | aile medyanı | fark |
|---|---|---|---|---|
| QQQC | MA 50/100 | +0,26 | +0,29 | **−0,03** |
| SPY | MA 50/150 | +0,11 | +0,12 | **−0,00** |
| IBM | MA 50/100 | −0,15 | −0,13 | **−0,02** |

Üçünde de fark sıfır ya da hafif negatif. **Şampiyon, gelecekte medyandır.**

Bu, istenen "ayırt edici ölçütün" yerine geçebilecek tek dürüst tasarımı da
veriyor ve o ayırt edici değil **söndürücü** bir ölçüttür: bir adayın
raporlanacak beklentisi kendi backtest sayısı değil, **ailesinin medyanıdır.**
Adayın kendi sayısı in-sample seçim gürültüsü içerir; medyan içermez.

## Tasarımın cevabı: ayırt eden ölçüt YAZILMAMALI

İstenen ölçüt tasarlanmadı — iyi bir fikir bulunamadığı için değil, **ölçeceği
büyüklük bu veride var olmadığı için.** Doğru ifade "kalıcılık tam sıfır" değil:
n≈30 ve parametreler bağlaşık olduğu için tek ölçümün güven aralığı geniş; ölçüm
**güçlü kalıcılığı** dışlıyor. Bir parametre seçicisinin seçim yanlılığını
yenmesi için tam da güçlü kalıcılık gerekir, o yüzden sonuç değişmiyor — ama
dürüst cümle "kullanılabilir kalıcılık yok". Bu bir başarısızlık değil, bir ölçüm
sonucu: `exposure_drawdown_evidence` bugünkü dar kapsamıyla (yalnız düşüş,
yalnız teşhis) doğru kapsamdaymış.

Kapsam bir tercih değil ölçüm sonucu olduğu için teste çivilendi:
`tests/test_low_frequency_has_its_own_evidence.py::test_the_diagnostic_deliberately_reports_only_the_stable_property`
— fonksiyonun gövdesi (docstring'siz, AST ile yeniden üretilmiş) Calmar
hesaplarsa test kırılır ve okuyucuyu bu ölçüme yönlendirir.

## Uygulandı: `family_median_expectation`

Söndürücü ölçüt `auto/robustness.py` içinde yaşıyor ve robustness paketinin
1.5. adımı olarak koşuyor (çok-sembol kısa devresinden SONRA, IS/OOS'tan önce).

Ne yapıyor: adayın blok tipleri, rolleri, mantığı ve pozisyon boyutlandırması
sabit tutulup **yalnız sayısal parametreleri** `BLOCK_CATALOG` şemasının
aralığından yeniden çekiliyor — enum'lar (yön vb.) adayın değerinde kalıyor,
çünkü yön değiştirmek aileyi değiştirir. 20 kardeş aynı mühürlü çerçevede
koşuyor, medyanları adayın kendi sayısının yanına yazılıyor.

Tasarım kararları ve gerekçeleri:

- **Aday SIRASI raporlanmıyor.** "20 kardeş içinde 3." satırı, yukarıda
  çürütülen çıkarımı davet ederdi. Rapor: medyan + IQR + kaç kardeşin
  ölçülebildiği. Teste çivili.
- **Aday, kardeşlerin arasına KATILIYOR** — sayısı aynı partiden geliyor. İlk
  hâli adayı ayrı bir çağrıyla koşturuyordu ve bu sessizce elma-armut kıyası
  üretiyordu: ölçüldü, aynı aday aynı çerçevede iki yürütme yolunda 0,164 ve
  0,15 medyan veriyordu. Fark projenin kendi iki yolundan geliyor — söndürücü
  ölçüt hiç devrede değilken de var: `pnl_pct` 3,127 vs 3,192, `sharpe` 25,42
  vs 0,56 — sonuncusu ayrı bir kusurdu ve düzeltildi, bkz.
  [[yillıklastirma_veriden_okunur_2026_08_21]].
  Aday partiye katıldıktan sonra söndürme miktarı iki yolda da aynı:
  −0,1215 ve −0,1237.
- **Hiçbir atlama sessiz değil.** Dev çerçeve, katalog dışı blok, yetersiz
  kardeş — hepsi sebebini ve KARAR VEREN sayıyı yazıyor. Yapısal bir test her
  `return None`'ın hemen önünde bir sebep bildirimi arıyor; ilk hâli `_skip`
  sayısını sayıyordu ve dişsizdi (mutasyon testiyle görüldü), komşuluk
  denetimine çevrildi.
- **Geçerlilik çeşitliliği yiyerek sağlanmıyor.** Projede hazır duran
  `_fix_fast_slow` geçersiz çifti sabit 10/40'a çeviriyor; kardeş üretiminde
  kullanılsaydı örneklem tek bir çifte çökerdi. Onun yerine `slow` önce, `fast`
  onun altından çekiliyor.
- **Sessiz daralma imkânsız**: `n_siblings` ve `n_valid` her zaman birlikte
  raporlanıyor. 8 ölçülebilir kardeşin altında medyan hiç yazılmıyor.
- **Maliyet ölçüldü ve sınırlandı**: tek backtest 1-DAY'de 0,27 sn, 1-HOUR'da
  1,30 sn — yani 20 kardeş 6-26 sn, dakikalar süren paketin yanında ihmal
  edilebilir. `FAMILY_MAX_BARS = 200.000` üstünde (1-DAKİKA kripto ≈ 1M bar)
  ölçüm atlanıyor.
- **v1 yalnız yerleşik bloklar**: custom blok farklı sandbox/kill semantiği
  taşıyor, `None` dönüyor.
- **Kapıya girmiyor.** Aile medyanı adayı ayırt etmez — ayırt EDEMEZ, ölçüldü.
  Yalnız adayın kendi sayısındaki seçim yanlılığını söndürür. Ölçülemezse
  `family` anahtarı hiç yazılmıyor (boş sözlük "ölçüldü ve boş çıktı" gibi
  okunurdu).

Gerçek veride söndürme miktarı (mühürlü eğitim çerçevesi, MA 50/100):

| enstrüman | adayın sayısı | aile medyanı | IQR |
|---|---|---|---|
| QQQC | +0,28 | **+0,16** | +0,14…+0,23 |
| SPY | +0,13 | **+0,11** | +0,08…+0,15 |
| IBM | −0,02 | **−0,04** | −0,06…+0,01 |

Testler: `tests/test_family_median_deflates_the_number.py`.

Genel ders, bu oturumun üçüncü tekrarı: **bir ölçütü tasarlamadan önce ayırt
edeceği farkın var olup olmadığını ölç.** Üç hipotez (ince zaman dilimi daha çok
gözlem verir, havuzlanmış alfa yavaş adayları kurtarır, kapı ulaşılamaz) bu
oturumda aynı disiplinle çürütüldü.

Kod tarafı: `auto/robustness.py::exposure_drawdown_evidence`,
`tests/test_low_frequency_has_its_own_evidence.py`.

## İlk canlı ateşleme: koşu `5e132203` (2026-08-21, QQQ.NASDAQ)

Ölçüt ilk kez gerçek bir AUTO koşusunda, tur 2'nin iki finalistinde çalıştı
(aday kardeşleriyle AYNI partide, `irange` tam çerçeve):

| finalist | TF | adayın Calmar'ı | aile medyanı | IQR | kardeş | sonra ne oldu |
|---|---|---|---|---|---|---|
| Volatility Breakout | 1-DAY | +0,50 | **+0,47** | +0,44…+0,49 | 16/20 | IS/OOS ✓ 0,75 · robustluk geçti · mühürlü holdout 4 işlem (5 gerekiyordu) → yayınlanmadı |
| Volume and RSI | 4-HOUR | +0,51 | **+0,41** | +0,24…+0,48 | 18/20 | IS/OOS ✗ 0,19 → elendi |

Tasarımın öngördüğü iki davranış da görüldü: adayın sayısı ailesinin sıradan
sayısına yakınsa (0,50 vs 0,47) yapı sahiden o kadar ediyor; aile medyanının
belirgin üstündeyse (0,51 vs 0,41, geniş IQR) o fazlalık seçimin payıdır — ve
aynı aday IS/OOS'ta aşırı-uyum etiketi aldı. Ölçüt kapı DEĞİL, ama kapının
vereceği kararı bir adım önce söyledi.

**Bulunan boşluk:** 👪 canlı satır, oturum artefaktı (`robustness-r2-c1.json.gz`,
tam `family` bloğu) ve bellek-içi `rob_scan_log` sayıları taşıyordu; ama
`/reports`'un birleştirdiği KALICI kayıt `robustness_log.jsonl` ve oturum
olayının `summary`'si taşımıyordu. Koddaki yorum "sayılar kalıcı kayda
geçiyor" diyordu — `web.shared.log_robustness` `family`'yi hiç okumuyordu.
Yorum bir vaatti, onu okuyan test yoktu. Kapatıldı: kalıcı kayıt tam bloğu
(`own/median/iqr/n_siblings/n_valid`; atlandıysa `null`, eski sürümde anahtar
yok — ikisi ayrı), oturum özeti `summary.family`, ilerleme tablosu
"Aile (kendi / medyan)" sütunu. Üç kopya da `tests/test_family_reaches_the_permanent_record.py`
ile çivili ve üçü de mutasyonla doğrulandı (koruma silinince test düşüyor,
geri alınca yeşil). `/reports` ekranı henüz okumuyor — veri artık orada, görünüm
ayrı iş.

Koşunun kendisi: 3 tur, 45 backtest, 30 fallback (`qwen2.5-coder:14b`
JSON/kod üretiminde düştü), kazanan yok (`winless_limit`) — bkz.
[[kapi_ucdan_uca_dogrulandi_2026_08_21]] kapı zinciri için.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[kapi_ucdan_uca_dogrulandi_2026_08_21]]
- [[yillıklastirma_veriden_okunur_2026_08_21]]
<!-- BACKLINKS:END -->
