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
last_updated: 2026-08-16
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

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_kapi_ve_geri_bildirim]]
- [[nau_performans_denetimi]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
