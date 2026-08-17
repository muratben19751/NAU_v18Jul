---
title: Çok-sembol genellemesi — peer seçimi ve üstünlük ölçütü
type: synthesis
summary: Robustness'ın EN UCUZ kapısı: aynı spec akran sembollerde koşar, geçemezse IS/OOS+WFO+MC hiç çalışmaz. Peer seçimi dikiş-farkındalıklı dışlama + katalog venue'suna göre çözümleme + PEER_SAMPLE_SIZE kırpması; üstünlük ölçütü ana benchmark kapısıyla ORTAK (Calmar üstünlüğü + kârlılık tabanı).
sources:
  - sources/09_baglam_ve_butce_olcumu_2026_08_16.md
  - https://github.com/muratben19751/NAU_v18Jul
key_concepts:
  - auto_mission_control
related:
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
  - wiki/synthesis/auto_arama_ekonomisi.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/nau_bulgu_kapatma_turu_2026_08_17.md
last_updated: 2026-08-17
---

# Çok-sembol genellemesi

`backtest_robustness.run_multi_symbol`, aynı spec'i akran sembollerde aynı zaman
penceresinde koşturur ve "bu strateji bu enstrümana mı özel?" sorusunu sorar.
Robustness zincirinin **ilk ve en ucuz** halkasıdır: kesin bir ret burada çıkarsa
IS/OOS, Walk-Forward ve Monte Carlo hiç çalıştırılmaz
(`multi_symbol_definitive_failure` → `skipped after definitive multi-symbol
rejection`). Kapının tasarım felsefesi için bkz. [[auto_kapi_ve_geri_bildirim]].

## Peer seçimi üç süzgeçten geçer

Sıra önemli; her adım ayrı bir hatanın izidir.

**1 · Dikiş-farkındalıklı dışlama** (`peer_exclusions`). Düz `p != symbol`
karşılaştırması dikilmiş serileri tanımıyordu: QQQC, `stitch:QQQ+QQQQ` ile
üretilmiş sürekli bir seri olduğu için bir QQQC koşusunda QQQ'yu peer seçmek
"başka enstrümanda da çalışıyor mu" diye sormuyor — **aynı veriyi ikinci kez**
soruyor. Genelleşebilirlik testi diye görünen şey testin kendisinin tekrarıydı
(DeepR 2026-08-10). Dışlama iki yönlü: dikiş bileşenlerini, bileşen de kendisini
içeren dikişi dışlar.

**2 · Venue çözümlemesi** (`resolve_peer_ids`, 2026-08-16). Sepet gerçek dünyanın
venue'sunu yazıyordu — `SPY.ARCA`, `IWM.ARCA`; piyasa gerçeği doğru, ikisi de NYSE
Arca'da listeli. Ama ingest her enstrümanı kendi damgasıyla yazıyor ve ölçülen
kutuda 16 enstrümanın 16'sı `.NASDAQ`. Eşleşmeyen id, aşağıdaki veri filtresinden
**sessizce** düşüyordu: beş peer'lık sepet fiilen üçle, QQQC koşusunda **ikiyle**
karar veriyordu ve hiçbir yerde uyarı yoktu. Eşleştirme bu yüzden venue'ya değil
**bare ticker**'a bakar; katalog okunamazsa sepet değişmeden döner (bir peer
listesi uğruna robustness çökmez).

**3 · Veri filtresi + kırpma.** O granülaritede verisi olan peer'lar seçilir, SONRA
`PEER_SAMPLE_SIZE` (5) kadarı alınır. Ters sıra bir kez yazıldı ve ilk üç peer'ın
verisi yoksa dördüncü/beşinci hiç denenmiyordu.

`EXTERNAL_PEER_BASKET` 7 girdi taşır (3 endeks ETF + 4 mega-cap) — QQQC gibi üç
ticker'ı birden dışlayan bir seride bile tam örneklem kalsın diye yedekli. Havuz
hedefin altında kalırsa koşu logu bunu **yazar**; sessiz daralma tam olarak bu
kapının 5 peer sanılırken 2 ile karar vermesine yol açmıştı.

## Örneklem boyutu eşiğin ÇÖZÜNÜRLÜĞÜDÜR

Etiket eşikleri `pass_rate` üzerinden: `>= 0.7` → ✓ Generalizable, `>= 0.4` →
⚠ Limited, altı → ✗ Symbol specific. İki peer'la olası değerler yalnız 0 / %50 /
%100'dür, yani 0,7 pratikte **2/2 zorunlu** demekti ve ara bant tek sembollük
gürültüyle belirleniyordu. Örneklem küçüldükçe kapı sertleşmiyor,
**kararsızlaşıyor** — tek zayıf akran adayı komple eliyordu. Beşte 4/5 geçer,
3/5 "Limited" olur.

Geçerlilik ayrı bir süzgeç: `n_trades >= 5` ve ölçülmüş bir karşılaştırma bacağı.
Eksik piyasa verisi, geniş bir ralli sırasında nominal kâr edildi diye sessizce
geçişe dönüşmemeli.

## Üstünlük ölçütü ana kapıyla ORTAK

`peer_is_superior` (2026-08-16, kullanıcı kararı). Varsayılan `risk_adjusted`
modunda asıl ölçü **Calmar üstünlüğü**; alfanın pozitif olması şart değil, ama
**taban** durur: `strategy_cagr > 0` — para kaybeden bir strateji, düşüşü küçük
diye üstün sayılmaz. Anahtar ana kapıyla ortaktır: `AGENT_BENCHMARK_GATE`
(`risk_adjusted` | `absolute`).

**Kural artık TEK KOPYA** — `app_constants.benchmark_rejection` (kod incelemesi,
2026-08-16). Bu kural iki kez kopyalandı ve iki kez ıraksadı. Önce **ölçüt**: ana
kapı 2026-08-15'te risk-ayarlıya çekilirken çok-sembol kapısı terk edilen mutlak
kuralda kaldı. Sonra, ölçüt hizalandığında, **geri düşme basamağı**: Calmar
ölçülemediğinde ana kapı `annualized_alpha`'ya düşerken çok-sembol kapısı
`excess_return_fraction`'a düşüyordu — damgalayıcısının "karar ölçütü olamaz"
dediği sayı (büyüklüğü pencere uzunluğuna bağlı, brüt al-tut'a karşı net
strateji). Sonuç: aynı geçişte bir peer kümülatif farkla, kardeşi Calmar'la
yargılanıp tek bir `pass_rate` paydasında toplanabiliyordu.

Bağımlılık yönü paylaşımı yaprak modüle zorladı: `agent_backtest` zaten
`backtest_robustness`'ı içeri alıyor, ters yön döngü olurdu — ikisinin de içeri
aldığı `app_constants` doğal ev. Mod da orada ve **çağrı anında** okunuyor;
import-anı bir sabit, "birini yeniden yükle, diğerini yükleme" gibi sessiz bir
ıraksama yüzeyiydi. Ölçü basamakları (yukarıdan aşağı): Calmar üstünlüğü +
kârlılık tabanı → yıllık alfa → (yıllıklandırma hiç yoksa) kümülatif fark.
Testte `test_both_gates_return_the_same_verdict_for_the_same_metrics` iki kapıyı
aynı sözlükle karşılaştırır: ikinci bir kopya açılırsa orada kırılır.

Neden değişti: ana kapı 2026-08-15'te risk-ayarlıya çekilmişti, çok-sembol kapısı
o değişikliğin dışında kalmıştı. Bedeli ölçüldü — koşular `392287b2` ve
`38bdfeff`, iki farklı strateji ailesi (ADX ve EMA/MACD): **dokuz adayın dokuzu da
ortalama Sharpe'ı pozitifken 0/N ile elendi.** Peer'ların al-tut getirisi %23-118
arasındaydı; long-only bir strateji piyasadan zaman zaman çıktığı için mutlak
getiride kaybeder. `38bdfeff` tur 1'de IWM'de strateji **+1.202 kazandı, Sharpe
0,69** — al-tut %48,2 yaptığı için "başarısız" yazıldı.

Sonuç satırları bu yüzden `strategy_calmar` / `benchmark_calmar` / `strategy_cagr`
/ `annualized_alpha` **taşır**: kararı veren fonksiyon yalnız satırı görüyor,
alanlar taşınmasa kapı sessizce bir alt basamağa düşer ve düzeltme hiçbir yerde
hata vermeden etkisiz kalırdı. `annualized_alpha` tam olarak o geri düşme
basamağı — damgalayıcıda Calmar'DAN ÖNCE yazıldığı için gerçek bir satırda
"Calmar var ama alfa yok" hâli oluşamaz. İlerleme satırındaki `✓/✗` ikonu da kapının gerçek ölçütünü gösterir —
eskiden excess'e bakıyordu, yani kapı değiştikten sonra ekranda "✗" yazan bir peer
skorda geçmiş olabilirdi.

## Sahada doğrulandı — kapı sonucu DEĞİŞTİRDİ

Koşu `da461e3e` (2026-08-16, commit `57077c5`, QQQC.NASDAQ, 4-HOUR), tur 1'in
en iyi adayı `QQQ Channel Breakout`. Beş peer'ın **beşinde de excess NEGATİF**:

| peer | excess | Sharpe | Calmar (str/bench) | işlem | ikon |
|---|---|---|---|---|---|
| SPY | −%21,3 | 1,15 | 1,36 / 1,16 | 2 | ✓ |
| IWM | −%32,6 | 0,59 | 0,68 / 0,77 | 8 | ✗ |
| AAPL | −%27,7 | 0,65 | 0,75 / 0,66 | 7 | ✓ |
| MSFT | −%14,8 | 0,39 | 0,34 / 0,31 | 11 | ✓ |
| NVDA | −%80,4 | 0,97 | 0,89 / 1,26 | 14 | ✗ |

Eski ölçütle (`excess > 0`) bu tablo **0/4 → ✗ Symbol specific** olurdu ve
`multi_symbol_definitive_failure` ("✗" arar) IS/OOS + WFO + MC'yi hiç
çalıştırmazdı. Risk-ayarlı ölçütle **2/4 → ⚠ Limited**: kesin ret değil, zincir
devam etti ve aday IS/OOS'tan **✓ Robust** ile çıktı. Yayım yine olmadı ama
sebebi başka ve meşru: mühürlü holdout'ta 0 işlem (20 gerekiyor) — kapı
stratejinin kanıtını istedi, veri penceresinin yan etkisini değil.

**Ekranla sayacın çeliştiği yer kapatıldı.** Bu tabloda SPY ✓ yazıyor ama 2
işlemle `n_trades >= 5` süzgecinden düştüğü için paydaya girmiyordu: okuyan
ekranda üç ✓ sayıyor, özette "2/4" görüyor ve ikisini bağdaştıramıyordu. İkon
"bu peer üstün mü", sayaç "GEÇERLİ peer'ların kaçı üstün" sorusuna cevap
veriyor — ikisi de tek başına doğru, sebep yazılmayınca birlikte yanıltıcı.
Artık elenen satır sebebini taşıyor ve özet paydayı açıklıyor:

```
[SPY.NASDAQ] ✓ … · Calmar=1.36/1.16 · 2 trade ⊘ sayılmadı: 2 işlem < 5
Multi-symbol completed · 2/4 symbols positive alpha · pass_rate=50% · ⚠ Limited · 1 sayılmadı
```

Geçerlilik ölçütü de tek kopya oldu (`peer_exclusion_reason`): ekrana basılan
sebep ile `valid` süzgeci aynı fonksiyondan okuyor. İkiye ayrıldığı an ekran ile
sayaç yeniden ıraksardı — bu satırın var oluş sebebi zaten o ıraksamaydı.
Artefakt da `symbols_excluded` taşıyor, yani `tested − valid` farkı sessiz bir
eksiltme değil ölçülmüş bir eleme.

Testler: `tests/test_multi_symbol_generalization.py` — etiket eşikleri, dikiş
dışlama, venue çözümlemesi, sepet yedekliliği, ve üstünlük ölçütünün dört hâli
(negatif excess'e rağmen Calmar geçişi, kârlılık tabanı, fail-closed geri düşme,
absolute modu).


## Sepet nominal 7, etkin ~2 (ölçüldü 2026-08-17)

Peer sayısı bir güven ifadesidir: `pass_rate >= 0.7` "beş bağımsız testin dördü
geçti" diye okunur. Sepetin İÇ korelasyonu ölçülünce o okuma tutmuyor —
`equity_catalog`'taki günlük serilerle, log-getiri korelasyon matrisinin
özdeğerlerinden `(Σλ)²/Σλ²`:

| sepet | gün | ort ρ | PC1 | etkin / nominal |
|---|---|---|---|---|
| 7'li (tam) | 497 | 0,54 | %62 | **2,37 / 7** |
| GOOGL'suz 6 | 4.173 | 0,63 | %70 | **1,95 / 6** |
| QQQ'suz 5 | 5.761 | 0,58 | %67 | **2,07 / 5** |
| yalnız üç ETF | 4.173 | 0,82 | %88 | **1,27 / 3** |
| yalnız üç mega-cap | 5.761 | 0,46 | %64 | **2,11 / 3** |

22 yıllık pencerede sonuç değişmiyor. SPY↔QQQ **0,95** — ikisi pratikte aynı
seri, ve `PEER_SAMPLE_SIZE = 5` üç ETF'i birlikte seçerse etkin sayı 1,3'e
iniyor. Yani bir strateji "beş sembolde genelleşti" derken çoğunlukla TEK
piyasa faktörüne beş kez sorulmuş oluyor.

İki veri kusuru aynı ölçümden çıktı:

* **GOOGL'ın yalnız 498 günlük barı var** (2024-08-06'dan), diğerlerinin 5.762
  (2003'ten). Yedi sembol birlikte istendiğinde ortak pencere 22 yıldan 497
  güne düşüyor — sepet sessizce heterojen.
* **QQQ 4.174 bar** ve yükleyici `split suspicion: 1 bar |getiri|>%40
  (2011-03-23) — seri düzeltilmemiş olabilir` uyarısı veriyor. Uyarı var ama
  sembolün sepete girmesini engellemiyor.

Survivorship (sepet bugün likit olanlardan seçili, düşen isimler hiç test
edilmiyor) hâlâ geçerli ve artefakta beyan olarak yazılı
(`peer_basket_selection`, `peer_survivorship_note`); ölçmek nokta-zaman evren
verisi ister. Ama ETKİN BAĞIMSIZLIK ölçülebilir çıktı ve daha bağlayıcı.
Seçenekler: sepeti düşük korelasyonlu isimlerle genişletmek, `pass_rate`'i
etkin sayıyla ağırlıklandırmak, ya da en ucuzu — `effective_symbols`'ü
artefakta ve ekrana yazıp etiketin ne kadar bilgi taşıdığını görünür kılmak.

## Karar verildi: üçüncüsü (2026-08-17)

`backtest_robustness.effective_symbol_count` — katılım oranı `(Σλ)²/Σλ²`,
korelasyon matrisinin özdeğerlerinden; yalnız `valid` sembollerle hesaplanır
(payda oysa, bağımsızlık da onun olmalı). Adım satırında
`etkin bağımsız sembol ≈ 2.3/5`, artefaktta `effective_symbols`
(`None` = ölçülemedi — sıfır ya da nominal DEĞİL).

**TEŞHİS, karar değil**: etiket eşikleri yalnız `pass_rate` okur ve bir test
karar bloğunda `effective` geçmediğini çiviliyor. Ağırlıklandırma seçilmedi,
çünkü teşhis eklerken kapıyı oynatmak ölçmek için ölçtüğünü bozmaktır.

Ölçüm koşu 755b7880'in KENDİ penceresinde yenilendi (son 730 gün = 502 işlem
günü, 1-DAY):

| sepet | nominal | etkin | PC1 |
|---|---:|---:|---:|
| koşuda kullanılan 5'li (SPY·IWM·AAPL·MSFT·NVDA) | 5 | **2,32** | %60 |
| GOOGL'sız 6'lı (QQQ dahil) | 6 | 2,17 | %65 |
| tam sepet 7'li | 7 | 2,37 | %62 |

Sezgiye aykırı sonuç: sepeti GENİŞLETMEK bağımsızlığı düşürüyor. Eklenenler ya
artık (QQQ↔SPY = **0,95**) ya da veri açısından sakat (GOOGL 498 bar, ötekiler
5.762 — sepete girdiği anda ortak takvimi 729'dan 497 güne indiriyor).
Bağımsızlık sayıyla değil ÇEŞİTLE artar: farklı sektör, varlık sınıfı, ABD dışı.

Sayı PENCEREYE bağlıdır (aynı sepet 729 işlem günlük pencerede 2,42), o yüzden
sabit bir "sepet katsayısı" olarak saklanmaz — her koşuda testin kendi
penceresiyle yeniden hesaplanır.

Canlı davranış: [[nau_auto_kosusu_755b7880_2026_08_17]] — iki aday
`⚠ Limited` (2/5 ve 3/5) ile zincire devam etti, biri `✗` almadı.
Bkz. [[nau_bulgu_kapatma_turu_2026_08_17]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_auto_kosusu_755b7880_2026_08_17]]
- [[nau_bulgu_kapatma_turu_2026_08_17]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
