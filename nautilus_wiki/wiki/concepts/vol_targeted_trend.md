---
title: Vol-Targeted Sizing (composer vol_target mode)
type: concept
sources:
  - sources/03_strategies_docs.md
  - https://nautilustrader.io/docs/latest/concepts/strategies
last_updated: 2026-07-19
summary: EWMA volatilite hedefli pozisyon boyutlandırma; artık bağımsız strateji değil, composer motorunun trade_size_mode="vol_target" sizing modu. Describe/Composer akışında herhangi bir sinyal setiyle birlikte seçilir.
key_concepts:
  - strategy_and_actor
  - order_flow_pipeline
  - backtesting_guide
  - accounting
---

# Vol-Targeted Sizing (composer `vol_target` mode)

> **Değişiklik (2026-07-19):** Vol-targeted trend eskiden `strategies.py`
> içindeki bağımsız bir `VolTargetedTrendStrategy` + `STRATEGY_REGISTRY`
> kaydı + ayrı `/backtest/run_vtt` route'u + `backtest.html`'de ayrı radio idi.
> Kullanıcı "her şeye ayrı ekran istemiyorum" gerekçesiyle bu, **composer
> motorunun bir pozisyon-boyutlandırma moduna** taşındı. Artık yön sinyalini
> Describe/Composer akışının ürettiği bloklar verir; volatilite hedefli boyut
> ise `ComposedStrategy._compute_qty`'deki `vol_target` dalıdır. Ayrı strateji
> sınıfı, registry kaydı ve route kaldırıldı.

## Ne oldu, ne kaldı

- **Yön (sinyal):** artık composer bloklarından gelir (ör. `ma_cross` entry/exit).
  Herhangi bir strateji tarifiyle birlikte kullanılabilir — MA crossover'a bağlı değil.
- **Boyut (sizing):** `composer.py` `TradeSizeMode` literaline eklenen
  `"vol_target"` modu. Diğer modlarla (`fixed`, `fixed_usdt`, `percent_equity`,
  `atr_target`) aynı yerde, `_compute_qty(price)` içinde yaşar.

## Boyutlandırma (EWMA vol targeting)

```
size = (vol_target / ewma_vol) * capital / price
```

- `ewma_vol`, `indicators.py`'deki `calc_ewma_vol(self._closes, span)` ile
  hesaplanır. Eski bağımsız strateji artımsal `_ewma_var` state tutuyordu; yeni
  modda `_compute_qty` **yalnızca giriş sinyalinde** çağrıldığı için (her barda
  değil) O(1) state'in getirisi yoktu — hazır, test edilmiş `calc_ewma_vol`
  yeniden kullanıldı. (Artımsal yol ayrıca warmup civarında ~%13 sapma bug'ı
  çıkarmıştı.)
- **`capital` sabittir** (canlı equity değil): `spec.trade_size_capital`,
  Describe formundaki *Initial Capital* değerinden beslenir (boşsa 10.000).
  Bu, `atr_target`/`percent_equity` modlarının canlı equity davranışından
  bilinçli olarak farklıdır — eski VTT davranışıyla birebir uyum için.
- **Warmup:** `< vol_span+1` close varken `calc_ewma_vol` `None` döner ve
  boyut `spec.trade_size`'a düşer. `_buf_cap`, `vol_span+5` tabanına yükseltilir
  ki EWMA penceresi blok lookback'lerinden bağımsız tutarlı olsun.
- **Üst sınır:** boyut `0.95 * capital / price` ile kaplanır (aşırı kaldıraç /
  negatif bakiye önlemi). `make_qty` `size_increment`'e yuvarlar; sıfıra
  yuvarlanan boyut, `_submit_entry`'deki `qty<=0 → skip` guard'ıyla atlanır.

## UI

Backtest sayfasında (`backtest.html`) **Broker Settings** altına eklenen
"Pozisyon boyutu" dropdown'ı: Sabit / % Equity / ATR hedefli / Vol hedefli.
Vol-hedef seçilince `trade_size_vol_target` + `trade_size_vol_span` inputları
görünür; capital olarak yukarıdaki Initial Capital alanı kullanılır. `/backtest/describe`
bu alanları `ComposedStrategySpec`'e yazar; spec katalog + subprocess JSON turunda
(`to_dict`/`from_dict`) korunur.

## allow_short → MARGIN hesap

`allow_short` composer'da zaten `spec.allow_short` ile mevcut ve Describe
formundan besleniyor; vol_target modu için ek bir tesisat gerekmez. Short açan
SELL emirleri MARGIN hesap gerektirir (bkz. [[accounting]]). Emirler
[[order_flow_pipeline]]'a girer; sonuçlar [[backtesting_guide]] yolunda değerlendirilir.

## Parametreler (spec alanları)

| Alan | Varsayılan | Anlamı |
|---|---|---|
| `trade_size_mode` | `"fixed"` | `"vol_target"` seçilince aşağıdakiler devreye girer |
| `trade_size_vol_target` | 0.02 | Hedef günlük vol fraksiyonu (0.02 = %2) |
| `trade_size_vol_span` | 10 | EWMA vol tahmini span'i (≥2) |
| `trade_size_capital` | 10000.0 | Boyutlandırma için sabit nominal sermaye (Initial Capital) |

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
