---
source: https://nautilustrader.io/docs/latest/concepts/strategies
retrieved: 2026-07-05
type: docs_snapshot
immutable: true
---

# Strategies (Docs) Snapshot

## Actor ve Strategy
**Actor**: Veri alan, event handle eden, state yöneten temel bileşen.
**Strategy**: Actor'ü extend eder, order management ekler. Tüm Actor işlevselliğini + trading-specific özellikler taşır.

## Strateji Türleri
- Directional trading
- Momentum
- Rebalancing
- Pairs trading
- Market making

## Emir Tipleri
- LIMIT: Belirtilen fiyattan
- MARKET: Anlık piyasa fiyatı
- GTD: Good-till-Date, expiry yönetimli

Emirler tekil veya toplu (batch) gönderilebilir; emulation ve modification destekli.

## Execution Algorithms
Stratejiler `ExecAlgorithmId` ile execution algorithm'e routing yapabilir. Örnek: **TWAP** (Time-Weighted Average Price).

Emir akışı (konfigürasyona göre):
1. Order Emulator (emulation trigger varsa)
2. Execution Algorithm (belirtilmişse)
3. Risk Engine (varsayılan)

## Strateji Erişimleri
- **Clock**: Timestamp ve timer
- **Cache**: Piyasa verisi ve execution nesneleri
- **Portfolio**: Hesap bilgisi ve P&L
