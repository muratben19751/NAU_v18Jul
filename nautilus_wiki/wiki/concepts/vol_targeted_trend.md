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
```

`ewma_vol`, `on_bar` içinde **artımsal (O(1)/bar)** tutulan bir EWMA varyansının
kareköküdür (`self._ewma_var`), her barda `calc_ewma_vol(closes)`'i baştan
hesaplamaz. Seed davranışı `indicators.py`'deki `calc_ewma_vol` ile **birebir
eşleşecek** biçimde ayarlanır: ilk gözlemlenen log-getiri tam ağırlıkla girer
(`ewma_var = lr²`), alpha = 2/(span+1) yalnızca ikinci getiriden itibaren
uygulanır. Böylece warmup sonrası tahmin referans hesaplayıcıyla aynıdır (aksi
halde warmup civarında ~%13 sapma oluşuyordu).

Warmup kapısı: `_vol_warmup` (gözlemlenen getiri sayısı) `vol_span`'a ulaşana
dek — yani ilk `vol_span+1` bara dek — vol-boyutlandırma devre dışıdır ve
`trade_size`'a düşülür. İki güvenlik sınırı: boyut sermayenin **%95**'iyle
kaplanır (`AccountBalanceNegative` önlemi) ve `instrument.size_increment`
tabanına yuvarlanır.

MA yönü de artımsal koşan toplamlarla (`_fast_sum`/`_slow_sum`) O(1)'de
hesaplanır; float yuvarlama kaymasını sınırlamak için her 4096 barda toplamlar
deque'ten `math.fsum` ile yeniden hesaplanır.

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

## MTM equity snapshot (risk metrikleri)

Strateji, warmup sonrası **her `_MTM_SAMPLE` barda bir** (`_snapshot_mtm`)
`portfolio.equity(venue)` üzerinden bar-çözünürlüklü mark-to-market equity
biriktirir (`_mtm_equity`/`_mtm_ts`). `backtest.py` bunu `getattr` ile okur ve
`max_dd`, bar-frekanslı Sharpe ve MTM equity eğrisini bundan türetir ([[reports]]).
Örneklem seyreltmesi (varsayılan her 5 bar), per-bar `equity()` maliyetini
düşürürken pozisyon-içi drawdown çözünürlüğünü büyük ölçüde korur — snapshot'ı
tümüyle atmak `max_dd`/Sharpe'ı sessizce trade-çözünürlüğüne düşürürdü.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
