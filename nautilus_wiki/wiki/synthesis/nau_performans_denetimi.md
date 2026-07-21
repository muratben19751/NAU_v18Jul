---
title: nautilus_web_app Performans Denetimi (2026-07)
type: synthesis
summary: nautilus_web_app'in runtime-performans denetimi — 50 ham bulgu, çekişmeli-doğrulama sonrası 31 doğrulanmış darboğaz. En yüksek ROI: LLM prompt-caching yokluğu; en sert kısıt: NAU_WINDOW=260 sabit-pencere paritesi incremental indikatörleri kilitler.
key_concepts:
  - single_threaded_core
  - crash_only_design
  - backtesting_guide
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/backtesting_guide.md
last_updated: 2026-07-21
---

# nautilus_web_app Performans Denetimi — 2026-07-21

`nautilus_web_app` repo kodunun (~31k satır) **runtime-performans** denetimi — [[webapp_module_map]]'in
performans-odaklı tamamlayıcısıdır (o *ne nereye bağlanır*'ı, bu *ne yavaş ve neden*'i verir).
12 modül paralel performans-lensiyle tarandı, ardından her bulgu **çekişmeli doğrulayıcı**
("kod karşısında bunu çürüt") ile denetlendi: **50 ham bulgu → 31 doğrulandı** (64 ajan).
Yalnız repo kodu incelendi; NautilusTrader kütüphanesine dokunulmadı ([[single_threaded_core]]
GIL kısıtı sandbox izolasyonunun *neden* var olduğunu açıklar — H3 bunu doğrular).

## İki baskın tema

1. **Sıcak-yol CPU.** Robustness taraması (WFO × GA × k-fold, `backtest_robustness.py` +
   `parallel_exec.py`) yüzlerce tam backtest çalıştırır. Her backtest'te NAU recursive
   indikatörleri (`composer.py` `adx_threshold`/`stoch_rsi_cross`/`wave_trend_cross`
   blokları) **her bar 260-pencereyi saf-Python'la sıfırdan** hesaplar; her guarded çağrıda
   **~1 sn'lik subprocess cold-boot** (nautilus/pandas/numpy re-import) ödenir.
2. **Cache eksikliği + sınırsız birikim.** `agent.py` LLM çağrıları ~16.6K token'lık sabit
   sistem prefix'ini **prompt-caching olmadan her seferinde** yeniden gönderir; `state.py`
   `AppState.iterations` listesi gece-boyu loop'ta **sınırsız büyür** (RAM + her 2 sn poll'da
   O(n) render).

## En yüksek ROI — 4 HIGH bulgu

| # | Bulgu | Modül | Efor |
|---|-------|-------|------|
| **H1** | LLM prompt-caching (`cache_control`) hiç yok (grep:0); 16.6K token her çağrıda yeniden işleniyor | `agent.py` `_create_message`, `web/routes/agent_backtest.py` | **S** ⭐ |
| **H2** | `iterations` listesi sınırsız (RAM + O(n) poll her 2 sn) | `state.py` | M |
| **H3** | Her guarded çağrıda subprocess spawn + ağır import (~1 sn) | `sandbox.py` | L |
| **H4** | NAU indikatörleri her bar 260-pencereyi saf-Python sıfırdan (~83–160 µs/çağrı) | `composer.py` | L |

**En pratik ilk iş — H1 (prompt-caching):** `agent.py`'de `system`'i content-block listesine
çevirip son büyük sabit bloğu `{"type":"text","text":…,"cache_control":{"type":"ephemeral"}}`
ile işaretle; `usage.cache_read_input_tokens > 0` ile doğrula. Backtest/spec/JSON semantiği
değişmez; onlarca-çağrılı continuous-loop koşularda belirgin maliyet/latency düşüşü.

## En sert kısıt — NAU_WINDOW=260 parite

En büyük CPU kazancı incremental indikatör state'inde (H4 + orta/düşük bulgular: ADX/StochRSI/
WaveTrend'i her bar full-recompute yerine running-state ile güncellemek). **Ama engel mimaridir:**
[[webapp_module_map]]'te belgelenen `composer.py` **NAU_WINDOW=260 sabit pencere** semantiği
korunmalı — naif incremental state farklı değer üretir, strateji sinyalleri kayar, NAU paritesi
bozulur. Kural:

> Bu göstergelerde herhangi bir incremental/vektörize optimizasyon **kayan-260 pencereyi koruyan**
> bir sürüm olmalı ve `indicators.calc_*` referansıyla **bit-parite testi** (tolerans <1e-9)
> geçmeli. `tests/test_regression_anchors*.py` yeşil kalmalı. Geçmezse uygulanmaz.

Bu, "Sağlamlaştırma & regresyon" bölümündeki NAU-uyum denetiminin ([[webapp_module_map]])
performans-tarafı devamıdır: **hız için strateji doğruluğunu feda etme.**

## Doğrulamanın kattığı değer — reddedilen "iyileştirmeler"

- **Elle tek-geçiş min/max** (`indicators.calc_stoch_rsi`): CPython'da `min()`/`max()`
  C-implemented; elle döngü **~%15 DAHA YAVAŞ**. Yalnız monotonik-deque gerçek kazanç.
- **Equity N-bar örnekleme** (`composer.py` `on_bar`): `_mtm_equity` telemetri değil **birincil
  metrik kaynağı**; örneklemek max_dd ve Sharpe'ı bozar.
- **"O(n²) kilitlenir"** (`indicators.calc_nadaraya_watson`): n her yolda ≤~1040 sınırlı —
  premis yanlış.
- **Thread-executor'a geçiş** (`sandbox.py`): killability'i kaybettirir ([[single_threaded_core]]
  GIL — thread güvenle öldürülemez); bilinçli bir takas.

## Güvenli hızlı kazanımlar (S efor, parite korur)

- `indicators.sma()` running-sum (bit-identical, tol 1e-9) + monotonik-deque min/max.
- `composer.py` `on_bar` FFI çağrılarını (`is_net_long`/`is_net_short`) sinyal-guard altına al.
- `backtest.py` `_extract_trades` `itertuples`; `_metrics` tek `np.asarray`.
- `wfo_optimizer.py` fold dilimlerini kandidat başına değil bir kez ön-hesapla.
- `backtest_robustness.py` Monte-Carlo win-rate vektörizasyonu (`(shuffled>0).sum(axis=1)`).

Dikkat gerektiren (invalidasyon/off-by-one): `data.py`/`web/routes/backtest.py` mtime-keyed
cache'leri custom-block durumu + dir-mtime anahtarına katmalı; WFO pencere label-slice
inclusive-end off-by-one'a dikkat.

## Metodoloji

`Workflow` 4-fazlı: Recon → Scan (12 modül paralel) → Verify (her bulgu çekişmeli, REJECTED/
PLAUSIBLE/CONFIRMED) → Synthesize. Aynı çok-ajanlı çekişmeli desen [[webapp_module_map]]'in
"Sağlamlaştırma" turlarında ve mimari incelemede de kullanıldı — tekrarlanabilir bir kalite kalıbı.

> Bu bir *denetim anlık görüntüsüdür* (2026-07-21). Fix'ler uygulandıkça bulgular kapanır;
> güncel modül-eşlemesi için [[webapp_module_map]] esastır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_mimari_denetimi]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
