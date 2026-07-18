---
title: Events
type: concept
summary: Her state değişikliğini temsil eden event hiyerarşisi (Order/Position/AccountState/Time) ve message_bus üzerinden handler'lara akışı.
status: draft
key_concepts:
  - event_driven_architecture
  - orders
  - positions
  - message_bus
  - event_sourcing
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/events.md
last_updated: 2026-07-13
---

# Events

NautilusTrader'da her state değişikliği bir event nesnesiyle temsil edilir; bu nesneler [[message_bus]] üzerinden strategy ve actor handler'larına akar. Bu, [[event_driven_architecture]] omurgasının somut yüzüdür: bileşenler birbirini çağırmaz, olay yayınlar.

## Olay kategorileri (v1.x)

| Kategori | Örnekler | Üretim noktası |
|----------|----------|----------------|
| Order    | `OrderAccepted`, `OrderFilled`, `OrderCanceled` | [[execution_engine]] (venue'dan) |
| Position | `PositionOpened`, `PositionChanged`, `PositionClosed` | ExecutionEngine (fill'lerden türetilir) |
| Account  | `AccountState` | ExecutionClient / [[portfolio]] |
| Time     | `TimeEvent` | Clock (timer ve alert) |

## Handler dispatch önceliği

Bir event stratejiye ulaştığında handler'lar sabit öncelikle, spesifikten genele çağrılır:

1. Spesifik handler (örn. `on_order_filled`, `on_position_opened`)
2. Kategori handler'ı (`on_order_event` / `on_position_event` — kategorinin tüm olaylarını alır)
3. Catch-all `on_event` (her şeyi alır)

`TimeEvent` için `set_timer` / `set_time_alert` çağrısına `callback` verilirse olay o metoda, verilmezse `on_event`'e gider.

## Order event'leri: state machine geçişleri

Her order event, [[orders]] state machine'inde bir geçişe karşılık gelir: `OrderInitialized` (yerel oluşturma) → `OrderSubmitted` → `OrderAccepted` → `OrderFilled` / `OrderCanceled` / `OrderExpired`. Ara yollar: `OrderDenied` (pre-trade reddi), `OrderEmulated` / `OrderReleased` ([[order_emulator]] yolu), `OrderTriggered`, `OrderPendingUpdate` / `OrderPendingCancel` ve reddedilen değişiklikler (`OrderModifyRejected`, `OrderCancelRejected`).

[[execution_engine|ExecutionEngine]] event'i order nesnesine uygular, [[cache|Cache]]'i günceller ve MessageBus'ta yayınlar. Tüm order event'leri ortak çekirdeği taşır: `trader_id`, `strategy_id`, `instrument_id`, `client_order_id`, `venue_order_id`, `account_id`, `reconciliation` bayrağı ([[venue_reconciliation]] sırasında üretildiyse), `event_id`, `ts_event`, `ts_init`. Tip-özel alanlar eklenir (örn. `OrderFilled`: `last_qty`, `last_px`, `trade_id`, `commission`).

## Fill'den pozisyona: nedensel zincir

[[positions|Position]] event'leri fill'lerin doğrudan sonucudur. Her `OrderFilled` için ExecutionEngine:

1. Fill'i order'a uygular, Cache'teki order state'ini günceller.
2. OMS tipine ve strateji konfigürasyonuna göre position ID'yi çözer.
3. Üç sonuçtan biri: pozisyon yoksa yeni `Position` yaratır → `PositionOpened`; pozisyon açık kalıyorsa günceller → `PositionChanged`; miktar sıfıra inerse → `PositionClosed`.
4. **Flip durumu**: fill pozisyonu ters çevirirse (long 10 iken sell 15), fill ikiye bölünür — biri eski pozisyonu kapatır (`PositionClosed`), diğeri yenisini açar (`PositionOpened`).

Changed/Closed event'leri P&L alanlarını taşır (`realized_pnl`, `unrealized_pnl`, `avg_px_close`, `peak_qty`); `PositionClosed` ek olarak `closing_order_id`, `duration_ns` ve `ts_closed` içerir. Cache, order ↔ position gezinmesini iki yönlü sağlar:

```python
# Pozisyona fill katan tüm order'lar
orders = self.cache.orders_for_position(position.id)

# Order'dan ait olduğu pozisyona
position = self.cache.position_for_order(order.client_order_id)
opening_order_id = position.opening_order_id
```

## Account event'leri

`AccountState`, bakiye ve margin snapshot'ıdır ([[accounting]]). İki tetikleyici vardır: venue'nun execution client üzerinden bildirdiği hesap güncellemesi, veya margin hesaplarında `calculate_account_state` açıkken [[portfolio|Portfolio]]'nun pozisyon güncellemesi sonrası yeniden hesaplaması. Portfolio bu event'lere içeriden abone olarak exposure ve bakiye takibini sürdürür.

## Aktör abonelikleri

[[strategy_and_actor|Actor]]'lar trade etmedikleri enstrümanların execution akışını da izleyebilir: `subscribe_order_fills()` → `on_order_filled()`, `subscribe_order_cancels()` → `on_order_canceled()`. Bu abonelikler MessageBus'ı doğrudan kullanır; [[data_engine|DataEngine]] devreye girmez. Fill kalitesi veya cancel oranı izleyen monitoring actor'ları için uygundur.

Üretilen order/position/account event'leri aynı zamanda [[event_sourcing]] kapsamında event store tarafından yakalanır — durum daha sonra bu kayıtlardan yeniden inşa edilebilir.

## Bilinen boşluklar

- Kısmi fill ve triggered order'ların ek state geçişleri (upstream'deki tam "order state flow" tablosu) buraya alınmadı.
- Event tipi başına tam alan listeleri API referansında; burada yalnızca ortak çekirdek ve birkaç örnek verildi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[event_driven_architecture]]
- [[event_sourcing]]
- [[orders]]
- [[positions]]
<!-- BACKLINKS:END -->
