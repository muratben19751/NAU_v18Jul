---
title: OrderBook
type: entity
summary: Rust order book — L1-L3 tam book state'i, OrderBook + OwnOrderBook ayrımı, delta/depth/snapshot akışları ve kullanılabilir likidite görünümü.
status: draft
key_concepts:
  - data_engine
  - orders
  - bar_aggregation_and_type_syntax
  - rust_python_hybrid
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/order_book.md
last_updated: 2026-07-13
---

# OrderBook

Rust'ta implemente edilmiş yüksek performanslı order book; L1'den L3'e kadar tam book state'i tutar. İki ana tip vardır: `OrderBook` halka açık piyasa derinliğini izler, `OwnOrderBook` ise stratejinin kendi emirlerini ayrı tutar — ikisi birlikte "gerçek kullanılabilir likidite" görünümü üretir. Tipler Python'a PyO3 binding'leriyle açılır (`nautilus_pyo3.OrderBook`); `cache.order_book()` v1.x'te legacy Cython `OrderBook` döndürür — arayüz benzer ama birebir aynı değildir.

## Book tipleri

Her instrument için hem backtest hem live'da bir book tutulur:

- `L3_MBO` — market by order: her price level'daki her emri order ID ile izler.
- `L2_MBP` — market by price: emirleri price level bazında toplar (fiyat başına tek girdi).
- `L1_MBP` — top-of-book (BBO): yalnızca en iyi bid/ask fiyatları.

`QuoteTick`, `TradeTick` ve `Bar` gibi top-of-book veriler de `L1_MBP` book besleyebilir; bar tiplerinin sözdizimi için bkz. [[bar_aggregation_and_type_syntax]].

## Abonelik ve veri akışı

Strategy/Actor katmanı book verisine üç yolla abone olur; akış [[data_engine]] üzerinden ilgili handler'a ulaşır:

```python
self.subscribe_order_book_deltas(instrument_id)                          # L3/L2 artımlı delta
self.subscribe_order_book_depth(instrument_id)                           # 10 seviyeye kadar depth
self.subscribe_order_book_at_interval(instrument_id, interval_ms=1000)   # zamanlanmış tam snapshot

def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None: ...
def on_order_book_depth(self, depth: OrderBookDepth10) -> None: ...
def on_order_book(self, order_book: OrderBook) -> None: ...
```

## Erişim ve analiz metotları

Top-of-book erişimciler: `best_bid_price()`, `best_ask_price()`, `spread()`, `midpoint()`. Analiz/simülasyon metotları: `get_avg_px_for_quantity` (verilen miktar için ortalama fill fiyatı), `get_avg_px_qty_for_exposure` (hedef notional için fiyat/miktar), `get_quantity_for_price` (fiyata kadar kümülatif miktar), `get_quantity_at_level` (tek seviyedeki miktar), `simulate_fills` (emri mevcut book'a karşı simüle eder), `get_all_crossed_levels`. `pprint(depth, group_size)` book'u insan-okur tablo olarak basar; `group_size` ince tick'li enstrümanlarda seviyeleri daha kaba gruplara toplar.

## Bütünlük kontrolleri

`book_check_integrity` book state'inin tipiyle tutarlı olduğunu doğrular ve delta uygulanırken içeride çalışır:

- `L1_MBP`: taraf başına en fazla bir seviye.
- `L2_MBP`: price level başına en fazla bir emir.
- `L3_MBO`: yapısal kısıt yok.
- Tüm tipler: best bid, best ask'i aşamaz (crossed book hatadır; locked market, bid == ask, geçerlidir).

Gelen delta'nın instrument ID'si book'unkiyle eşleşmezse `BookIntegrityError::InstrumentMismatch` döner.

## OwnOrderBook

Kendi çalışan [[orders|emirlerini]] public book'tan ayrı izler; market maker'lar her seviyedeki likiditeyi kendi emirlerini düşerek tahmin eder. [[execution_engine|Execution engine]] `manage_own_order_books` açıkken own book tutar; [[cache]] emir event'leri geldikçe kaydı günceller. Fiyatı olan ve `IOC`/`FOK` kullanmayan emirler izlenir; terminal event'ler yine de mevcut kaydı temizleyebilir.

- **Yaşam döngüsü**: emir submit veya reconciliation ile eklenir, state değişimleriyle (accepted, partially filled, pending cancel...) güncellenir, kapanınca çıkarılır. Her `OwnBookOrder` client/venue order ID, side/price/size, status ve `ts_submitted`/`ts_accepted`/`ts_last` zaman damgalarını taşır.
- **Audit**: `audit_open_orders` own book'u geçerli client order ID kümesine karşı uzlaştırır; kümede olmayan kayıtlar silinip audit hatası loglanır. Küme açık + in-flight emirlerden kurulduğu için normal venue gecikmesinde submit edilmiş emirler silinmez; live sistemler auditi periyodik çalıştırabilir.
- **Filtreli görünümler**: `filtered_view` kendi miktarları düşülmüş yeni bir `OrderBook` döndürür — `spread`, `midpoint`, `get_avg_px_for_quantity` gibi tüm analiz metotları net book üzerinde kullanılabilir. Status filtresi (örn. yalnızca `ACCEPTED`) ve `accepted_buffer_ns` grace period'u (public feed'e henüz yansımamış taze accepted emirleri hariç tutar) desteklenir.

## Binary piyasalar

Prediction market'larda (örn. Polymarket) YES/NO fiyatları 1.0'a tamamlanır: NO tarafında 0.40'a bid, YES tarafında 0.60'a ask ile ekonomik olarak eşdeğerdir. `OwnOrderBook::combined_with_opposite` NO tarafındaki emirleri parite dönüşümüyle (fiyat → 1 − fiyat, bid ↔ ask) YES book'una birleştirir; birleşik own book ile public YES book filtrelenerek iki taraftaki kendi likiditenin bütünsel resmi elde edilir.

## Bilinen boşluklar

- Upstream sayfa ağırlıkla Rust API'sini belgeler; legacy Cython `OrderBook` ile PyO3 arayüzü arasındaki fark listesi API referansına bırakılmıştır (v1.x).
- `OrderBookDelta`/`OrderBookDeltas` tiplerinin alan düzeyi şeması ve delta action türleri (ADD/UPDATE/DELETE/CLEAR) bu upstream sayfada yer almaz.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[data_engine]]
- [[orders]]
<!-- BACKLINKS:END -->
