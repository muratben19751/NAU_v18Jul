---
source: https://nautilustrader.io/docs/latest/
retrieved: 2026-07-09
type: docs_research
immutable: true
---

# NautilusTrader v2 Latest Docs — Deep Research Sonuçları

2026-07-09 tarihinde `deep-research` workflow ile 98 subagent, 2.1M token, 924 tool call ile doğrulanmış bulgular.

## Doğrulanmış bulgular (adversarial 3-vote verification)

### Kernel / MessageBus (confidence: HIGH)
- "Within a node, the kernel consumes and dispatches messages on a single thread."
- MessageBus: Publish/Subscribe (broadcasting events) + Request/Response (operations requiring acknowledgment).
- Background services (networking, persistence, adapters) → separate threads or async runtimes → communicate back via MessageBus.
- Kaynak: https://nautilustrader.io/docs/latest/concepts/architecture

### Backtest-Live Parity (confidence: MEDIUM)
- "NautilusTrader deploys backtested strategies to live markets with no code changes." (strategy source code)
- "Only the LiveExecutionEngine performs reconciliation, since backtesting controls both sides of execution."
- Uyarı: "no code changes" strateji kaynak kodu için geçerli; konfigürasyon (reconciliation windows, adapter setup) yine de farklı.
- Kaynak: https://nautilustrader.io/docs/latest/concepts/live

### BacktestNode vs BacktestEngine (confidence: HIGH)
- BacktestNode = "higher-level API that orchestrates the management of multiple BacktestEngine instances" via BacktestRunConfig/BacktestDataConfig/BacktestVenueConfig.
- BacktestEngine = lower-level, manual component setup.
- "Use BacktestNode for config-driven backtesting with the Parquet data catalog. This is the recommended path for production workflows."
- Kaynak: https://nautilustrader.io/docs/latest/concepts/backtesting, https://nautilustrader.io/docs/nightly/getting_started/backtest_high_level

### ParquetDataCatalog Dual-Backend (confidence: HIGH)
- Rust backend: 7 core type (OrderBookDelta, OrderBookDeltas, OrderBookDepth10, QuoteTick, TradeTick, Bar, MarkPriceUpdate)
- PyArrow backend: custom data types (flexible fallback)
- Kritik: "Nautilus strictly expects the initialization timestamp (ts_init) of each bar to represent its closing time to prevent look-ahead bias."
- `ts_init_delta` parametresi: open-timestamped bar verisini uyumlu hale getirir.
- Bug fix v1.231.0 (develop, TBD): "Fixed v2 internal bar aggregation to include the first tick when aggregating from ticks, quotes, or trades in backtests"
- Kaynak: https://nautilustrader.io/docs/latest/concepts/data, https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md

### OrderEmulator + Execution Pipeline (confidence: HIGH)
- Routing: OrderEmulator → ExecAlgorithm (opsiyonel) VEYA doğrudan ExecutionEngine.
- TWAP algoritması zorunlu parametreler: `exec_algorithm_params={"horizon_secs": N, "interval_secs": M}` — ikisi de zorunlu, runtime'da validate edilir.
- v1.229.0: `add_native_exec_algorithm` ve `ExecutionAlgorithmConfig` Python v2 backtest engine binding'leri eklendi.
- Kaynak: https://nautilustrader.io/docs/latest/concepts/execution, https://github.com/nautechsystems/nautilus_trader/releases

### Portfolio Analytics Bug (confidence: HIGH)
- `PortfolioAnalyzer._calculate_portfolio_returns()` çok para birimli hesaplarda (len(balances) != 1) sessizce `_empty_returns()` döndürür — uyarı/log yok.
- Docstring bunu belgeliyor: "Multi-currency accounts are not yet supported; the caller silently receives empty statistics."
- Sharpe/Sortino NaN sorununun asıl nedeni bu olabilir (USDT+BTC çift bakiyeli hesap).
- v1.227.0 yeni eklenti: `PortfolioSnapshot` event → per-account mark-to-market; `snapshot_interval_ms` ile gatelandı; `subscribe_portfolio_snapshot` / `publish_portfolio_snapshot` MessageBus API.
- Kaynak: https://nautilustrader.io/docs/latest/concepts/portfolio, https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.227.0
