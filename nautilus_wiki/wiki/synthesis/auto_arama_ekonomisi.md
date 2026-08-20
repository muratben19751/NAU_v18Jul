---
title: AUTO aramasının ekonomisi — pozisyon boyutu, komisyon, eleme eşikleri
type: synthesis
summary: AUTO döngüsünün "hiçbir aday geçemiyor" sonucu stratejilerden değil boyutlandırmadan geliyordu; bir clamp her hisse backtest'ini 1 hisseye kilitliyor ve sabit IBKR komisyonunu işlem başına %0,91'lik vergiye çeviriyordu. Düzeltme percent_equity; ölçümde kaybeden 10 adayın 8'i yalnız pozisyon çarpanıyla kâra geçiyor.
key_concepts:
  - auto_mission_control
  - backtesting_guide
sources:
  - sources/08_hibrit_kosu_olcumleri_2026_08_16.md
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/nau_performans_denetimi.md
last_updated: 2026-08-20
---

# AUTO aramasının ekonomisi

AUTO döngüsü ([[auto_mission_control]]) haftalarca koştu ve tutarlı bir sonuç
üretti: **hiçbir aday robustness'tan geçmiyor.** 2026-08-04 denetimi bunun
sebebinin strateji kalitesi değil **işlem ekonomisi** olduğunu ölçtü.

## Zincir: crypto trade_size → 1 hisse → %0,91 vergi

```
LLM kripto alışkanlığıyla trade_size=0.01 üretir
  → hisse senedinde lot tam sayı (size_precision=0) → 0 adede yuvarlanır
  → _clamp_spec_trade_size onu 1.0'a sabitler          (eski davranış)
  → QQQ ortalama $220 → pozisyon $220
  → IBKR Fixed: max(adet × $0,005, $1) → gidiş-dönüş $2
  → komisyon / pozisyon = %0,91 HER İŞLEMDE
```

Kritik ayrıntı: komisyon **200 hisseye kadar sabit** $1/emir. 1 hisse alan da
200 hisse alan da aynı $2'yi öder — yani pozisyon boyutu **gideri değil yalnız
kazancı** çarpar ve 1 hisse, oran açısından mümkün olan **en kötü** boyuttur.

Ölçülen sonuç (aynı sinyal, aynı işlemler, yalnız pozisyon çarpanı; üst sınır
hesabı — kayma/likidite dahil değil):

| aday | işlem | net @1 hisse | brüt/işlem | net @10 | net @20 |
|---|---|---|---|---|---|
| ADX ATR Güç Kalkanı | 1608 | −2.430 | $0,489 | +4.648 | +12.512 |
| DMI ATR Zırhı | 1037 | −1.327 | $0,720 | +5.394 | +12.861 |
| ADX Güç ATR Fren | 1659 | −2.596 | $0,435 | +3.898 | +11.114 |
| ADX Pullback Surfer | 153 | −107 | $1,096 | +1.403 | +3.080 |
| DMI Cross ATR Trail | 251 | −304 | $0,599 | +1.050 | +2.554 |
| ADX Ignition Breakout | 368 | −481 | $0,481 | +1.111 | +2.881 |
| ADX Pullback Rider | 204 | −292 | $0,362 | +373 | +1.112 |
| DI Pullback ATR Trail | 16 | −19 | $0,595 | +66 | +161 |
| ADX Surge Chandelier | 681 | −1.179 | $0,063 | −794 | −368 |
| DI Cross Chandelier | 22 | −56 | −$0,761 | −207 | −374 |

**Kaybeden 10 adayın 8'i yalnız çarpanla kâra geçiyor.** Gerçekten brüt kenarı
olmayan iki aday var. Katalog geneli tabloyu doğruluyor: 824 spec'in 659'u
`trade_size_mode="fixed"`, `trade_size` 516 kez `0.01`.

## Düzeltme: sabit adet değil, `percent_equity`

`_clamp_spec_trade_size` artık ölçek bozuk (<1) **ve** mod `fixed` olan hisse
spec'lerini `percent_equity`'ye çevirir (`AGENT_EQUITY_PCT`, varsayılan %95):

```python
if getattr(spec, "trade_size_mode", "fixed") != "fixed":
    return spec                      # ajan bilinçli mod seçtiyse dokunma
if float(spec.trade_size) < 1:
    spec.trade_size_mode = "percent_equity"
    spec.trade_size_percent = AGENT_EQUITY_PCT
    spec.trade_size = 1.0            # "fixed"e düşen yollar için taban
```

**Neden sabit adet değil:** QQQ 2003'te ~$25, bugün ~$725 (30×). İlk fiyata göre
seçilen adet sonda hesabı aşar, son fiyata göre seçilen adet başta mikroskobik
kalır. `percent_equity` her işlemde `equity × yüzde / fiyat` hesaplar
(`composer.py::_compute_qty`) ve `make_qty` tam sayıya yuvarlar — 30×'lik
aralığı yalnız bu kaldırır.

Komisyon oranı: işlem başına **%0,91 → ~%0,02**.

> **Açık uç:** pozisyon büyüyünce drawdown da dolar olarak büyür. Kazanç net
> P&L'in *işaret değiştirmesinden* gelir, risk-getiri oranının iyileşmesinden
> değil — Calmar yaklaşık korunur. Etkinin gerçek ölçümü için yeni bir AUTO
> koşusu gerekir.

## Skorlama: az işlem iki kez cezalanıyor

`_score` bileşiği (`0.7×Calmar + 0.3×perTradeSharpe`) ardından **sürekli** bir
güven çarpanı uygular: `n/(n+20)` (n=17 → ×0,46; n=20 → ×0,50). Bunun üstüne
`n < _MIN_TRADES → -inf` **sert kapısı** vardır. İkisi aynı işi yapar; ikincisi
bilgiyi yok ederek yapar — ölçülen bir koşuda net pozitif üç adayın ikisi
(17'şer işlem, +8,15 ve +41,91) hiç değerlendirilmeden elendi, yerlerine
−2.596 dolar kaybeden aday robustness'a girdi.

**Eşik bilerek 20'de bırakıldı.** İki gerekçe:

1. Değer `test_threshold_is_nau_aligned` ile korunuyor — NAU_ev optimizer'ının
   `JUNK_MIN_TRADES=20` eşiğiyle **bilinçli parite**, yani bir kusur değil
   yöntem kararı.
2. Düşürme gerekçesi çürüdü: eşiği kurcalama sebebi sabit komisyonun düşük
   frekansa yapay avantaj vermesiydi; boyutlandırma düzeltilince bu baskı
   kalktı.

Denemek için `AGENT_MIN_TRADES=10` (env, kod değişikliği gerekmez).

## Ölçüm doğrulandı: komisyon brüt kârın tamamını yiyebiliyor

2026-08-04 canlı koşusu (`1376c812`, QQQ 15-DK, `percent_equity` düzeltmesi
DEVREDE) tablonun mantığını doğrudan gösterdi:

```
n_trades          4283
commission_total  8566.69   (IBKR min $1/emir × 2 fill × 4283)
pnl (net)        -8514.77
→ brüt PnL          +51.92   → komisyon brütün %16.500'ü
```

Boyutlandırma düzeltildikten sonra bile **emir başına minimum** yüksek frekansta
bağlayıcı kısıt olmaya devam ediyor: strateji kötü değil, sıfır kenarlı; hesabı
bitiren tamamen komisyon tabanı. Bu bilgi artık modele de gidiyor
([[auto_kapi_ve_geri_bildirim]] §2) — önceden model yalnız net PnL'i görüp bunu
"kötü fikir" diye okuyordu.

## Calmar tabanı düşük riski cezalandırıyor

`calmar = pnl_pct / max(|max_dd|, 0.01)` — taban %1. Drawdown'ı %0,3 olan bir
aday Calmar 1,4 yerine 0,42 alır: **düşük riskli strateji, düşük riskli olduğu
için** düşük skorlanır. Açık, düzeltilmemiş bulgu.

## Bütçe artık İKİ tavan, iki birim (2026-08-15)

Token sayısı TEK sağlayıcı varken faturanın iyi bir vekiliydi. Amaç-başına model
eşlemesi bedava bir uç ekleyince bağ koptu: koşu `0057a0cd`'de bütçenin **%92'sini**
(194.375/210.411 token) hiç para harcamayan YEREL model yedi, gerçek fatura
1,03 USD'ydi ve tur 28 dakikada tek round kapatamadan `outcome: budget` ile
kesildi. Yerele geçmenin gerekçesi "token bedava → daha çok iterasyon"du; bedava
model BEDAVA OLDUĞU İÇİN değil SAYILDIĞI İÇİN koşuyu kısalttı.

  · **PARA** — `AGENT_DEFAULT_MAX_COST_USD` (5 → 20 USD), `_run_cost` ile ölçülür.
  · **TOKEN** — `AGENT_RUNAWAY_MAX_TOKENS` (2M), artık kaçak döngü emniyeti.

**Körlük şartı** tasarımın kilit taşı: para tavanı ancak maliyeti GÖREBİLDİĞİ
kadar korur. Fiyatı bilinmeyen paralı bir uçta hiç tetiklenmez — orada token
tavanı `AGENT_BLIND_MAX_TOKENS`'a (250k) iner. Gevşeme yalnız parayı gördüğümüzde.

Uygulama tuzağı kayda değer: route, token tavanını worker'a ULAŞMADAN
`DEFAULT_CONTINUOUS_MAX_TOKENS`'a kelepçeliyordu ve `HARD_MAX_AUTO_TOKENS`'ın
varsayılanı da o eski sabitten türüyordu. Üçü birden değişmeden davranış
değişmiyor — bir düğmeyi çevirmek, o düğmenin yolundaki HER kelepçeyi görmeyi
gerektirir.

Ölçülen etki: `5e89d42a` 66 dakikada **2 tur** tamamladı (eski tavanla 0-1) ve
para tavanında temiz kapandı. 20 USD ≈ 4,4 saat, yani bağlayıcı tavan artık SÜRE.
Testler: `tests/test_budget_has_two_ceilings.py`.

## Çıtaya uzaklığın haritası: 199 aday (2026-08-18)

"Hiçbir aday geçemiyor" cümlesi bu sayfada bir kez boyutlandırmaya bağlanmıştı.
Bu kez kapının ölçüsü doğrudan sayıldı — beş koşunun defterindeki **199 adayın**
al-tut'a göre Calmar oranı (`calmar_ratio_vs_benchmark`).

> **Ölçüm kesiti:** son koşu (`568f2838`) sayım anında HÂLÂ KOŞUYORDU. Sayılar
> 199 adaylık kesittir, nihai değil. İlk yazımda kesit 173'tü ve dağılım o
> aralıkta anlamlı biçimde oynadı (Haiku n=11 → 37 iken medyanı 0,14 → 0,20'ye
> çıktı). Devam eden bir koşudan alınan sayı, kesiti yazılmadan raporlanmamalı.

| | ×al-tut |
|---|---|
| min | −0,35 |
| p25 | 0,00 |
| **medyan** | **0,23** |
| p75 | 0,60 |
| p90 | 1,00 |
| max | 1,88 |

| bant | aday | pay |
|---|---:|---:|
| **≥×1,00 (geçen)** | 20 | **%10** |
| ×0,90–0,99 (kılpayı kaçıran) | 15 | %8 |
| ×0,70–0,89 | 7 | %4 |
| ×0,40–0,69 | 28 | %14 |
| ×0,00–0,39 | 96 | %48 |
| negatif | 33 | %17 |

Dağılımın şekli iki ayrı şey söylüyor ve karıştırılmamalı: **kütle çok uzakta**
(%65'i ×0,40'ın altında — eşik ayarı bunu kurtarmaz) ama **bir küme çok yakın**
(%8'i ×0,90–0,99). Çıtayı ×0,90'a indirmek geçen sayısını 20'den 35'e çıkarırdı
(+%75) ve tam olarak "al-tut'tan biraz daha kötü" olanı yayımlamak demekti. Bant
bir fırsat değil, **aramanın ne kadar yaklaştığının ölçüsü.**

Frekans ekseninde temiz bir ilişki YOK — geçme oranı bantlara göre %12 (<20
işlem, n=59), %4 (20-59, n=27), %8 (60-199, n=51), %14 (200-599, n=44), %11
(≥600, n=18). Komisyonun yüksek frekansı ezmesini beklerken en kötü bant 20-59
çıktı: az işlem hem istatistiki güç vermiyor hem çıtayı geçmiyor.

## Aptal taban zeki yolu geçiyordu — ve yayımlanamıyordu

Aynı 173 adayın üreticiye göre kırılımı beklenmedik çıktı:

| üretici | n | medyan | ≥×1,00 |
|---|---:|---:|---:|
| **rastgele fallback** | 117 | **0,46** | **20** |
| Haiku 4.5 (hedefli prompt) | 45 | 0,37 | 4 |
| Opus 5 | 45 | 0,26 | 4 |
| Haiku 4.5 (eski prompt) | 45 | 0,26 | 2 |
| Sonnet 5 | 45 | 0,14 | 2 |

> **Kesit notu:** "rastgele" satırı üç `or:qwen` koşusundan geliyor ve o
> koşularda yerel model ucu hiç ayakta değildi — yani orada bir MODEL yok,
> rastgele arama var (bkz. [[model_secici_ve_gorunurluk]]). Bu, bulguyu
> zayıflatmıyor GÜÇLENDİRİYOR: karşılaştırma "zayıf model vs güçlü model" değil,
> **"hiç model yok vs model"** — ve modelsiz taban hâlâ önde.

LLM erişilemediğinde devreye giren rastgele kompozisyonlar üç modelin hepsini
geçti — ve `research-only` damgası yüzünden **tanım gereği yayımlanamıyorlar.**
Yani sistem kapıya en yakın adaylarını kendi eliyle eliyordu.

Sebep prompt'ta duruyordu: kapı 2026-08-15'te risk-ayarlıya çevrildi, ÜRETİCİNİN
sistem mesajı hiç güncellenmedi. Bir kabul ölçütünün iki yakası var —
yargılayan ve üreten; yalnız birini güncellemek sistemi kendi kendine
çeliştirir ve fark "model yetersiz" gibi görünür.

**Düzeltme:** `COMPOSED_SYSTEM_PROMPT`'a ACCEPTANCE CRITERIA bloğu
(`agent.OBJECTIVE_IN_PROMPT`, `AGENT_OBJECTIVE_IN_PROMPT=0` ile kapatılır).
Yazılanlar: birincil çıtanın al-tut Calmar'ı olduğu + ölçülen gerçeklik ("medyan
öneri onun ~%20'sine ulaşıyor"), 20 işlem tabanı, ve göremediği OOS zinciri.

İki şey KASITLI dışarıda: **skor formülü** (`T/(T+20)` çarpanı doğrudan
şişirilebilir ve frekans-Calmar ilişkisi ölçümde bulunmadı) ve **"basit daha
iyidir"in yasa hâli** (örneklem dengesiz: rastgele n=72 iki koşudan, haiku
n=11) — sadelik ve drawdown önceliği hipotez olarak, "unless you have a specific
reason" kaydıyla girdi. Oynanabilir bir hedef, ölçütü bozar.

Hangi kolun koştuğu koşu kaydında (`objective_in_prompt`) duruyor; aksi hâlde iki
koşuyu karşılaştıran kişi neyin değiştiğini bilemez. Ölçüm:
[[nau_auto_kosulari_2026_08_18]].

## A/B sonucu: hedefi söylemek işe yaradı, ve söylediğin şeyi aldın (2026-08-18)

Kabul ölçütünün prompt'a yazılması ölçüldü. Aynı model (Haiku 4.5), aynı
enstrüman, aynı kapılar, **45'er aday**; değişen tek şey sistem mesajı.

| | eski prompt | hedefli prompt |
|---|---|---|
| Calmar medyanı | 0,26 | **0,37** |
| çıtayı geçen | 2 (%4) | **4 (%9)** |
| nitelenen | 2 | **4** |
| tur süresi | 104 dk | 56 dk |

Sıralama tarafı net iyileşti. **Ama iki yan etki de ölçüldü:**

* Robustluğa giren dört adayın **dördü de günlük bara** kaydı (eski kolda 1-DAY
  ve 4-HOUR karışıktı); WFO pencereleri 33-39 aya çıktı, geçerli pencere 6-8'e
  indi.
* Monte Carlo medyan düşüşü −%26,1'den **−%32,6**'ya kötüleşti
  (−39,5 / −25,8 / −42,0 / −14,5).

Hikâye tutarlı: az işlem, eğitim penceresinde düşük GERÇEKLEŞMİŞ drawdown
demek. Calmar tek bir yolun düşüşünü ölçer, Monte Carlo alternatif yolları sorar.
Model ölçülen metriği iyileştirirken ölçülmeyen kırılganlığı artırdı.

**Prompt buna göre yumuşatıldı (`41ec2ed`):** "FEWER CONDITIONS / prefer 2-3
blocks" tamamen kaldırıldı (kanıtı zaten dengesiz bir örneklemdi ve yan etkiyi
besliyordu); "drawdown control" kaldı ama yanına ödülün yan etkisini yakalayan
ölçüt eklendi ("Calmar tek bir gerçekleşmiş yoldan hesaplanır; az işlemli bir
strateji Monte Carlo'nun yıktığı gurur verici bir sayı üretebilir") ve frekans
açıkça nötrlendi ("Fast and slow ideas both pass and both fail here").

**Genel kural:** bir hedefi üreticiye söylemek onu iyileştirir — ve söylenmeyen
komşu boyutu bozabilir. Ödülü yazarken ödülün YAN ETKİSİNİ yakalayan ölçütü de
yaz.

## İpucu (hint) bir DARALTMA aracıdır — bağlama gücü ölçüldü

Kullanıcının her koşuda yazdığı serbest metin, üreticiye *"bunu strateji
fikrine dahil et"* diye giriyor. Üretilen aday ADLARININ kaçı ipucunun
ailesinde kalmış:

| ipucu | bağlama |
|---|---|
| `anchor vwap` | **24/24 (%100)** |
| `ema ve sma ve adx` | **24/24 (%100)** |
| `hh ll vwap` | 17/24 (%71) |
| `Dynamic Swing Anchored VWAP` | 17/27 (%63) |

Sezginin tersi: **en uzun ve en "profesyonel" görünen ipucu en gevşek bağladı.**
Sıfat zinciri ("dinamik", "swing", "anchored") modele yorum alanı açıyor ve
model Bollinger/Stochastic/Momentum'a dağılıyor; iki kelimelik somut bir ad ise
tek nesneye işaret ediyor.

İki pratik sonuç: (1) kesin bir şey istiyorsan KISA ve SOMUT yaz, (2) aramanın
keşfetmesini istiyorsan ipucunu BOŞ bırak — sistem prompt'undaki çeşitlilik
kuralı ("indikatör ailelerini döndür") ancak o zaman çalışabiliyor. Ölçülen beş
koşunun beşinde de ipucu doluydu, yani arama hiç serbest bırakılmadı.

Üçüncü, daha ince nokta: ipucu neyi SÖYLEMEDİĞİNİ de belirler. `anchor vwap`
çapanın nereye konacağını (seans başı? son swing? hacim patlaması?) modele
bırakır ve 15 aday 15 farklı çapa seçebilir. Tezi yazmak hem bağlar hem
belirsizliği kapatır.

## DÜZELTME: "rastgele taban" rastgele değildi (2026-08-20)

Yukarıdaki "aptal taban zeki yolu geçiyor" bulgusu — rastgele fallback
kompozisyonlarının Calmar dağılımında üç Claude modelini de geçmesi — ölçülmüş
bir gerçek. Ama ondan çıkarılan yorum ("karşılaştırma *hiç model yok vs model*")
**yanlıştı.**

`_fallback_composed` bloklarını `BLOCK_CATALOG`'dan seçiyor. Katalog canlı
kutuda **408 blok** içeriyor ve bunun **337'si `agnt_*`**, yani önceki
koşularda LLM'in ürettiği ve diske kalıcılaşmış bloklar. Yalnız 71'i dahili.

Yani "rastgele taban" aslında **geri dönüştürülmüş model çıktısının rastgele
yeniden birleştirilmesi**. Doğru okuma: *"taze model çıktısı"* vs *"geçmiş model
çıktısı, rastgele kombinasyonla"*. İkincisinin kazanması "model gereksiz"
demiyor; daha çok, tek bir çağrının ürettiği kombinasyonun, birikmiş blok
havuzundan rastgele seçime göre daha dar olduğunu düşündürüyor.

İki taban ayrı ölçüldüğünde WFO çıtasında aralarında anlamlı fark çıkmadı
(medyan %23 vs %23) — bkz. [[wfo_cita_kalibrasyonu_2026_08_20]]. Calmar
tarafında ayrım henüz yeniden ölçülmedi; **açık uç.**

Ders, bulgunun kendisinden bağımsız: **"rastgele" diye adlandırılan bir taban,
neyin arasından rastgele seçtiği yazılmadıkça bir taban değildir.**

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_kapi_ve_geri_bildirim]]
- [[nau_auto_kosulari_2026_08_18]]
- [[nau_performans_denetimi]]
- [[webapp_module_map]]
- [[wfo_cita_kalibrasyonu_2026_08_20]]
<!-- BACKLINKS:END -->
