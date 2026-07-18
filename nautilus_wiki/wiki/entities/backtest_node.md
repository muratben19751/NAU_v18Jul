---
title: BacktestNode
type: entity
status: draft
sources:
  - sources/04_backtesting_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/backtesting
  - https://nautilustrader.io/docs/nightly/getting_started/backtest_high_level
last_updated: 2026-07-10
summary: BacktestNode — çoklu BacktestEngine instance'ını yöneten yüksek seviyeli katalog-güdümlü orkestratör; üretim iş akışı için önerilen yol.
related:
  - wiki/synthesis/backtesting_guide.md
  - wiki/tutorials/tutorial_backtest_high_level.md
---

# BacktestNode

Yüksek seviyeli, yapılandırma güdümlü geri test orkestratörü. Resmi dokümantasyon BacktestNode'u şöyle tanımlar:

> **"BacktestNode is the higher-level API that orchestrates the management of multiple BacktestEngine instances."**

Ve doğrudan önerir:

> **"Use BacktestNode for config-driven backtesting with the Parquet data catalog. This is the recommended path for production workflows because the strategies, actors, and execution algorithms you build here carry forward to live trading with TradingNode."**

(kaynak: docs/nightly/getting_started/backtest_high_level, docs/latest/concepts/backtesting)

## BacktestEngine vs BacktestNode — Resmi Ayrım

| | BacktestEngine | BacktestNode |
|---|---|---|
| API seviyesi | Low-level — manual component setup | High-level — config-driven |
| Veri kaynağı | DataFrame (direkt) | [[parquet_data_catalog]] |
| Kullanım | Küçük veri, öğrenme, hızlı deney | Üretim, büyük veri, paralel sweep |
| TradingNode uyumu | Strateji kodu taşınabilir | Tam uyumlu |

## Yapılandırma Katmanları

- `BacktestRunConfig` — kök; `engine`, `data`, `venues` alanlarını tutar (partialable — parametre sweep için klonlanabilir)
- `BacktestEngineConfig` — motor ayarları
- `BacktestVenueConfig` — venue tanımı
- `BacktestDataConfig` — veri kaynağı bildirimi (`catalog_path`, `data_type`, `instrument_id`, `bar_types`)

Strateji `ImportableStrategyConfig` ile bildirilir. v1.229.0'dan itibaren `add_native_exec_algorithm` ve `ExecutionAlgorithmConfig` Python v2 backtest engine binding'leri de mevcut.

### BacktestDataConfig — v2.0.0rc1'de Kritik Notlar

**`bar_types` parametresi (filtre):**
```python
BacktestDataConfig(
    catalog_path=str(catalog.path),
    data_type=Bar,
    instrument_ids=["BTCUSDT.BYBIT"],
    bar_types=["BTCUSDT.BYBIT-1-MINUTE-LAST-EXTERNAL"],  # string listesi
    start_time=start_ns,   # nanosecond int — datetime geçirme!
    end_time=end_ns,       # nanosecond int — datetime geçirme!
)
```

- **`bar_spec` parametresi v2.0.0rc1'de desteklenmiyor** — `TypeError` alınır. Bunun yerine `bar_types=[str]` kullan.
- **`start_time` / `end_time` nanosecond int olmalı.** `datetime` nesnesi geçirilirse `TypeError` alınır. Dönüşüm: `pd.Timestamp("2024-01-01").value` veya `int(pd.Timestamp(...).timestamp() * 1e9)`.

### ImportableStrategyConfig ile ComposedStrategy Yükleme

```python
from nautilus_trader.config import ImportableStrategyConfig

importable_cfg = ImportableStrategyConfig(
    strategy_path="composer:ComposedStrategy",    # modül:sınıf
    config_path="composer:ComposedStrategyConfig",
    config={
        "instrument_id": "BTCUSDT.BYBIT",   # str — Config.__init__ içinde dönüştürülmeli
        "bar_type": "BTCUSDT.BYBIT-1-MINUTE-LAST-EXTERNAL",
        # diğer parametreler...
    },
)
```

`ImportableStrategyConfig`, `config` alanını dict (str değerler) olarak iletir; bu nedenle `StrategyConfig.__init__` içinde `str → BarType / InstrumentId` dönüşümü **zorunludur** (bkz. [[strategy_and_actor]]).

### BacktestResult — Sonuç Metrikleri

- `result.total_positions` → backtest boyunca açılan toplam pozisyon (trade) sayısı.
- `result.stats_general` sözlüğünde "Total Trade Count" v2 rc1'de bulunmuyor; `total_positions` kullan.

### Hız Referansı (webapp'te ölçüldü)

| Senaryo | Süre |
|---|---|
| 7 gün × 1m bar (≈10K bar) — BacktestNode | ~0.26s |
| 7 gün × 1m bar (≈10K bar) — BacktestEngine | ~0.15-0.26s |
| 3.3M bar — BacktestNode | ~582s (Python callback dar boğazı) |

## Tipik Veri Hattı

CSV/API → DataFrame → `QuoteTickDataWrangler` → [[parquet_data_catalog]].write_bars() → `BacktestDataConfig` ile yükle → `BacktestNode.run()` → `BacktestResult`

## v2.0.0rc1'de Çalışma Sırası (webapp'te doğrulandı)

```python
node = BacktestNode(configs=[run_config])
node.build()   # build() ÖNCE; add_strategy_from_config SONRA
node.add_strategy_from_config(run_config.id, importable_cfg)
results = node.run()
```

`node.build()` çağrılmadan `add_strategy_from_config` yapılırsa `RuntimeError: No engine for run config` hatası alınır.

## İlgili Sayfalar

- [[backtesting_guide]]
- [[parquet_data_catalog]]
- [[getting_started_roadmap]]
- [[live_node]] — TradingNode karşılığı

## Bilinen boşluklar

- Paralel multi-config yürütme mekaniği (thread vs process) dokümante değil.
- `BacktestEngineConfig` içindeki cache/exec/risk alt-konfigürasyonlar sentezlenmedi.
- `BacktestResult.stats_general` sözlüğünde "Total Trade Count" v2 rc1'de yok — `total_positions` kullan.
- 3.3M bar gibi büyük veri setlerinde BacktestNode Python callback'leri hâlâ dar boğaz; Rust-side aggregation mevcut değil.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
- [[getting_started_roadmap]]
- [[live_node]]
- [[option_greeks_pipeline]]
- [[parquet_data_catalog]]
- [[visualization]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
