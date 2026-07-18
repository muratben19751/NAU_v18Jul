---
title: Portfolio ve İstatistikler
type: entity
status: draft
sources:
  - sources/03_strategies_docs.md
  - sources/05_latest_docs_research.md
  - https://nautilustrader.io/docs/latest/concepts/portfolio
  - https://nautilustrader.io/docs/latest/concepts/strategies
last_updated: 2026-07-13
summary: Strategy'nin hesap/P&L erişim yüzeyi; multi-currency hesaplarda PortfolioAnalyzer sessizce boş istatistik döndürür; v1.227.0 PortfolioSnapshot eklemesi.
related:
  - wiki/entities/strategy_and_actor.md
  - wiki/synthesis/v1_to_v2_migration_lessons.md
---

# Portfolio ve İstatistikler

Portfolio, [[strategy_and_actor]] içindeki Strategy'nin hesap ve P&L bilgisine eriştiği servistir. Hesap tipleri (CASH/MARGIN/BETTING) ve bakiye/margin hesaplama kuralları için bkz. [[accounting]].

## Strategy Yüzeyi

- `Portfolio.is_net_long` / `.is_net_short` / `.is_flat` — v2'de aynı imzalar
- `account.balance_total(currency) -> Money` — aynı

## v2 İstatistik API'si

v1'deki `engine.portfolio.analyzer.get_performance_stats_returns()` kaldırıldı. v2'de:
- `engine.portfolio.statistics() -> PortfolioStatistics(pnls, returns, general)`
- `engine.get_result() -> BacktestResult` — `stats_returns`, `stats_pnls`, `stats_general` property'leri.

## Kritik: Multi-Currency Hesaplarda Sessiz İstatistik Hatası

Araştırmayla doğrulanmış kritik bulgu (kaynak: docs/latest/concepts/portfolio + kaynak kod analizi):

> `PortfolioAnalyzer._calculate_portfolio_returns()` **çok para birimli hesaplarda (len(balances) != 1) herhangi bir uyarı, log veya exception olmaksızın `_empty_returns()` döndürür.**

Docstring bunu açıkça belgeliyor:
> "Multi-currency accounts are not yet supported; the caller silently receives empty statistics."

**Pratik sonuç:** USDT+BTC çift bakiyeli hesap kullanan backtest'lerde (ki bizim `run_composed_backtest` tam bu şekilde başlatılıyordu — CASH hesap + `[Money(1M, USDT)]` ile `base_currency=None`) Sharpe/Sortino/Volatility NaN döner. Bu v2 rc1 bug'ının asıl nedeni olabilir — BacktestEngine path'inde.

**Çözüm:** `base_currency=USDT` veya `base_currency=USD` ile tek-para-birimi hesap kullanmak. BacktestNode path'inde bu sorun yoktu (BacktestNode stats yolu farklı). Bakınız: [[v1_to_v2_migration_lessons]].

## v1.227.0 Yeni Özellik: PortfolioSnapshot

(kaynak: github.com/nautechsystems/nautilus_trader/releases/tag/v1.227.0)

- `PortfolioSnapshot` event: per-account mark-to-market, `snapshot_interval_ms` ile gatelandı.
- MessageBus API: `subscribe_portfolio_snapshot` ve `publish_portfolio_snapshot` (`events.portfolio`).
- Gerçek zamanlı P&L izleme için live trading'de kullanılabilir.

## Bilinen boşluklar

- `PortfolioStatistics` dataclass'ının tam alan listesi.
- `stats_general`'ın içeriği (v2 rc1'de BacktestNode path'inde sadece "Long Ratio" görünüyor).
- Sharpe NaN bug'ının RC2/final release'te giderilip giderilmediği takibi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[accounting]]
- [[events]]
- [[option_greeks_pipeline]]
- [[positions]]
- [[strategy_and_actor]]
- [[v1_to_v2_migration_lessons]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
