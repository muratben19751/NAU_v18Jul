---
title: Accounting
type: concept
summary: Her hesap için bakiye, margin ve PnL takibi; CASH/MARGIN/BETTING hesap tipleri ve para birimi dönüşümleri — backtest ile live'da aynı model.
status: draft
key_concepts:
  - positions
  - portfolio
  - execution_engine
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/accounting.md
last_updated: 2026-07-13
---

# Accounting

Accounting alt sistemi, platformun etkileşimde olduğu her hesap için bakiye, margin ve PnL takibini yapar; backtest ve live'da aynı model geçerlidir. Pozisyon düzeyinde PnL'in nasıl üretildiği [[positions]] sayfasında, hesaplar arası toplulaştırma (equity, exposure, mark-to-market) [[portfolio]] tarafındadır.

## Hesap tipleri
Venue motora bağlanırken `account_type` ile üç moddan biri seçilir (v1.x):

| Tip | Tipik kullanım | Motorun kilitlediği |
|---|---|---|
| **CASH** | Spot (BTC/USDT, hisse) | Bekleyen emirlerin açacağı notional değer |
| **MARGIN** | Türevler, kaldıraçlı ürünler | Emir başına initial margin + açık pozisyonlar için maintenance margin |
| **BETTING** | Bahis / prediction market | Venue'nun istediği stake; kaldıraç/margin yok |

Reduce-only emirler maruziyeti yalnızca azaltabildiğinden CASH'te `balance_locked`'a, MARGIN'de initial margin'e katkı yapmaz.

## Bakiye modeli
`AccountBalance` aynı para biriminde üç değer tutar: `total` (venue'nun raporladığı), `locked` (açık emir/pozisyonlara rezerve), `free` (`total - locked`). Değişmez kural: `total == locked + free` her zaman, para birimi hassasiyetinde sağlanmalıdır. Rust adaptörlerinde `from_total_and_locked` / `from_total_and_free` türetilmiş kurucuları eksik alanı hesaplar ve `[0, total]` aralığına kırparak (clamp) invariant'ı merkezi olarak garanti eder; Python kurucusu üç alanı da ister (v1.x).

## Margin kapsamları
`MarginBalance` dört alan taşır: `initial`, `maintenance`, `currency` ve opsiyonel `instrument_id`. Bu son alan iki kapsamdan birini seçer:
- **Per-instrument** (`instrument_id` dolu): isolated margin veya backtest'te `AccountsManager`'ın yerel hesapladığı margin.
- **Account-wide** (`instrument_id=None`): cross-margin venue'ların collateral para birimi başına raporladığı tek toplam; giriş `currency` ile anahtarlanır.

İki kapsam aynı `MarginAccount` üzerinde ayrı depolarda birlikte yaşar. Kritik davranış: `MarginAccount.apply()` gelen `AccountState` event'inden her iki depoyu da **değiştirir (replace)**, önceki durumla birleştirmez — kısmi snapshot yollayan adaptörler her güncellemede tüm canlı margin girişlerini içermek zorundadır, yoksa eksikler düşer. Sentetik `ACCOUNT.{VENUE}` placeholder'ları kullanılmaz (v1.x).

## Sorgu API'si
Sorguyu venue'nun raporlama şekline göre seç:

| İstenen değer | Metot |
|---|---|
| Per-instrument (isolated) | `margin(id)`, `margin_init(id)`, `margin_maint(id)`, `margins()` |
| Account-wide (tek collateral) | `margin_for_currency(ccy)`, `account_margins()` |
| İki kapsamın toplamı | `total_margin_init(ccy)`, `total_margin_maint(ccy)` |

Nokta sorguları giriş yoksa `None`, toplam sorguları her zaman `Money` (yoksa sıfır) döner. [[portfolio|Portfolio]] düzeyinde `margins_init/margins_maint` yalnız per-instrument girişleri yansıtır; `unrealized_pnls`, `realized_pnls`, `net_exposures`, `mark_values`, `equity` sorguları `venue` ve çoklu-hesap venue'lar için opsiyonel `account_id` alır (v1.x).

## Margin modelleri
Modeller yalnızca **hesaplanan** yolda çalışır (backtest ve `calculate_account_state=True` ile live reconciliation); venue'nun raporladığı marginler modele girmeden depolara akar. İki yerleşik model, notional'ın yüzdesi olarak margin hesaplar; fark kaldıracın bölünüp bölünmemesidir:
- **StandardMarginModel**: `notional * instrument.margin_init` — kaldıraç yok sayılır (geleneksel broker, ör. Interactive Brokers).
- **LeveragedMarginModel** (varsayılan): `(notional / leverage) * instrument.margin_init` — kripto borsası davranışı.

Örnek — EUR/USD, 100k EUR @ 1.10 (notional $110,000), 50x kaldıraç, %3 `margin_init`: Standard $3,300; Leveraged $66. $10,000'lık hesapta Standard model işlemi bloklar, Leveraged izin verir.

```python
from nautilus_trader.backtest.models import StandardMarginModel

account.set_margin_model(StandardMarginModel())  # varsayılan: LeveragedMarginModel
```

Özel model için `MarginModel` alt sınıflanır ve `MarginModelConfig` ile yapılandırılır; backtest genelinde `BacktestVenueConfig` üzerinden seçilir (v1.x). Bkz. [[backtesting_guide]].

## Bilinen boşluklar
- HEDGING modunda margin hesabı için alt-pozisyonların `ts_opened` sırasıyla varsayımsal NETTING pozisyonuna netlenmesi (replay kuralları) burada özetlenmedi — upstream "HEDGING-mode netting" bölümüne bakılmalı.
- Rust tarafı API adları (`account_margin`, `total_initial_margin` vb.) ve adapter convention tabloları kısaltıldı.
- Betting hesaplarının stake/payout mekaniği upstream'de de yüzeysel; Betfair adaptör dokümanı ayrıca incelenmeli.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[events]]
- [[portfolio]]
- [[positions]]
- [[vol_targeted_trend]]
<!-- BACKLINKS:END -->
