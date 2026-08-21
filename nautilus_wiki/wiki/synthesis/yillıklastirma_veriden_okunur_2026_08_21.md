---
title: Yıllıklaştırma tabanı veriden okunur — sentetik enstrüman Sharpe'ı 45× şişiriyordu
type: synthesis
summary: Çağıran bir instrument vermediğinde backtest sentetik bir Bybit 1-DAKİKA enstrümanı kuruyor ve bu nesne yıllıklaştırmaya da giriyordu; günlük hisse barları 1-dakikalık kripto gibi yıllıklaştırılınca Sharpe sqrt(525.600/252) = 45,7 kat şişiyordu (gözlenen 45,22). Düzeltme: bar tipi bir şey söylemiyorsa veri söyler — aralık indeksin medyan farkından, 24/7 olup olmadığı hafta sonu barı oranından (hisse %0,0, kripto %28,4). Aktif bir kapı bozucu değildi çünkü sıralama skoru işlem başına Sharpe okuyor ve üretimdeki her çağıran instrument geçiriyor; instrument geçirmeyen çağıranlar için gizli bir tuzaktı.
key_concepts:
  - aile_ici_ayirt_edicilik_2026_08_21
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/aile_ici_ayirt_edicilik_2026_08_21.md
last_updated: 2026-08-21
---

# Yıllıklaştırma tabanı veriden okunur

Bu kusur, başka bir işin yan ürünü olarak ortaya çıktı: söndürücü ölçüt
([[aile_ici_ayirt_edicilik_2026_08_21]]) paralel ve sıralı yollarda farklı
medyanlar veriyordu. Farkın kaynağını ararken, ölçüt hiç devrede değilken tek
bir adayı iki yoldan geçiren bir kıyas yapıldı:

```
TEK ADAY, aynı barlar, söndürücü ölçüt DEVREDE DEĞİL
  pnl_pct   doğrudan 3,1271    havuz 3,1923
  max_dd    doğrudan -0,27391  havuz -0,27382
  sharpe    doğrudan 25,42     havuz 0,562      ← 45 kat
```

## Mekanizma

`run_composed_backtest`/`run_backtest`, çağıran bir instrument vermediğinde
**sentetik** bir Bybit 1-DAKİKA enstrümanı kuruyor (motor bir enstrüman ister).
Bu nesne yıllıklaştırma tabanına da giriyordu:

| yol | bar tipi | enstrüman | taban |
|---|---|---|---|
| varsayılan | 1-MINUTE-LAST | CurrencyPair | **525.600** |
| havuz | 1-DAY-LAST | Equity | **252** |

`sqrt(525.600/252) = 45,67` — gözlenen oran **45,22**. (Kalan fark pnl'in
kendisinin ~%2 ayrışmasından; ayrı ve önceden var olan mesele.)

Kod zaten benzer bir yarayı taşıyordu: `_periods_per_year` docstring'i "eski
kod koşulsuz 365 kullanıyordu, 1m veride Sharpe ~38× eziliyordu" diyor ve H610
notu trade-çözünürlüklü eğriye bar-frekanslı yıllıklaştırma uygulamanın Sharpe'ı
~725× şişirdiğini anlatıyor. Aynı sınıf, üçüncü kez.

## Düzeltme: otorite veridir

Bar tipi bir şey söylemiyorsa **veri söyler.** İki büyüklük de seriden okunuyor:

- **aralık** → indeksin medyan farkı
- **24/7 mi** → hafta sonu barı oranı. Ölçüldü: QQQC 1-DAY ve 1-HOUR **%0,0**,
  BTCUSDT 1m **%28,4** (≈2/7). Ayrım temiz.

Çağrı yerleri artık sentetik `active_*` yerine **çağıranın verdiklerini**
geçiriyor. Sonuç:

| çerçeve | önce | sonra |
|---|---|---|
| QQQC 1-DAY (hisse) | 525.600 | **252** |
| QQQC 1-HOUR (hisse) | 525.600 | **1.764** |
| BTCUSDT 1m (kripto) | 525.600 | 525.600 *(değişmedi)* |
| gerçek instrument ile | 252 | 252 *(değişmedi)* |

Sharpe: **25,42 → 0,5566** (havuz yolu 0,5622, hep doğruydu).

İki güvenlik freni: iki haftadan kısa kapsamda hafta sonu çıkarımı yapılmıyor
(eski varsayım korunuyor — sessiz bir "hisse" tahmini tabanı yanlışlıkla 252'ye
düşürürdü), ve enstrüman BİLİNİYORSA veri tahmini hiç devreye girmiyor.

## Şiddet: düzeltilirken küçültüldü

İlk teşhis "sıralama skorunu bozuyor" idi ve **yanlıştı.** `_score` işlem başına
Sharpe okuyor (`sharpe_per_trade`, `√n` ölçekli) ve onu `_periods_per_year` hiç
etkilemiyor; üstelik üretimdeki her sıralama/robustluk çağıranı zaten instrument
geçiriyor. Yani aktif bir kapı bozucu değildi — instrument geçirmeyen çağıranlar
(`capture_baseline.py`, legacy sayfalar, testler) için **gizli** bir tuzaktı.
`capture_baseline.py` tam o yoldan geçtiği için kayıtlı taban çizgileri şişik
Sharpe taşıyor olabilir; yeniden hesaplanmaları ayrı bir karar.

Genel ders: **bir arıza raporunun şiddeti de ölçülmeli.** "Kapıyı bozuyor"
demek kolay ve yanlış olduğunda düzeltmenin aciliyetini de, kapsamını da
çarpıtır.

Testler: `tests/test_annualization_comes_from_the_data.py` — metrik eşitliğini
değil YILLIKLAŞTIRMA TABANINI çiviliyor. İkisini karıştırmak, düzeltilmemiş
pnl farkını bu testin sırtına yıkardı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[aile_ici_ayirt_edicilik_2026_08_21]]
<!-- BACKLINKS:END -->
