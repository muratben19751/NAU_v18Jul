---
title: Bar Aggregation Modları ve BarType Söz Dizimi
type: concept
status: draft
summary: BarType DSL'i (InstrumentId + step-aggregation-price + INTERNAL/EXTERNAL), time/threshold/information aggregation modları ve time-bar davranış ayarları.
key_concepts:
  - data_engine
  - data_wranglers
  - dst
  - event_driven_architecture
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/data.md
related:
  - wiki/tutorials/tutorial_fx_mean_reversion_ax.md
  - wiki/tutorials/tutorial_hurst_vpin_kraken.md
  - wiki/synthesis/index_backtest_via_equity_proxy.md
last_updated: 2026-07-13
---

# Bar Aggregation Modları ve BarType Söz Dizimi

`BarType`, bir bar akışını benzersiz kılan üç bileşeni birleştirir: `InstrumentId`, `BarSpecification` (`step`, `aggregation`, `price_type`) ve `AggregationSource`. String DSL'i (v1.x):

```
{instrument_id}-{step}-{aggregation}-{price_type}-{INTERNAL | EXTERNAL}
```

Örnek: `AAPL.XNAS-5-MINUTE-LAST-INTERNAL` — Nasdaq'taki AAPL trade'lerinden Nautilus'un yerel olarak topladığı 5 dakikalık barlar.

## Aggregation Modları

Modlar üç kategoriye ayrılır (v1.230.0 tablosu):

- **Time** — `MILLISECOND`, `SECOND`, `MINUTE`, `HOUR`, `DAY`, `WEEK`, `MONTH`, `YEAR`; `step` süre birimidir, bar emisyonunu timer sürer (en verimli mod).
- **Threshold** — `TICK`, `VOLUME`, `VALUE` ("dollar bars"), imbalance varyantları (`TICK/VOLUME/VALUE_IMBALANCE`) ve `RENKO` (tick cinsinden sabit brick); eşik dolunca bar kapanır. Büyük bir trade kalan eşiği aşarsa volume/value barları arasında bölünebilir.
- **Information** — `TICK_RUNS`, `VOLUME_RUNS`, `VALUE_RUNS`; aynı aggressor tarafın **ardışık** aktivitesi eşiğe ulaşınca kapanır, taraf değişince sayaç sıfırlanır. Imbalance barları ise **net** alış/satış dengesizliğiyle kapanır — dengeli piyasada yavaş, yönlü harekette hızlı oluşurlar.

Imbalance/runs barları `aggressor_side` alanına muhtaç olduğundan yalnızca `TradeTick`'ten üretilebilir. Time-bar `step` değerleri üst birimi tam bölmeli ve ona eşit olmamalıdır: `MILLISECOND` 1000'i, `SECOND`/`MINUTE` 60'ı, `HOUR` 24'ü, `MONTH` 12'yi böler (`60-MINUTE` değil `1-HOUR`); `DAY`/`WEEK`/`YEAR` ve threshold/information/RENKO bu kurala tabi değildir.

## Price Type: Kaynak Veri Seçimi

`@` içermeyen bir BarType'ta kaynak veri tipini `price_type` belirler:

- `LAST` → `TradeTick` akışından (trade-to-bar).
- `BID` / `ASK` / `MID` → `QuoteTick` akışından (quote-to-bar; spread analizi için).

## Aggregation Kaynağı: INTERNAL vs EXTERNAL

- `INTERNAL` — bar, yerel Nautilus sistem sınırı içinde [[data_engine]] aggregator'ları tarafından kurulur.
- `EXTERNAL` — bar, sistem sınırının dışında (venue ya da veri sağlayıcı) hesaplanmıştır; tarihsel EXTERNAL barlar tipik olarak [[data_wranglers|BarDataWrangler]] ile yüklenir.

## Composite Barlar (bar-to-bar)

`@` soneki kaynak bar tipini tanımlar: türetilen taraf **her zaman `INTERNAL`** olmalı, kaynak bar daha yüksek granülariteli olmalı (kaynak `INTERNAL` veya `EXTERNAL` olabilir), instrument ID kaynağa otomatik geçer:

```python
# 5 dakikalık barlar, harici 1 dakikalık barlardan yerel olarak toplanır
bar_type = BarType.from_str("AAPL.XNAS-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL")
# Zincirleme: TradeTick -> 1-MINUTE -> 5-MINUTE -> 1-HOUR
hourly = BarType.from_str("6EH4.XCME-1-HOUR-LAST-INTERNAL@5-MINUTE-INTERNAL")
```

## Open/Close Zaman Damgalama

Time-bar davranışı `DataEngineConfig` ile ayarlanır (v1.x):

- `time_bars_timestamp_on_close` (varsayılan `True`) — `True` iken `ts_event` bar **kapanış** zamanıdır; `False` iken **açılış** zamanı.
- `time_bars_interval_type` — `"left-open"` (varsayılan: başlangıç hariç, bitiş dahil) veya `"right-open"` (başlangıç dahil, bitiş hariç).
- `time_bars_skip_first_non_full_bar` — aggregation aralık ortasında başladığında ilk kısmi barı atlar.
- `time_bars_build_with_no_updates` (varsayılan `True`) — güncelleme gelmeyen aralıkta da bar üretir.
- `time_bars_origin_offset` — bar hizalamasını kaydırır (ör. 09:30 seans açılışına).
- `time_bars_build_delay` — bar sınırındaki verinin timer tetiklenmeden önce işlenmesi için mikrosaniye cinsinden gecikme (backtest'te faydalı).

Bar timer'ları gibi zamanlamaya duyarlı davranışların seed-kontrollü, bitwise tekrarlanabilir testleri için bkz. [[dst]].

## Request / Subscribe Sırası

`request_bars()` tarihsel barları `on_historical_data()`'ya, `subscribe_bars()` canlı barları `on_bar()`'a taşır; `request_aggregated_bars()` bağımlılık sıralı bir listeyle iç barları anında kurar. Indicator'lar veri istenmeden **önce** register edilmelidir; `validate_data_sequence=True` olan canlı adaptörlerde subscribe'ı `request_bars(callback=...)` içinden başlatmak warm-up barlarının düşmesini önleyen resmi kalıptır.

## Bilinen boşluklar

- Value tabanlı aggregator'lar (value/imbalance/runs) v1.230'da hâlâ `f64` ile hesaplıyor; fixed-point migrasyonunun tamamlanması ve v2.x'te config adlarının birebir korunup korunmadığı izlenmeli.
- RENKO brick üretiminin (tek büyük hareketten çoklu bar) DSL step birimi ayrıntıları bu sayfada özetlenmedi.
- Gelecek sürümde saat/takvim sınırına hizalanmayan keyfi bar periyotları için validation override sözü var; henüz yayınlanmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtesting_guide]]
- [[continuous_futures]]
- [[data_wranglers]]
- [[dst]]
- [[order_book]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
