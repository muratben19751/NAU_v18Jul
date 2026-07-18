---
title: Reports
type: concept
summary: ReportProvider, cache'teki order/fill/position/account verisini pandas DataFrame raporlarına çevirir — performans, execution kalitesi ve PnL doğrulama.
status: draft
key_concepts:
  - positions
  - execution_engine
  - venue_reconciliation
  - cache
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/reports.md
last_updated: 2026-07-13
---

# Reports

`ReportProvider` sınıfı, [[cache]]'teki ham order/fill/position/account verilerini analiz ve görselleştirme için pandas DataFrame'lerine dönüştürür. Amaç strateji performansını değerlendirmek, execution kalitesini incelemek ve PnL muhasebesini doğrulamaktır. Aynı raporlar backtest ve live'da tutarlı çalışır. İki kullanım yolu vardır: önerilen `trader.generate_*_report()` yardımcı metotları veya veri seçimini elle kontrol etmek için doğrudan `ReportProvider`'ın statik metotları (v1.x).

## Rapor türleri
| Rapor | İçerik | Not |
|---|---|---|
| Orders | Tüm emirler: ID'ler, side/type/status, quantity, `avg_px`, timestamp'ler | Index: `client_order_id`; kolonlar order tipine göre değişir |
| Order fills | Emir başına tek satır | Yalnız `filled_qty > 0`; timestamp'ler datetime'a çevrilir |
| Fills | Fill event başına tek satır | `trade_id`, `last_px/last_qty`, `liquidity_side` (MAKER/TAKER), komisyon |
| Positions | Pozisyon yaşam döngüsü: giriş/çıkış fiyatları, `peak_qty`, realized PnL | NETTING'de snapshot'ları da içerir; `is_snapshot` kolonu |
| Account | Zaman içinde bakiye/margin değişimi | Çoklu para birimi → hesap durumu başına birden çok satır; `venue` parametresi ister |

```python
# Trader yardımcı metodu — NETTING OMS'te snapshot'ları otomatik dahil eder
positions_report = trader.generate_positions_report()

# Veya doğrudan ReportProvider ile
from nautilus_trader.analysis import ReportProvider
report = ReportProvider.generate_positions_report(
    positions=cache.positions(),
    snapshots=cache.position_snapshots(),  # NETTING için zorunlu
)
```

## PnL muhasebesi
- **OMS tipi belirleyicidir**: NETTING'de pozisyon kapanıp yeniden açıldığında tarihsel PnL snapshot'ta korunur — doğru toplam PnL için snapshot'lar rapora mutlaka dahil edilmelidir. HEDGING'de her pozisyonun ID'si benzersiz olduğundan ve yeniden açılmadığından snapshot kullanılmaz. Ayrıntı: [[positions]].
- **Komisyon etkisi**: realized PnL'e yalnız settlement currency'deki komisyonlar dahildir.
- **Çoklu para birimi**: her pozisyon PnL'i kendi settlement currency'sinde tutar; portföy toplaması için döviz çevrimi kullanıcıya aittir (kur verisi dışarıdan sağlanır).

## Backtest sonrası analiz
Koşu bittikten sonra `engine.get_result()` istatistik kategorilerini verir (`stats_pnls`, `stats_returns`, `stats_general` — Sharpe, Profit Factor, Win Rate vb.); raporlar `engine.generate_fills_report()` / `generate_account_report(venue=...)` ile motor üzerinden de üretilebilir. `create_tearsheet(engine, output_path="tearsheet.html")` equity eğrisi, drawdown, aylık getiri ısı haritası ve istatistik tablosu içeren interaktif Plotly raporu yazar (`nautilus_trader[visualization]` extra'sı gerekir; ayrıntı [[visualization]]) (v1.x). Backtest kurulumu için bkz. [[backtesting_guide]]. Live'da bir Actor, `clock.set_timer` ile periyodik rapor üretip CSV'ye yazabilir.

## ExecutionReports ile ilişki
Bu sayfadaki raporlar **analitik çıktılardır**: [[cache]]'ten okunur, DataFrame olarak sunulur, sistem durumunu değiştirmez. Bunlar, [[venue_reconciliation]] sürecinde kullanılan **ExecutionReports** ailesinden (`OrderStatusReport`, `FillReport`, `PositionStatusReport`) farklıdır — o raporları adaptörler venue'dan üretir ve [[execution_engine|ExecutionEngine]] başlangıçta/çalışma anında iç durumu venue gerçeğiyle hizalamak için tüketir. İkisi birbirini tamamlar: reconciliation Cache'i doğru tutar, `ReportProvider` da o doğru durumdan analiz üretir. Pozisyon tarafında `position.events` ve `position.trade_ids` broker ekstresi eşleştirmesi için ham kanıt sağlar.

## Bilinen boşluklar
- Upstream reports.md ExecutionReports/reconciliation mekaniğini anlatmıyor; yukarıdaki ilişki execution dokümanlarından sentezlendi — alan adları için [[venue_reconciliation]] ve upstream execution guide esas alınmalı.
- Rapor kolon tabloları kısaltıldı; tam alan listeleri `Order.to_dict()` / `OrderFilled.to_dict()` üzerinden.
- `create_equity_curve` gibi tekil grafik yardımcıları ve kaleido PNG export detayı atlandı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[execution_engine]]
- [[positions]]
- [[visualization]]
<!-- BACKLINKS:END -->
