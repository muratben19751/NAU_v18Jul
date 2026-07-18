---
title: nautilus_web_app Modül Haritası
type: synthesis
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - sources/02_architecture_docs.md
last_updated: 2026-07-18
summary: nautilus_web_app'in her Python modülünün Nautilus wiki karşılığını gösteren canlı harita; kod ↔ doküman geri-beslemesini formalize eder.
key_concepts:
  - nautilus_kernel
  - data_engine
  - strategy_and_actor
  - order_flow_pipeline
  - v1_to_v2_migration_lessons
  - crash_only_design
  - environment_contexts
  - backtesting_guide
related:
  - wiki/synthesis/v1_to_v2_migration_lessons.md
  - wiki/synthesis/backtesting_guide.md
  - wiki/synthesis/index_backtest_via_equity_proxy.md
---

# nautilus_web_app Modül Haritası

Bu sayfa `nautilus_web_app` repo kodunun Nautilus wiki karşılıklarını **tek yerde** listeler. Amaç: Karpathy-tarzı LLM knowledge base pattern'inin son bacağını kapatmak — kod → wiki → kod. Uygulama artık her modülünün en üstünde bir `Wiki References` bloğu taşır; bu sayfa da ters yönden aynı köprüyü sunar.

## Genel şema

Uygulama Nautilus'un **düşük seviyeli** yolunu kullanır ([[backtesting_guide]] "BacktestEngine seç eğer" satırlarına uyar). [[backtest_node]] + [[parquet_data_catalog]] yüksek seviyeli yoluna yakınsayacak biçimde yapılandırılmıştır, ancak hâlâ tek-instrument + in-process.

## Modül karşılıkları

### Web katmanı (FastAPI)

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `server.py` | Lifespan + router mount | [[nautilus_kernel]] (küçük ölçekte "compose then run"), [[event_driven_architecture]] |
| `web/routes/data.py` | GET /data — instrument catalog | [[parquet_data_catalog]], [[bar_aggregation_and_type_syntax]], [[index_backtest_via_equity_proxy]], [[precision_modes]] |
| `web/routes/strategy.py` | Strategy composer + block CRUD. **Multi-timeframe trend filtresi** advanced-options'ta açık: `trend_filter`/`trend_interval`/`trend_ema_period` spec alanlarına yazar (ana TF'de işlem + üst TF'de EMA trend onayı; `composer.ComposedStrategy` ikincil bar feed'ini subscribe eder, look-ahead güvenli) | [[strategy_and_actor]], [[order_flow_pipeline]], [[bar_aggregation_and_type_syntax]] |
| `web/routes/backtest.py` | **/backtest** birleşik tarif-odaklı panel: stratejiyi doğal dille tarif et → `POST /backtest/describe` Claude'a entry/exit için AYRI custom blok(lar) yazdırır (`propose_condition_breakdown` → koşul-başına `propose_custom_block` → blok-seviyesi OR/AND spec), sonra seçilen zaman dilimlerine göre zincirler: **2+ TF → `POST /backtest/sweep`** (aynı spec'i her TF'de koşup karşılaştırma tablosu — cache-yok TF atlanır, en-iyi sütun yeşil), **1 TF → `POST /backtest/run`** (tam sonuç + equity + robustness OOB). Sembol datalist (yaz-bul), çoklu-TF checkbox. `_normalize_intervals` ortak; `intervals_csv` hx-vals dizi kodlamasına güvenmeden çoklu-TF taşır. Index/External backtest route'ları (`/run`, `/tickers`, `/external_instruments`) korunur ama panelden Bybit-odaklı akış için kaldırıldı | [[backtesting_guide]], [[environment_contexts]], [[bar_aggregation_and_type_syntax]] |
| `web/routes/agent_backtest.py` | Otonom backtest pipeline (5-faz: veri→strateji üret→backtest döngüsü→sıralama→robustness). **NAU kompozit skor** (`_score`): `0.7·calmar + 0.3·sharpe_per_trade` × `n/(n+20)` güven sönümü; junk kapısı `n<20 VEYA max_dd≥0 → -inf` (NAU ile hizalı); sharpe terimi **per-trade** ((mean/std)×√n), annualized DEĞİL. Robustness kapısı (`_robustness_passed`) IS/OOS + penalized OOS sharpe + Monte-Carlo (medyan DD>-25%) + multi-symbol katmanlıdır. Backtest'ler `sandbox.run_backtest_guarded(force_subprocess=True)` ile öldürülebilir child'da (event loop donmaz); robustness `run_robustness_guarded` child'ında, ilerleme `_IPC_Q` kuyruğuyla parent'a akar. Canlı **Gantt zaman çizelgesi**: her işlem (veri/LLM/backtest/robustness) bir span; `_tl_begin/_tl_end` ile SVG'de çizilir, `/sessions` replay eder. Bellekte olmayan run için dürüst terminal mesajı (session_end görülmüşse "tamamlandı", log yarıda kesikse "sunucu yeniden başlatıldı") | [[backtesting_guide]], [[environment_contexts]], [[crash_only_design]] |
| `web/routes/loop.py` | Start/stop background loop (dashboard `/` sayfasından) | [[crash_only_design]] |
| `web/routes/robustness.py` | IS/OOS + WFO + MC + multi-symbol | [[backtesting_guide]], [[portfolio]] |
| `web/routes/chart.py` | GET /chart/data — Lightweight Charts için OHLCV + strateji indikatörleri (JSON). Pencere/TF fizibilite guard'ı: aralık×TF >60k mum üretecekse veri yüklemeden reddeder (6y×1m ≈ 3M mum tarayıcıyı kilitler) | [[bar_aggregation_and_type_syntax]] |
| `web/routes/lab.py` | Strategy Lab — tek tıkla Claude fikir→custom block→backtest→KPI | [[strategy_and_actor]] |
| `web/routes/reports.py` | Backtest Reports — backtest_log.jsonl + robustness_log.jsonl join'li geçmiş tablosu. İstemci-tarafı: hızlı sıralama (değer cache + tek-reflow), kolon filtreleri (sayısal ifade + substring), sayfalama (DOM'da yalnız aktif sayfa), kalıcı görünüm durumu (`reports_layout.json`). **`GET /reports/detail?ts=`**: log satırındaki tam spec + veri kimliğiyle backtest'i sandbox child'ında deterministik yeniden koşar (`run_in_executor`, event loop bloklanmaz) → fiyat grafiği (giriş/çıkış marker + PnL-renkli çizgiler) + trade başına giriş/çıkış sebebi tablosu + sadakat rozeti (log vs yeniden); 8-girişli FIFO cache | [[backtesting_guide]] |
| `web/routes/sessions.py` | Agent Session Logs — agent_sessions/*.jsonl görüntüleyici | (app-spesifik) |
| `web/routes/wiki.py` | Karpathy wiki frontend | (app-spesifik) |
| `web/routes/dashboard.py`, `fragments.py` | UI | (app-spesifik) |

### Domain (Nautilus yüzeyi)

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `backtest.py` | `BacktestEngine` wrapper + `BacktestNode` yolu; `NAUTILUS_DEBUG_LOG` env var ile iç loglama açılabiliyor. Enstrüman spec'leri NAU universe.yaml'a pinli: `_BYBIT_SPECS` per-symbol precision (BTC/ETH sp3, SOL sp2, XRP sp1), kategori-başına venue (`BYBIT_SPOT`/`BYBIT_LINEAR`/`BYBIT_INVERSE` — katalog anahtarları ayrık kalır), `_make_index_instrument` pp2/tick 0.01. `_extract_trades` her trade'e **giriş/çıkış sebebi** ekler: positions raporundaki `opening/closing_order_id` → fills raporu emir tag'leri (`dr:<seq>`/`xr:<seq>`/`sl`/`tp`/`flip`/`eob`) → composer karar günlüğü join'i (fills lookup tek-geçiş dict) | [[backtest_node]], [[backtesting_guide]], [[environment_contexts]], [[data_wranglers]], [[precision_modes]], [[portfolio]], [[v1_to_v2_migration_lessons]], [[index_backtest_via_equity_proxy]] |
| `strategies.py` | `MACrossoverStrategy` + `RSIMeanReversionStrategy` | [[strategy_and_actor]] hiyerarşisi (Actor + Strategy + Config), [[v1_to_v2_migration_lessons]] StrategyConfig plain-class kalıbı |
| `composer.py` | Block-based `ComposedStrategy` — **13 builtin blok** (`ma_cross`, `rsi_threshold`, `price_breakout`, `momentum`, `volume_spike`, `ema_cross`, `bollinger_break`, `macd_cross`, `atr_stop`, `adx_threshold`, `stoch_rsi_cross`, `wave_trend_cross`, `donchian_channel`). Son 4 blok NAU parite indikatör kütüphanesi `indicators.py` üstünde kurulu (`ind` modülü olarak enjekte); bloklar `closes` + `indicators["volumes"]/["highs"]/["lows"]` (gerçek OHLC serileri) görür. **NAU_WINDOW=260 sabit pencere**: recursive (Wilder/EMA-tohumlu) `adx_threshold`/`stoch_rsi_cross`/`wave_trend_cross` blokları `_nau_win` ile son 260 barda hesaplar; bu bloklar spec'te varsa buffer ≥260 tutulur (kompaksiyon süreksizliği → sahte kesişim önlenir). **Flip yolu**: `allow_short=True` iken ters sinyal `_cancel_working()` + `close_all_positions(tags=['flip'])` sonrası ters taraf açar (M17 flat-kalma guard'ı); her flat girişte de dolmamış GTC limit iptal edilir (birikme yok). **Karar günlüğü** (`_decision_log`): her giriş/çıkış kararında ateşleyen blok(lar) + params + indikatör anlık değeri kaydedilir, emirlere `dr:`/`xr:` tag'i basılır (SL/TP/flip/eob tag'leri) → backtest sonrası sebep join'i. **2-TF trend filtresi**: `spec.trend_filter` iken `ComposedStrategy` `secondary_bar_type`'ı (üst TF, `trend_interval`) subscribe eder; üst-TF kapanışlarından `trend_ema_period` EMA hesaplayıp `_trend_bias` ile ana-TF girişlerini kapıya sokar (look-ahead güvenli — üst-TF barı yalnız KAPANDIĞINDA `on_bar`'a gelir). Perf: `_closes`/`_volumes` düz-list buffer (bar başına tam kopya yok), `_current_equity` hızlı-yol cache (`_equity_mode`) — hepsi sonuç-birebir | [[strategy_and_actor]] altında custom Strategy; emir çıkışı [[order_flow_pipeline]]'a girer |
| `custom_block_store.py` | Kullanıcı bloklarının on-disk kayıt defteri | [[strategy_and_actor]] |

### Motor katmanı (izolasyon + paralel yürütme)

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `sandbox.py` | Öldürülebilir process sandbox: `run_backtest_guarded` (custom-block veya `force_subprocess=True` → tek killable child, 150s timeout; Nautilus backtest'i GIL'i tuttuğu için sunucu event loop'unu dondurmasın diye), `run_robustness_guarded` (tüm robustness suite'i tek child'da, 900s; child NON-daemon — kendi worker havuzunu açabilsin), `_start_parent_watchdog` (sunucu ölürse child ≤1s'de self-exit). **Graceful-exit koruması**: çalışan non-daemon child'lar bir `atexit` handler'ıyla (mp'nin atexit join'inden önce, LIFO) terminate edilir — aksi halde zarif shutdown mp'nin join'inde kilitlenirdi | [[crash_only_design]], [[single_threaded_core]] |
| `backtest_robustness.py` | Robustness motoru: `run_walk_forward` / `run_insample_oos_split` / `run_multi_symbol` (+ vektörize `run_monte_carlo`). Üçü de opsiyonel `run_many=` callable ile birim backtest'leri havuza fan-out eder; `_wfo_window_bounds` pencere matematiğini iki yol için paylaşır | [[backtesting_guide]], [[portfolio]] |
| `parallel_exec.py` | `ProcessPoolExecutor` (spawn) havuzu: WFO (pencere × aday) birimlerini çekirdeklere dağıtır (~8.7x, 22 pencere × 20 aday ölçümü). Veri bir kez parquet snapshot'a yazılır, worker initializer bir kez yükler (per-task pickle yok); her worker parent-liveness watchdog taşır (yetim süreç yok). Env: `NAUTILUS_PARALLEL=0` kill-switch, `NAUTILUS_PARALLEL_WORKERS` (varsayılan `cpu//2-2`, clamp [1,28]). Determinizm: aday üretimi (seed'li RNG) parent'ta — kazananlar sıralı yolla birebir aynı | [[crash_only_design]] |
| `wfo_optimizer.py` | WFO parametre arama primitifleri: `build_param_space`, `mutate_spec`, `objective_value`, hafif GA (`ga_plan`/`ga_next_population`/turnuva/elit — deterministik seed'li, sıralı ve paralel yol birebir aynı adaylar), `optimize_window`. **NAU skorlama paritesi**: `objective_value` sharpe objektifi **per-trade sharpe** ((mean/std)×√n) okur — annualized 252-gün DEĞİL; `_calmar` DD_FLOOR=0.01 + CALMAR_CAP=±10; fold-kabul `WF_MIN_VALID_FOLDS_FRAC=0.6` (geçerli-fold oranı eşiği), embargo `WF_EMBARGO_DAYS=2` | [[backtesting_guide]] |

### Data

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `data.py` | Yahoo BTC-USD, Bybit v5 klines, US index tick CSVs. Ayrıca **harici salt-okunur Nautilus katalogları**: `EXTERNAL_CATALOGS` (`NAUTILUS_EXTERNAL_CATALOGS` env, varsayılan NAU_ev — 591 US-equity), `/data` sayfasında `xq` filtreli panel (`_external_catalog_rows` — bar sayıları parquet footer'dan, kopyasız) ve `load_external_bars` ile /backtest'te tek-atışlık External kaynak (kapanış→açılış kaydırması; cache `~/.cache/.../external/`, harici köke asla yazılmaz) | [[data_engine]] Layer-1 ingest bacağı; per-source parquet cache = küçük [[parquet_data_catalog]] analogu; `_prepare_df` OHLCV normalizasyonu; [[bar_aggregation_and_type_syntax]] `BarType` string üretimi burada; [[index_backtest_via_equity_proxy]] tick→bar resample deseni |

### Runtime

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `state.py` | In-memory session state (`IterationResult` listesi) | [[cache]] (ephemeral, single-source-of-truth, restart'ta boş) |
| `loop_runner.py` | Background loop: `agent`- veya `catalog`-mode; `symbol/category/interval` parametreli | [[crash_only_design]] (her iterasyon idempotent; engine yeniden yaratılır) |
| `agent.py` | Claude tabanlı strateji önerici + custom block AST-validated code generation; `hint` parametresi `propose_composed_strategy`'e iletiliyor; `_get_client` thread-safe double-checked lock. LLM backend'i seçilebilir (`NAUTILUS_LLM_BACKEND=auto\|api\|claude-cli`): `ANTHROPIC_API_KEY` yoksa `claude -p` headless CLI ile abonelik (OAuth) üzerinden çağrı — system prompt `--system-prompt-file` ile (Windows 8K sınırı), araçlar kapalı | (app-spesifik) |

### Ops

| Modül | Rol | Wiki karşılığı |
|---|---|---|
| `capture_baseline.py` | Migration-öncesi parite snapshot'ı | [[v1_to_v2_migration_lessons]] "6 catalog spec bit-identical parite" iddiasını üretir |
| `wiki_helper.py` | `[[bare]]` → `/wiki/*` HTML rewriter | (app-spesifik) |
| `nautilus_wiki/tools/wiki_tools.py` | Wiki CLI | (app-spesifik) |

## Uçtan uca akış

Kullanıcı web UI'da bir strateji seçip **Run backtest**'e tıkladığında olan sıra:

1. **State** — `web/routes/backtest.py` spec'i `state.py`'den okur (draft/composed).
2. **Data** — `data.py:load_btc_bars()` cached parquet döndürür ([[data_engine]] ingest paritesi).
3. **Engine seçimi** — `web/routes/backtest.py` **otomatik seçim** yapar:
   - Katalogda veri varsa → `backtest.py:run_composed_backtest_node` (**BacktestNode** yolu)
   - Yoksa → `backtest.py:run_composed_backtest` (**BacktestEngine** yolu)
4. **BacktestNode yolu** — `run_composed_backtest_node(spec, instrument_id, bar_type, start_ns, end_ns, ...)`:
   - `BacktestNode + ParquetDataCatalog` kullanır
   - `ImportableStrategyConfig` ile `composer:ComposedStrategy` yükler
   - `BacktestDataConfig(bar_types=[bar_type_str], start_time=start_ns, end_time=end_ns)` ile veri filtresi
   - Per-trade detayı (giriş/çıkış zamanları) yok — `total_positions` ve özet metrikler döner
5. **BacktestEngine yolu** — `run_composed_backtest`:
   - `BacktestEngineConfig(...)` + `BacktestEngine(config=...)` ([[nautilus_kernel]] küçük ölçekte)
   - `add_venue` + `add_instrument` + `add_data(bars)` + `add_strategy(spec)`
6. **Nautilus** — Strategy `submit_order` → [[order_flow_pipeline]] (OrderEmulator → ExecAlgo → RiskEngine → Adapter → Venue)
7. **Metrics** — `engine.portfolio.statistics()` ([[portfolio]] surface, v2 rc1 sharpe NaN buglığıyla) → `_metrics()` → `_equity_curve()`
8. **State** — Sonuç `IterationResult` olarak state'e append ([[cache]] eşdeğeri)
9. **UI** — HTMX fragment ile ekranda güncelleme.

## Karpathy loop: neden bu köprü var

Karpathy'nin "Explorations always add up in the knowledge base" prensibi:

- **Kod → wiki**: `wiki_helper.py`, `tools/wiki_tools.py` ve `nautilus_wiki/wiki/**` wiki'yi ekose halinde tutar.
- **Wiki → kod**: bu sayfa + her modülün `Wiki References` bloğu ters yönü kurar. Böylece yeni bir developer wiki üzerinden koda, kod üzerinden wiki'ye gidebilir; LLM ajanları ise dediklerini kod tabanında doğrulayabilir.

Örnek: [[v1_to_v2_migration_lessons]] sayfası "portfolio.statistics().returns içinde Sharpe NaN" der. `backtest.py:_metrics` bu davranışı Sortino'ya düşerek yönetir; kaynak dosyanın `Wiki References` bloğu tam olarak bu sayfayı gösterir. Wiki iddiası ↔ kod uygulaması bağlantı zinciri açıktır.

## Sağlamlaştırma & regresyon (2026-07)

- **NAU-uyum denetimi** (fonksiyon-fonksiyon, `NAU_ev` referansına karşı): sayısal çekirdek — fitness formülü, calmar cap/floor, junk kapısı, WF fold/embargo/60-gün holdout, `MIN_VALID_FOLDS_FRAC`, **per-trade sharpe**, profit_factor cap (99.0), indikatör port'u — NAU ground-truth'una sadık doğrulandı.
- **NAU per-trade sharpe hizalaması**: composite skorun 0.3 terimi annualized 252-gün Sharpe yerine per-trade sharpe kullanacak şekilde değişti (`_score` + `objective_value`). Backtest metrikleri değişmez, yalnız kazanan-seçimi; bu tarihten önceki mutlak skorlar yeni skorlarla kıyaslanamaz.
- **Yürütme-yolu düzeltmeleri**: composer NAU_WINDOW buffer + flip GTC-birikme, codegate loop-budget (helper içi sonsuz döngü artık yakalanıyor), sandbox graceful-exit, katalog yazım kilidi (delete+write atomik), data force_refresh cache-merge + inverse-USD.
- **Birleşik backtest UI + çoklu-TF (2026-07-18)**: `/backtest` "01" paneli tarif-odaklı tek akışa indirildi — kayıtlı-strateji dropdown'ı ve instrument-kind seçici kaldırıldı; stratejiyi tarif et → Claude blok yazar → seçilen zaman dilimlerinin hepsinde backtest. `POST /backtest/sweep` aynı spec'i çoklu-TF'de koşup karşılaştırma tablosu üretir; `describe` 2+ TF'de sweep'e, 1 TF'de run'a zincirler. Sembol datalist typeahead. Ayrı ayrıca **2-TF trend filtresi** manuel composer'a açıldı (yukarı bkz.).
- **Regresyon süiti**: **289 test** (pytest); NAU sabitleri, sizing modları, fold-kabul, codegate red-matrisi, force_refresh-merge, delete-skip, flip yolu, per-trade-sharpe hizalaması, describe→sweep çoklu-TF zinciri, MTF trend filtresi ve LLM kredi-fallback dahil çapalar. Ruff lint/format temiz (PostToolUse hook).

## Bilinen boşluklar

- BacktestNode yolunda per-trade detayı (giriş/çıkış zamanları, individual fill price, karar sebebi) şu an yok — sadece `total_positions` ve özet metrikler mevcut. **BacktestEngine yolu** artık per-trade giriş/çıkış sebebini (ateşleyen blok + değerleri, SL/TP/flip/eob atfı) `composer._decision_log` + emir tag'leri üzerinden yakalar; sebepler `/reports/detail` panelinde ve /backtest sonucunda görünür. Node yolunda strateji instance tutulmadığı için bu mekanizma kullanılamaz.
- Live [[environment_contexts]] Sandbox/Live bacağı hiç kurulmadı; `TradingNode` webapp'in scope'unda değil ama [[live_node]] stub'ı Nautilus tarafında bunu bekliyor.
- [[option_greeks_pipeline]] ve [[venue_reconciliation]] webapp'te ilgisiz — bunlar sadece Nautilus wiki tarafında yaşıyor.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[getting_started_roadmap]]
- [[v1_to_v2_migration_lessons]]
<!-- BACKLINKS:END -->
