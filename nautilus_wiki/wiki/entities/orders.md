---
title: Orders
type: entity
summary: Tüm emir tipleri için birleşik API — OrderFactory, time-in-force seçenekleri, contingency (OTO/OCO/OUO), tetikleyiciler ve emir yaşam döngüsü durumları (v1.x).
status: draft
key_concepts:
  - order_flow_pipeline
  - order_emulator
  - execution_engine
  - value_types
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/orders/index.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/orders/advanced.md
last_updated: 2026-07-13
---

# Orders

NautilusTrader birçok emir tipi ve yürütme talimatı için tek bir birleşik API sunar; hedef venue bir seçeneği desteklemiyorsa emir gönderilmez, açıklayıcı bir hata loglanır (v1.x). Emirler her Strategy'ye otomatik bağlı gelen `OrderFactory` ile üretilir — factory, trader/strategy ID ataması ile init ID ve timestamp üretimini üstlenir. Fiyat ve miktarlar `Price`/`Quantity` gibi [[value_types]] ile ifade edilir.

## Emir Tipleri

İki temel tipten (MARKET, LIMIT) türetilmiş dokuz tip vardır:

| Tip | Semantik |
| --- | --- |
| `MARKET` | Mevcut en iyi fiyattan hemen işlem (agresif) |
| `LIMIT` | Book'ta bekler; yalnızca limit fiyattan veya daha iyisinden işlem (pasif) |
| `STOP_MARKET` | Trigger fiyata değince Market emri koyar |
| `STOP_LIMIT` | Trigger fiyata değince belirlenen fiyattan Limit emri koyar |
| `MARKET_TO_LIMIT` | Market olarak girer; kalan miktar fill fiyatından Limit olarak bekler |
| `MARKET_IF_TOUCHED` | Trigger fiyata dokunulunca Market emri koyar |
| `LIMIT_IF_TOUCHED` | Trigger fiyata dokunulunca Limit emri koyar |
| `TRAILING_STOP_MARKET` | Trigger'ı offset ile takip eder, tetiklenince Market |
| `TRAILING_STOP_LIMIT` | Trigger'ı offset ile takip eder, tetiklenince Limit |

Venue'nun native desteklemediği tipler [[order_emulator]] üzerinden yerelde emüle edilebilir; gerçek yürütmede venue'ya yalnızca MARKET ve LIMIT emirleri gider.

## Time-in-Force

`GTC` (iptale kadar aktif), `GTD` (`expire_time` ile verilen tarihe kadar), `IOC` (hemen yürür, dolmayan kısım iptal), `FOK` (tamamı hemen ya da hiç), `DAY` (seans sonuna kadar), `AT_THE_OPEN` (yalnızca seans açılışında), `AT_THE_CLOSE` (yalnızca seans kapanışında).

## Yürütme Talimatları ve Trigger Tipleri

- `post_only`: emir yalnızca likidite sağlar; [[order_book]]'ta agresör olarak işlem başlatmaz.
- `reduce_only`: yalnızca mevcut pozisyonu küçültür; flat'ken yeni pozisyon açmaz.
- `display_qty`: Limit emrin book'ta görünen kısmını sınırlar (iceberg).

Koşullu emirlerde trigger fiyat kaynağı `TriggerType` ile seçilir: `DEFAULT` (venue varsayılanı, tipik olarak LAST_PRICE veya BID_ASK), `LAST_PRICE`, `BID_ASK`, `DOUBLE_LAST`, `DOUBLE_BID_ASK`, `LAST_OR_BID_ASK`, `MID_POINT`, `MARK_PRICE`, `INDEX_PRICE`. Trailing stop offset'i `PRICE`, `BASIS_POINTS`, `TICKS` veya `PRICE_TIER` cinsinden verilir.

## Contingency: OTO / OCO / OUO

- **OTO** (one-triggers-other): parent yürüyünce child emir(ler) devreye girer. İki model vardır: full-trigger (child yalnızca parent tamamen dolunca salınır — Binance spot, retail brokerlar) ve partial-trigger (her kısmi fill'e pro-rata — Interactive Brokers, Kraken Pro) (v1.x).
- **OCO** (one-cancels-other): bağlı emirlerden birinin (kısmi dahi olsa) yürümesi diğerlerini best-effort iptal ettirir; emirler aynı anda canlıdır.
- **OUO** (one-updates-other): birindeki her kısmi yürütme, diğerinin açık miktarını orantılı olarak düşürür.

Contingent gruplar ortak `order_list_id` paylaşır ve listedeki tüm emirler aynı venue'da olmalıdır; child'lar parent'a `parent_order_id` ile bağlanır, parent iptali child'ları da otomatik iptal eder.

## Bracket Emirler

`OrderFactory.bracket()` bir entry (varsayılan MARKET) ile take-profit LIMIT ve stop-loss STOP_MARKET child'larını tek listede kurar; TP/SL bacakları `reduce_only` olup birbirine OUO ile bağlıdır:

```python
bracket: OrderList = self.order_factory.bracket(
    instrument_id=InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
    order_side=OrderSide.BUY,
    quantity=Quantity.from_int(10),
    tp_price=Price.from_str("3300.00"),
    sl_trigger_price=Price.from_str("2800.00"),
)
```

## Emir Yaşam Döngüsü

[[order_flow_pipeline]] boyunca emir şu durumlardan geçer (v1.x):

- **Yerel (non-terminal)**: `INITIALIZED` → `EMULATED` (emulation trigger tanımlıysa) → `RELEASED`.
- **In-flight**: `SUBMITTED`, `PENDING_UPDATE`, `PENDING_CANCEL`.
- **Venue'da açık**: `ACCEPTED`, `TRIGGERED` (stop tetiklendi), `PARTIALLY_FILLED`.
- **Terminal**: `DENIED` (Nautilus reddi — geçersizlik ya da risk limiti), `REJECTED` (venue reddi), `CANCELED`, `EXPIRED`, `FILLED`.

Fill işleme ve state geçişleri [[execution_engine]] tarafından yürütülür; contingent emirlerde `OrderDenied`/`OrderRejected` [[events|event'lerini]] ele almak kritiktir — kısmi başarısızlık pozisyonu korumasız bırakabilir.

## Bilinen boşluklar

- FIX OrdType eşleme tablosu ve venue-bazlı contingency destek matrisi (Binance/Bybit/OKX/IB/dYdX ayrıntıları) bilinçli olarak sayfaya alınmadı.
- Mixed-instrument order list'lerin temsilî `instrument_id` üzerinden yürüyen risk/cache davranışı ve adapter farklılıkları ayrıntılandırılmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[events]]
- [[execution_engine]]
- [[order_book]]
- [[order_emulator]]
- [[value_types]]
<!-- BACKLINKS:END -->
