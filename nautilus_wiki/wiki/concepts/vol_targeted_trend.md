---
title: Vol-Targeted Trend Strategy
type: concept
sources:
  - sources/03_strategies_docs.md
  - https://nautilustrader.io/docs/latest/concepts/strategies
last_updated: 2026-07-19
summary: MA crossover yönü + EWMA volatilite hedefli pozisyon boyutlandırma; allow_short=True iken MARGIN hesapla long+short açan nautilus_web_app stratejisi.
key_concepts:
  - strategy_and_actor
  - order_flow_pipeline
  - backtesting_guide
  - accounting
---

# Vol-Targeted Trend Strategy

`nautilus_web_app`'e özgü bir [[strategy_and_actor|Strategy]] (`strategies.py`
içindeki `VolTargetedTrendStrategy`). Yön kararını bir MA crossover'dan alır,
pozisyon **boyutunu** ise EWMA volatilite tahminini bir hedef vole eşitleyerek
belirler. `STRATEGY_REGISTRY`/`STRATEGY_PARAM_SPEC`'e `vol_targeted_trend`
anahtarıyla kayıtlıdır.

## Sinyal (yön)

Hızlı/yavaş SMA farkının işaret değiştirmesi (crossover):

- **Yukarı kesişim** (`prev_diff ≤ 0 < diff`): açık short varsa kapat, long yoksa BUY.
- **Aşağı kesişim** (`prev_diff ≥ 0 > diff`): açık long varsa kapat; `allow_short`
  açıksa ve short yoksa SELL (short aç).

`fast`/`slow` periyotları config'ten gelir (`slow > fast`). Kesişim ilk barda
başlatılmaz — `_prev_diff` None iken bir bar beklenir (sahte kesişim önlenir).

## Boyutlandırma (EWMA vol targeting)

```
size = (vol_target / ewma_vol) * capital / price
ewma_vol = calc_ewma_vol(closes, span=vol_span)   # sqrt(ewma(log_return², span))
```

`calc_ewma_vol` `indicators.py` içindedir; alpha = 2/(span+1) (pandas
`.ewm(span=N)` ve modüldeki `ema()` ile aynı konvansiyon). `span+1` bardan az
veri varken `None` döner (warmup) — bu durumda `trade_size`'a düşülür.

İki güvenlik sınırı: boyut sermayenin **%95**'iyle kaplanır (`AccountBalanceNegative`
önlemi) ve `instrument.size_increment` tabanına yuvarlanır.

## allow_short → MARGIN hesap

`allow_short=True` short pozisyon açmayı gerektirir; bunun için `backtest.py`
`run_backtest` venue'yü `AccountType.MARGIN` ile açar (aksi halde `AccountType.CASH`).
Bkz. [[accounting]] ve [[webapp_module_map]] `backtest.py` satırı. Emirler
[[order_flow_pipeline]]'a girer; sonuçlar [[backtesting_guide]] yolunda değerlendirilir.

## Parametreler

| Param | Aralık | Anlamı |
|---|---|---|
| `fast` | 2–50 | Hızlı MA periyodu |
| `slow` | 10–200 | Yavaş MA periyodu (> fast) |
| `vol_span` | 5–30 | EWMA vol tahmini span'i |
| `vol_target` | 0.001–0.05 | Hedef günlük vol fraksiyonu (0.01 = %1) |
| `capital` | 1e3–1e5 | Boyutlandırma için nominal sermaye |
| `allow_short` | bool | Trend aşağıyken short aç (MARGIN gerekir) |

`agent.py` `_fallback_proposal` ajan yokken bu parametreler için rastgele bir
öneri üretir (`vol_targeted_trend` dalı).

## Bilinen boşluklar

- MTM equity snapshot'ı (`_snapshot_mtm`) `portfolio.equity(venue)` üzerinden
  toplanıyor; equity eğrisinin backtest raporuna nasıl bağlandığı ([[reports]])
  ayrıca doğrulanmalı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
