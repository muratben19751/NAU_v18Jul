---
title: US-Equity Katalog Veri Bütünlüğü
type: synthesis
sources:
  - sources/04_backtesting_docs.md
  - sources/06_concepts_docs_v1230.md
last_updated: 2026-08-11
summary: 16 ticker'lık equity kataloğunda ölçülmüş altı veri kusuru (pre-market sızıntısı, low=0.00 barı, DST'de kırılan 4-HOUR, manifest çelişkisi, kapsam deliği, onarımda UTC/ET gün sınırı kayması) ve ingest_equities.py'deki kalıcı düzeltmeleri.
key_concepts:
  - parquet_data_catalog
  - bar_aggregation_and_type_syntax
  - dst
  - instruments
  - index_backtest_via_equity_proxy
---

# US-Equity Katalog Veri Bütünlüğü

2026-08-10 denetimi `C:\Users\MYDESK\.cache\nautilus_web_app\equity_catalog` (16
ticker, 1-MINUTE→1-DAY) üzerinde beş somut kusur ölçtü. Hepsinin ortak özelliği
**sessizce yanlış sonuç üretmeleriydi**: hiçbiri hata vermiyor, backtest çıktısı
makul görünüyordu. Düzeltmeler `ingest_equities.py`'de; okuma tarafı `data.py`.

## 1. Right-label sözleşmesi ve pre-market sızıntısı

Bu repoda bar `ts`'i **kapanıştır** (`window_start + 60e9`). Sonuç, ilk bakışta
görünmeyen bir tuzak: **"09:30" damgalı bar 09:29–09:30 dilimidir, yani
pre-market'tir.** Seansın ilk barı `09:31` damgalıdır.

`between_time("09:30", "16:00")` bu yüzden seans başına 390 yerine **391** bar
alıyordu ve sızan pre-market dakikası günlük/TF barın **açılışı** oluyordu.
Ölçüm: 5.738 seansın 4.983'ünde (%86,8) open hatalı, ortalama 3,2 bp, maks 116 bp.

Düzeltme: `RTH_FIRST_LABEL = "09:31"`. Sabitin yanındaki yorum "geri almayın"
uyarısı taşır — bu satır ileride yazım hatası sanılıp düzeltilecek türdendir.

## 2. `low = 0.00` bozuk barı

Ham kaynakta gerçek bir bar: `2004-07-28 12:29 ET`, `open=33.85 high=33.86
low=0.00 close=33.85`. Agregasyonla saatlik ve günlük bara yayılmış (QQQ ve
QQQC). `bar_adaptive_high_low_ordering` açıkken o seansta **her stop-loss
tetikleniyordu** — tek bir hücre bütün bir seansın backtest'ini çöpe atıyor.

`_sanitize_ohlc` dört kusuru tek kuralla kapatır (`high = max(o, c, high)`,
`low = min(o, c, low)`): pozitif-olmayan high/low, `high < low`,
`high < max(o,c)`, `low > min(o,c)`. Onarılamaz satır (open/close pozitif değil)
**düşürülür**. Dokunulan satır sayısı loglanır — sessiz onarım yok. Hem yeni
ingest hem TF türetme yolunda çalışır.

## 3. 4-HOUR ve DST

`resample("240min")` tz'li indekste **mutlak** zaman kovaları kullanır. 240 dk ne
saatin ne günün bölenidir, dolayısıyla kovalar seansa değil takvime oturur:
EDT'de seans başına 2 bar (12:00, 16:00), **EST'de 3 bar** (11:00, 15:00 ve
19:00). 19:00 barı borsa kapandıktan 3 saat sonra damgalıydı ve içinde yalnız
15:00–16:00 verisi vardı; ilk bar 1,5 saatlikti.

Düzeltme (`resample_tf` + `_session_bucket_labels`): 4-HOUR **seans-göreli**
gruplanır — kova indeksi seans açılışından geçen dakikaya göre, etiket kova sonu
(right-label korunur). RTH 390 dk olduğu için seans başına tam **2 bar** çıkar:
240 dk + 150 dk. İkinci bar kısadır ama her seansta aynıdır. Yarım günlerde
etiket, seansta gerçekten var olan son bara kırpılır.

**5/15/60 dakika ve 1-DAY bilinçli olarak `resample`'da kalır**: bunlar saatin
(günün) tam böleni oldukları için NY yerel saatinde hizalı kalır ve DST'den
etkilenmez. Ayrıca "1-DAY = 1-HOUR'un tam agregası" özelliği doğrulanmıştır.

## 4. Manifest: split ≠ temettü

Tek bir `adjusted` bayrağı iki ayrı gerçeği karıştırıyordu. Polygon/Massive
`/v2/aggs adjusted=true` **yalnız split ayarlar**, temettüyü asla ayarlamaz —
ama aynı bayrakla yazılan notlar bir partide "split/temettü ayarlı", diğerinde
"yalnız split ayarlı" diyordu ve çelişki UI'ya hiç yansımıyordu (`data.py` notu
okumuyordu).

Yeni şema: `split_adjusted` ve `dividend_adjusted` ayrı boolean'lar; eski
`adjusted` alanı `split_adjusted` ile eşitlenerek korunur. `migrate_manifest`
eski manifestleri göç ettirir ve çelişkili notları gerçeğe uydurur; kaynağı
bilinmeyen girdide `dividend_adjusted` **None (bilinmiyor)** kalır — uydurulmaz.
`data.py:_external_adjustment_flags` iki alanı ayrı döndürür.

Not: benchmark'a temettü getirisi eklemek ayrı bir iştir. Veri katmanının işi
`dividend_adjusted=false` bilgisini **okunabilir** kılmaktır.

## 5. Kapsam boşlukları

QQQ kendi serisinde 2004-12-01 → 2011-03-22 arası boş (o dönem ticker QQQQ'ydu):
**1.588 seans**, 2008 ayı piyasasının tamamı. Al-tut maxDD %53,5 yerine %35,6
görünüyor. (Dikilmiş `QQQC` temiz — bkz. `stitch_ticker.py`.)

`compute_coverage_gaps` 1-DAY serisinden hesaplar. Referans takvim ayrı bir borsa
takvimi değil, **kataloğun kendisidir**: bütün ticker'ların günlük bar
tarihlerinin birleşimi zaten gerçek işlem günleri kümesidir (tatiller
kendiliğinden dışarıda kalır, ek bağımlılık yok). Boşluk = ticker'ın kendi
ilk/son tarihi arasında referansta olup kendisinde olmayan ≥10 ardışık seans;
serinin başından öncesi kapsam sınırıdır, boşluk değil.

Sembol listeden **çıkarılmaz** — kullanıcı bilerek seçebilmeli, ama boşluğu
görmeli. `list_external_instruments` kaydında `coverage_gaps` alanı taşınır.

## 6. Onarımda UTC/ET gün sınırı kayması

Right-label sözleşmesinin ikinci tuzağı, 3'ünkinden farklı ve daha sinsi: **bir
seansın 1-DAY barı o seansın UTC gününde değildir.** `resample("1D",
label="right", closed="right")` NY yerel saatinde `(D 00:00, D+1 00:00]` kovasını
üretir ve barı **`D+1 00:00 New York`** ile damgalar — DST'ye göre `04:00` ya da
`05:00` UTC, yani D'nin UTC takvim gününün dışında.

`repair_massive_intraday._replace_day` silinecek pencereyi naif olarak
`[D 00:00 UTC, D+1 00:00 UTC)` hesaplıyordu. Bu pencere D'nin kendi günlük barını
**ıskalar** ve onun yerine `D 00:00 NY` damgalı **bir önceki seansın** barını
yakalar. Tek bir günü onarmanın iki sonucu vardı:

- **komşu gün siliniyor** — D-1'in günlük barı katalogdan düşüyor (kapsam deliği
  olarak görünür),
- **onarılan gün ikizleniyor** — D'nin eski (bozuk) barı yerinde kalıyor, üstüne
  bir de yenisi ekleniyor; aynı seans için iki bar.

Gün içi TF'ler tesadüfen etkilenmiyordu (09:31–16:00 NY damgaları her iki DST
rejiminde de aynı UTC gününe düşer) — kusur yalnız 1-DAY'de görünür, bu yüzden
gün içi testler yeşil kalarak hatayı gizliyordu.

Düzeltme (`ingest_equities.session_label_bounds_ns`): pencere UTC takviminden
değil **NY seansından** türetilir ve kovanın tam kendisidir — solda açık
(`D 00:00 NY`, D-1'in damgası; ona dokunulmaz), sağda kapalı (`D+1 00:00 NY`,
D'nin kendi barı). Sınırlar yerel gece yarılarından üretildiği için
DST-farkındalıklıdır; ters yönü (`damga → seans günü`) `_daily_session_dates`
yapar, ikisi aynı sözleşmenin iki yüzüdür.

Fonksiyon bilerek `ingest_equities`'te durur ve onarım aracı onu **import eder**:
damgalama sözleşmesinin iki ayrı kopyası olsaydı ıraksama yine onarılan günü
komşularından farklı yazardı — bu sayfadaki 3 ve 6 numaralı kusurların ortak
sınıfı tam budur.

**Mevcut katalog etkilenmemiştir** (2026-08-11 taraması): 16 ticker'ın hiçbirinde
ikizlenmiş seans ya da tek günlük delik yok; QQQ'nun 1.588 seanslık boşluğu 5'te
anlatılan QQQQ dönemidir. `.bak` dosyası da yok, yani onarım gerçek katalogda hiç
kuru-olmayan modda koşmamış.

## Mevcut veriyi yeniden üretme

1/2/3 numaralı düzeltmeler mevcut TF barlarını geçersiz kılar, ama 1-MINUTE
barlar katalogda durur — 78 GB'lık flat-file arşivini yeniden taramaya gerek
yoktur:

```
python ingest_equities.py --rebuild-tf      # arşive hiç dokunmaz
python ingest_equities.py --migrate-manifest --coverage-gaps
```

`--rebuild-tf` hedef verilmezse katalogda 1-MINUTE'ü olan her ticker'ı yeniden
türetir, sonra `coverage_gaps`'i tazeler.

## 7. İndirme mevcut veriyi indirmeden ÖNCE siliyordu (2026-08-11)

`download_massive.download_minute_bars` ilk API çağrısından önce hedef
ticker'ların tüm bar dizinlerini `rmtree` ediyordu. Ağ hatası, plan penceresi
dışı yıl (`NOT_AUTHORIZED` — bu modülün beklediği normal bir durum) veya kota
tükenmesi hâlinde silme çoktan olmuş oluyor, koşum "hiçbir ticker'da bar yok"
deyip çıkıyor ve önceden başarıyla ingest edilmiş veri kalıcı olarak
kayboluyordu. Kök aynı zamanda web uygulamasının okuduğu
`data.EQUITY_CATALOG_DIR` olduğu için enstrüman /data panelinden, Lab/Studio
picker'larından ve o sembolü kullanan kayıtlı stratejilerden düşüyordu.

Yeni akış **staging + atomik takas**: barlar katalogun DIŞINDA kardeş bir
staging köküne yazılır, ticker başarıyla ve en az bir bar'la bittiğinde eski
dizinler `data/bar` dışına taşınır ve yenileri yerine konur; takas yarıda
kalırsa eskiler geri konur. Yedeği `data/bar` içinde `.bak` adıyla tutmak
olmazdı: `list_external_instruments` dizin adlarını `rsplit("-", 4)` ile
ayrıştırıyor ve yedek SAHTE bir enstrüman olarak panele düşerdi.

## 8. Katalog geç bağlanmıyordu, manifest önbelleği hiç geçersizleşmiyordu (2026-08-11)

`EQUITY_CATALOG_DIR` yalnız **modül import anında** `EXTERNAL_CATALOGS`'a
ekleniyordu: kökü ilk kez bir CLI ingest'i oluşturursa çalışan sunucu onu asla
görmüyor, restart gerekiyordu. Daha incesi, `_external_instrument_meta` ve
`_external_manifest` sonuçlarını süresiz tutuyordu ("read-only reference data,
so it never changes" — bu varsayım NAU_ev kökü için doğru, projenin KENDİ
ingest'inin yazdığı kök için yanlış). Sonuç tutarsız bir tabloydu: yeni ticker
picker'da görünüyor (dizin taraması her çağrıda yapılıyor) ama
`split_adjusted`/`coverage_gaps` bayat manifest'ten okunuyor — yani **UNADJUSTED
uyarısı tam da yeni gelen veri için susuyordu**.

Şimdi `data.external_catalog_roots()` kökü her çağrıda soruyor (tek `stat()`) ve
her iki önbellek `(mtime, size)` imzasıyla geçersizleşiyor —
`composer._read_catalog_raw` ve `custom_block_store._read_registry` ile aynı
desen. Ayrıca **okuma hatası artık önbelleğe yazılmıyor**: açılıştaki tek bir
dosya kilidi, süreç ömrü boyunca "bu enstrümanın metası yok" demeye
dönüşüyordu; `composer.load_catalog`'un dersi — okunamıyorsa cevap
"bilinmiyor"dur, "boş" değil.

## 9. Onarımda `precision` ölü parametre değil, yanlış parametreydi (2026-08-11)

`repair_massive_intraday._fixed(values, precision)` imzası fiyat için 2, hacim
için 0 alıyor ama gövdede `precision` bir kez bile okunmuyordu; ölçek daima
1e9'du. Nautilus'un sabit-nokta gösterimi gerçekten 1e9 olduğu için fiyatlarda
fark görünmüyordu, ama hacimde görünüyordu: `Quantity(175.4, 0)` Nautilus'ta
`175_000_000_000` raw üretirken bu fonksiyon `175_400_000_000` yazıyordu — yani
onarılan gün katalogdaki komşularından FARKLI bir sözleşmeyle kodlanıyordu, ki
bu modülün varlık sebebi tam olarak bunun tersi. `precision` artık ölçeklemeden
ÖNCE uygulanan yuvarlamayı belirliyor ve yuvarlama Nautilus'un Rust tarafıyla
aynı (yarım sıfırdan uzağa; Python'un banker's rounding'i 10.125'i 10.12 diye
kodlardı, Nautilus 10.13 yazar). Test doğrudan `Price`/`Quantity`'nin `raw`
değerine karşı parite kuruyor.

Aynı turda `_replace_day`'in okuma handle'ı `with pq.ParquetFile(...)` ile
deterministik kapatıldı: Windows'ta `path.replace(backup)` dosya hâlâ açıkken
`PermissionError` verir ve onarım tam da yedeği alırken yarıda kalırdı.

## Aynı sözleşmeyi paylaşan modüller

`download_massive.py` (REST), `stitch_ticker.py` (dikiş) ve
`repair_massive_intraday.py` (tek gün onarımı) aynı `write_manifest` /
`resample_tf` / `_sanitize_ohlc` / `session_label_bounds_ns` yüzeyini kullanır.
Onarım yolunun ayrı bir resample kopyası tutması, onarılan günün katalogdaki
komşularından farklı bir sözleşmeyle yazılması demekti — bu yüzden ortak
yardımcıya bağlandı. Aynı gerekçe gün sınırı hesabı için de geçerlidir (bkz. 6):
**damgalama sözleşmesine dair her karar tek bir modülde durmalı.**

Bkz: [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]], [[dst]],
[[instruments]], [[index_backtest_via_equity_proxy]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[index_backtest_via_equity_proxy]]
- [[nau_deepr_dorduncu_tur_2026_08_11]]
<!-- BACKLINKS:END -->
