# nautilus_web_app — Mimari

Bu belge repo kodunu **wiki'nin Nautilus modelinden okuma** açısından düzenler.
Her modülün üstündeki `Wiki References` bloğu ile birlikte oluşturur; wiki
`nautilus_wiki/` altında (bkz. [nautilus_wiki/CLAUDE.md](nautilus_wiki/CLAUDE.md)).

Uygulama Nautilus'un **düşük seviyeli** yolunu kullanır (`BacktestEngine`),
ancak yapısal olarak yüksek seviyeli yola (`BacktestNode` +
`ParquetDataCatalog`) yakınsayacak şekilde şekillendirilmiştir.

## Katmanlar

```
┌──────────────────────────────────────────────────────────────────────┐
│  Web UI (FastAPI, HTMX, Jinja)                                        │
│    server.py                          entrypoint  (see: nautilus_kernel)
│    web/routes/dashboard.py            /                                │
│    web/routes/strategy.py             /strategy   (see: strategy_and_actor,│
│                                                    order_flow_pipeline) │
│    web/routes/backtest.py             /backtest   (see: backtesting_guide)│
│    web/routes/agent_backtest.py       /agent      (5-faz otonom pipeline; │
│                                                    sandbox'lı backtest'ler)│
│    web/routes/robustness.py           /robustness (IS/OOS + WFO + MC)    │
│    web/routes/lab.py                  /lab        (Strategy Lab)         │
│    web/routes/chart.py                /chart/data (OHLCV + indikatör JSON)│
│    web/routes/reports.py              /reports    (geçmiş çalışmalar)    │
│    web/routes/sessions.py             /sessions   (agent session logları)│
│    web/routes/loop.py                 /loop       (see: crash_only_design)│
│    web/routes/wiki.py                 /wiki       (Karpathy frontend)   │
│    web/routes/fragments.py            /fragments  (HTMX partials)       │
├──────────────────────────────────────────────────────────────────────┤
│  Domain (Nautilus surface)                                              │
│    backtest.py            BacktestEngine wrapper                        │
│                             (see: backtest_node, backtesting_guide,     │
│                              environment_contexts, data_wranglers,      │
│                              precision_modes, portfolio,                │
│                              v1_to_v2_migration_lessons,                │
│                              index_backtest_via_equity_proxy)           │
│    strategies.py          MACrossover, RSIMeanReversion                 │
│                             (see: strategy_and_actor,                   │
│                              v1_to_v2_migration_lessons)                │
│    composer.py            Block-based ComposedStrategy generator        │
│                             (see: strategy_and_actor, order_flow_pipeline)│
│    custom_block_store.py  On-disk registry of user-authored blocks      │
│                             (see: strategy_and_actor)                   │
├──────────────────────────────────────────────────────────────────────┤
│  Motor (izolasyon + paralel yürütme)                                     │
│    sandbox.py             Killable process sandbox                       │
│                             (run_backtest_guarded 150s,                  │
│                              run_robustness_guarded 900s non-daemon,     │
│                              parent-watchdog; see: crash_only_design)    │
│    backtest_robustness.py IS/OOS + WFO + MC + multi-symbol motoru        │
│                             (run_many= fan-out; see: backtesting_guide)  │
│    parallel_exec.py       ProcessPoolExecutor havuzu (~8.7x WFO;         │
│                             NAUTILUS_PARALLEL / _WORKERS env;            │
│                             snapshot + worker watchdog)                  │
│    wfo_optimizer.py       WFO param arama (generate_candidates            │
│                             deterministik seed'li çekiliş)               │
├──────────────────────────────────────────────────────────────────────┤
│  Data                                                                    │
│    data.py                Yahoo BTC-USD, Bybit v5 klines, index ticks,  │
│                             harici salt-okunur Nautilus katalogları      │
│                             (NAUTILUS_EXTERNAL_CATALOGS → NAU_ev 591     │
│                              US-equity; load_external_bars)              │
│                             (see: data_engine, data_wranglers,           │
│                              parquet_data_catalog,                       │
│                              bar_aggregation_and_type_syntax,           │
│                              index_backtest_via_equity_proxy)           │
├──────────────────────────────────────────────────────────────────────┤
│  Runtime                                                                 │
│    state.py               In-memory session state  (see: cache)         │
│    loop_runner.py         Background agent/catalog loop  (see: crash_only_design)│
│    agent.py               Claude-based param proposer  (app-specific;   │
│                             NAUTILUS_LLM_BACKEND=auto|api|claude-cli —   │
│                             API key yoksa `claude -p` abonelik backend'i)│
├──────────────────────────────────────────────────────────────────────┤
│  Ops                                                                     │
│    capture_baseline.py    Pre-migration parity snapshot                 │
│                             (see: v1_to_v2_migration_lessons)           │
│    wiki_helper.py         [[bare]] → /wiki/* HTML rewriter              │
│    nautilus_wiki/tools/wiki_tools.py  CLI (index/backlinks/lint/search/…)│
└──────────────────────────────────────────────────────────────────────┘
```

## Nautilus Karşılığı (webapp ↔ wiki)

| webapp                          | Nautilus wiki page                                 | notes |
|---------------------------------|----------------------------------------------------|-------|
| `server.py` lifespan            | [[nautilus_kernel]]                                | "compose then run" — küçük ölçekte |
| `data.py` cache/refresh         | [[data_engine]] + [[parquet_data_catalog]] (stub) | Parquet cache = katalog analogu, tek-instrument |
| `_bars_from_df`                 | [[data_wranglers]] (stub) v2 bölümü               | `BarDataWrangler.process(df)` v2'de kaldırıldı, biz `Bar()` direkt kuruyoruz |
| `strategies.py` StrategyConfig  | [[strategy_and_actor]] + [[v1_to_v2_migration_lessons]] | v2 plain-class kalıbı zaten uygulanıyor |
| `composer.py` ComposedStrategy  | [[strategy_and_actor]] altında custom Strategy    | Bloklar `evaluate` → signal, composer bloğu `submit_order`'a bağlar |
| `submit_order` çıkışı           | [[order_flow_pipeline]]                            | Emir → OrderEmulator → ExecutionAlgorithm → RiskEngine → Adapter |
| `run_backtest` in-process       | [[backtesting_guide]] low-level yolu               | `BacktestEngine` doğrudan; küçük veri için hızlı |
| Backtest venue                   | [[environment_contexts]] Backtest bacağı           | Aynı motor sandbox/live'da kullanılabilir |
| `portfolio.statistics()`        | [[portfolio]] (stub)                               | Tarihsel not: v2 rc1 sharpe/vol NaN bug'ı burada gözlemlenmişti; uygulama şu an 1.230.0 ile sabitlenmiş durumda |
| `state.py` in-memory dict       | [[cache]]                                          | Ephemeral; restart'ta sıfırlanır |
| Loop `catalog`/`agent` mode     | [[crash_only_design]]                              | Her iterasyon idempotent, engine yeniden yaratılır |
| `capture_baseline.py`           | [[v1_to_v2_migration_lessons]]                     | "6 catalog spec bit-identical parite" iddiasını üretir |
| US index tick backtest          | [[index_backtest_via_equity_proxy]]                | Nauitlus'te tradable olmayan IndexInstrument yerine Equity proxy |

## Uçtan uca akış

```
[user]                  compose blocks / pick strategy
   ↓                    (web/routes/strategy.py)
[state]                 draft spec  (state.py: get_state())
   ↓
[user]                  hit "Run backtest"
   ↓                    (web/routes/backtest.py POST /backtest/run)
[server]                → run_composed_backtest(spec, get_bars())
   ↓                    (backtest.py)
[data.py]               load_btc_bars() → cached parquet
   ↓
[backtest.py]           BacktestEngine + add_venue + add_instrument + add_data + add_strategy
   ↓                    engine.run()
[Nautilus]              Strategy.submit_order → [[order_flow_pipeline]]
   ↓                    Fill events → Portfolio
[backtest.py]           _metrics() + _equity_curve() → IterationResult
   ↓
[state]                 append iteration; broadcast to fragments
   ↓
[user UI]               dashboard chart, backtest result panel
```

## Wiki reflection

Her modülün en üst docstring'inde `Wiki References` bölümü var; wiki sayfası
tarafında karşılığı [wiki/synthesis/webapp_module_map.md](nautilus_wiki/wiki/synthesis/webapp_module_map.md).

Uygulamayı çalıştırmak için `.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000`.
Wiki'yi Obsidian vault olarak açıp graph view'da bağları görselleştirin:
`nautilus_wiki/` klasörünü Obsidian'a sürükleyin.

## Sınırlar

- Wiki'nin `sources/` katmanı **immutable**; kod bu klasörü hiç değiştirmez.
- `nautilus_wiki/wiki/**` katmanı LLM'in tam sahibi; kod cross-reference için
  okur, ancak yazımı `tools/wiki_tools.py` üzerinden yapılır.
- Docstring `Wiki References` blokları scriptle güncellenir; elle
  düzenlerken `----` fenced bölge yapısını koruyun.
