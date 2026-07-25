---
title: Strategy Studio (görsel strateji kurucu)
type: synthesis
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - sources/02_architecture_docs.md
last_updated: 2026-07-25
summary: /studio/{id} altındaki görsel strateji kurucu; sürümlü şema → derleyici → to_nautilus → composer spec → run_composed_backtest zinciri, çeviremediğini sessizce atmak yerine gerekçesiyle reddeder.
key_concepts:
  - strategy_and_actor
  - backtesting_guide
  - portfolio
  - order_flow_pipeline
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/backtesting_guide.md
  - wiki/entities/portfolio.md
---

# Strategy Studio (görsel strateji kurucu)

`nautilus_web_app`'e 2026-07-25'te birleştirilen ikinci strateji yüzeyi.
Kullanıcı kural bloklarını HTMX ile düzenler; sonuç sürümlü bir şemada saklanır,
derlenir ve mevcut NautilusTrader koşucusuna indirgenir.

Adı benzese de `/studio` ile **aynı şey değildir**: `/studio` Composer+Backtest
sayfasıdır ([[webapp_module_map]], `web/routes/studio.py`), kurucu ise
`/studio/{strategy_id}` altında yaşar. İkisi birbirini gölgelemez (farklı yol
şekilleri), ama ad ortaklığı akılda tutulmalı.

## Katmanlar

```
şema (pydantic, sürümlü)       strategy_studio/schema.py
   ↓ mutations.py              insan ve AI düzenlemeleri AYNI yoldan geçer
derleyici                      strategy_studio/compiler.py → CompiledStrategy
   ↓ to_nautilus()             strategy_studio/backtest.py
composer ComposedStrategySpec  ← mevcut blok kataloğu
   ↓ run_composed_backtest()   backtest.py → BacktestEngine
BacktestMetrics                → sparkline + fold tablosu + deploy kapısı
```

`CompiledStrategy` nötr bir ara temsildir: şema/UI/AI tarafı motor
değişikliğinden etkilenmez, motor yüzeyi tek dosyada (`backtest.py`) toplanır.

## Sessiz atlama yasağı

Bu zincirin en önemli tasarım kuralı: **çevrilemeyen hiçbir şey sessizce
düşürülmez.** Ekrandakinden farklı bir stratejinin metriklerini döndürmek, hata
vermekten daha kötüdür. `to_nautilus` tüm gerekçeleri birden toplayıp
`UnsupportedStrategy` fırlatır; kullanıcı koşunun neden başlamadığını tek
seferde görür.

Reddedilenler: rejim dalı · ranked allocation · composer bloğu olmayan
indikatör · motor karşılığı olmayan operatör (`ADX < x`) · enstrümanınkinden
farklı **timeframe**'e sabitlenmiş kural (koşu başına tek bar beslemesi) ·
`risk.max_concurrent > 1` (composer stratejisi tek pozisyon tutar) ·
`risk.time_stop_bars` (zaman bazlı çıkış yok) · `entry match='any'` + filtre
birleşimi (spec'te tüm giriş blokları için tek `entry_logic` var).

## İndikatör köprüsü

Şema indikatörleri `indicators.py`'deki gerçek fonksiyonlara tek tip bir
sözleşmeyle bağlanır: `impl(bars, **schema_params)`. İnce adaptörler şema
parametre adlarını (`len`, `n1`/`n2`) fonksiyonların kendi argümanlarına
(`period`, `channel_len`/`avg_len`) çevirir — şema kelime dağarcığı, fonksiyon
yeniden adlandırılsa bile sabit kalır. 15 kayıttan 8'i bağlı; kalanların
(`macd`, `funding_z`, `oi_z`, `cvd_divergence`, `volume_profile`, `time_stop`,
`session_filter`) uygulaması yok, `impl=None` bırakıldı.

## İki motor anahtarı

| Env | Neyi seçer | Varsayılan |
|---|---|---|
| `STUDIO_BACKTEST=nautilus` | Run butonu — tıklama başına tek koşu | stub |
| `STUDIO_BACKTEST_OPT=nautilus` | optimizer sweep + AI döngüsü denemeleri | stub |

Ayrı olmalarının nedeni maliyet: adaptörün her `run()` çağrısı
`1 + walkforward.folds` motor koşusudur ve optimizer 400 kombinasyona kadar
örnekler → tek sweep ~1 600 koşu. Tek-koşu anahtarını çevirmek bunu
tetiklememelidir. Gerçek motor sweep için seçiliyse `POST /optimize` ayrıca
`STUDIO_OPT_MAX_ENGINE_RUNS` (varsayılan 200) üstünde baştan 422 döner.

Stub adaptör silinmedi ve **her yerde varsayılandır**: piyasa verisi olmadan tüm
UI döngüsü çalışır, test takımı çevrimdışı kalır (`tests/studio`, 140 test,
env flag'siz yeşil).

## Metriklerin kaynağı (ve neden hepsi motordan alınmıyor)

- **Equity eğrisi** — bar seviyeli MTM serisi; saklanırken 260 noktaya
  indirgenir (sparkline 260 px, ham eğri koşu başına ~39 KB JSON'du).
  İstatistikler tam eğriden hesaplanır.
- **Drawdown** — motorun MTM figürü (realized eğrinin göremediği dipleri yakalar).
- **Sharpe** — bilinçli olarak `sharpe_per_trade`. Bar-frekanslı Sharpe her düz
  barı sıfır getiri sayar, piyasada az duran stratejinin paydasını şişirir (aynı
  koşu: 6.02 bar-frekanslı, 0.51 işlem bazlı). Studio strateji **sıralar**
  (optimizer objective, deploy kapısı) — bu yanlılık az işlem yapanı sistematik
  kayırırdı. `/backtest` bar-frekanslıyı gösterir; fark bilinçlidir.
- **`dsr`** — aslında PSR (tek denemeli DSR). Gerçek deflasyon, Sharpe'ın kaç
  deneme arasından seçildiğini bilmeyi gerektirir; o sayı optimizer entegre
  edilince gelir. Deploy kapısı bu yüzden iyimser tarafa hata yapar.
- **Fold'lar** — ardışık OOS dilimleri; `walkforward.embargo_bars` kadar baş
  kısmı atılır. Her enstrüman aynı şekilde dilimlenip başlıkla aynı biçimde
  harmanlanır.

Bu serinin pozisyon açıkken çökmesine yol açan `Portfolio.equity()` tuzağı ve
ölçülen etkisi [[portfolio]] sayfasındadır.

Çok enstrümanlı stratejide metrikler eşit ağırlıklı equity harmanıdır: her
sleeve kendi sermayesiyle koşar, rebalance yoktur — ortak sermayeli portföy
koşusu **değildir**.

## AI guardrail baseline'ı

`evaluate_trial` denemeyi baseline ile karşılaştırır. İki motor anahtarı farklı
ayarlanabildiği için baseline **denemenin koştuğu motorda** ölçülür
(`_trial_baseline`, (motor, tanım) anahtarıyla LRU önbellekli) — aksi halde
guardrail öneriyi değil motor farkını yargılardı. Baseline'ın *var olma* koşulu
değişmedi: tamamlanmış bir koşu gerekir, yani hiç backtest edilmemiş stratejide
guardrail kapalıdır. Deploy kapısı ise bilerek `latest_run`'ı okur — kullanıcının
kendi tetiklediği gerçek koşuyu yargılamalıdır.

## Durum

Beş INTEGRATION POINT'ten ikisi bağlı: `registry.py` (indikatörler) ve
`backtest.py` (motor). `optimizer.py`, `ai.py` ve `deploy.py` hâlâ stub —
sırasıyla `wfo_optimizer` / [[backtesting_guide]] walk-forward'ı, mevcut LLM
istemcisi ve canlı/sim TradingNode bağlanacak.

Tohumlanan iki demo stratejisi: `wt-funding-v3` (tasarım maketi — rejim dalı +
`funding_z` içerdiği için yalnız stub'da koşar) ve `rsi-adx-btc` (motorda
koşabilen; `rsi_threshold` + `adx_threshold` girişi, `atr_stop` çıkışı, tek
Bybit enstrümanı). Gerçek koşu (BTCUSDT 1h, 180 gün, 4319 bar): 23 işlem,
net +%1.54, Sharpe 0.51, DSR 0.71, Max DD −%2.99, ~2 sn.

## Bilinen boşluklar

- `dsr` gerçek DSR'a deflate edilmiyor (deneme sayısı optimizer'dan gelmeli).
- Çok enstrümanlı harman ortak sermayeli portföy koşusu değil.
- `walkforward` şemasının `scheme` / `in_sample_months` / `oos_months` alanları
  UI'da ayarlanabiliyor ama fold dilimlemesi yalnız `folds` + `embargo_bars`
  kullanıyor.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[portfolio]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
