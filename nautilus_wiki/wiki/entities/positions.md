---
title: Positions
type: entity
summary: Bir instrument'taki net maruziyeti temsil eder — ExecutionEngine fill'lerden açar/toplar/kapatır; netting vs hedging OMS'leri ve realized/unrealized PnL.
status: draft
key_concepts:
  - accounting
  - portfolio
  - execution_engine
  - orders
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/positions.md
last_updated: 2026-07-13
---

# Positions

Position, bir instrument'taki net piyasa maruziyetini (exposure) temsil eder. Strateji kodu pozisyon nesnesi yaratmaz: [[execution_engine|ExecutionEngine]] ilk fill'de pozisyonu açar, sonraki fill'leri toplar, net miktar sıfırlanınca kapatır. Durum ve snapshot'lar [[cache]]'te saklanır; [[portfolio|Portfolio]] bunları instrument/strateji bazında toplayarak hesap-düzeyi PnL üretir. Hesap bakiyeleri ve margin tarafı [[accounting]] sayfasındadır.

## Yaşam döngüsü
- **Açılış**: NETTING OMS'te instrument başına ilk fill'de tek pozisyon; HEDGING OMS'te her yeni `position_id` için ayrı pozisyon (v1.x).
- **Güncelleme**: her fill ile miktarlar toplanır; ortalama giriş/çıkış fiyatı, `peak_qty`, order/trade ID listeleri ve para birimi bazında komisyonlar güncellenir.
- **Kapanış**: net miktar sıfır → `FLAT`. Kapatan emir, `duration_ns` ve nihai realized PnL kaydedilir.

Strateji içinden erişim: `self.cache.position(position_id)` veya `self.cache.positions(instrument_id=...)`.

## OMS tipleri: NETTING vs HEDGING
| Özellik | NETTING | HEDGING |
|---|---|---|
| Instrument başına pozisyon | Tek | Birden çok eşzamanlı |
| Fill'ler arası netting | Otomatik | Yok — bağımsız takip |
| Yön değişimi (flip) | Aynı pozisyon LONG↔SHORT döner | Yeni pozisyon açılır |
| Kapanan pozisyon | Snapshot alınıp resetlenir | Yeniden açılmaz; yeni fill yeni pozisyon |

Strateji OMS'i ile venue OMS'i ayrı yapılandırılabilir (ör. venue HEDGING iken Nautilus tarafında tek NETTING pozisyonu tutulur). Çoğu senaryoda ikisini hizalı tutmak pozisyon yönetimini basitleştirir (v1.x). HEDGING'de her pozisyon margin'i bağımsız tükettiğinden gereksinim artar; bazı venue'lar gerçek hedging desteklemez.

## Fill toplama: signed_qty
Pozisyon net maruziyeti işaretli miktarla tutar: pozitif = `LONG`, negatif = `SHORT`, sıfır = `FLAT`.

```python
# BUY 100 @ $50   -> signed_qty = +100  (LONG)
# SELL 150 @ $55  -> signed_qty = -50   (SHORT'a flip)
# BUY 50 @ $52    -> signed_qty = 0     (FLAT, kapandı)
```

## PnL hesapları
- **Realized**: kısmi/tam kapanışta `(exit - entry) * closed_qty * multiplier`; inverse instrument'larda taraf-duyarlı `1/price` formu motor tarafından otomatik uygulanır (v1.x).
- **Unrealized**: `position.unrealized_pnl(price)` — bid/ask/mid/last/mark, herhangi bir referans fiyat verilebilir; `FLAT` pozisyonda her zaman `Money(0, settlement_currency)` döner.
- **Total**: `total_pnl(price)` = realized + unrealized.
- PnL settlement currency'de hesaplanır (FX'te tipik olarak quote, inverse kontratlarda base). Çoklu para birimi toplaması Position sınıfının dışında (Portfolio/kullanıcı) döviz çevrimi ister.
- Komisyonlar realized PnL'e yalnızca settlement currency'de olduklarında dahil edilir; diğerleri ayrıca izlenir.

## Position snapshotting (yalnız NETTING)
NETTING'de kapanan pozisyon aynı instrument için yeni fill geldiğinde resetlenir; motor resetten önce kapanmış durumu (nihai miktar/fiyatlar, realized PnL, tüm fill event'leri, komisyon toplamları) snapshot olarak [[cache]]'e yazar. [[portfolio|Portfolio]] toplam PnL'i snapshot'lar üzerinden toplar — snapshot'sız yalnız son döngünün PnL'i görünürdü. Rapor üretiminde snapshot'ların dahil edilmesi bu yüzden zorunludur; bkz. [[reports]]. Bu mekanizma, telemetri amaçlı periyodik `snapshot_positions` ayarından farklıdır (v1.x).

## Pozisyon düzeltmeleri (PositionAdjusted)
Normal fill dışı miktar/PnL değişimleri `PositionAdjusted` [[events|event]]'leriyle izlenir (v1.x):
- **Base currency komisyonu** (spot/FX çiftleri): komisyon base currency'deyse `signed_qty`'yi doğrudan etkiler — 1.0 BTC alıp 0.001 BTC komisyon ödemek net 0.999 BTC LONG bırakır; kapanışta da envanteri etkiler.
- **Funding ödemeleri** (perpetual futures): miktarı değiştirmez (`quantity_change=None`), PnL etkisi kaydedilir.
- `position.adjustments` tüm düzeltme geçmişini döner; `position.events` ve `position.trade_ids` broker ekstresiyle eşleştirme ve [[venue_reconciliation|reconciliation]] için kullanılır.

## Bilinen boşluklar
- PnL/ortalama fiyat aritmetiğinin `f64` hassasiyet analizi (test senaryoları, ~1e-15 epsilon sınırları) özetin dışında bırakıldı.
- Tam property listesi (identifier/timestamp/instrument spec alanları) atlandı — upstream API referansına bakılmalı.
- Spread instrument'lar için pozisyon oluşturulmaz; quanto kontratlarda `notional_value` sınırlaması var — detaylar upstream'de.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[accounting]]
- [[events]]
- [[execution_engine]]
- [[reports]]
<!-- BACKLINKS:END -->
