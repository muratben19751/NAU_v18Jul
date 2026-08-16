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
last_updated: 2026-08-16
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

Testler: `tests/test_multi_symbol_generalization.py` — etiket eşikleri, dikiş
dışlama, venue çözümlemesi, sepet yedekliliği, ve üstünlük ölçütünün dört hâli
(negatif excess'e rağmen Calmar geçişi, kârlılık tabanı, fail-closed geri düşme,
absolute modu).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
