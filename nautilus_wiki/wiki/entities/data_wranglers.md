---
title: Data Wrangler Ailesi
type: entity
status: draft
summary: Ham tabular veriyi (DataFrame) QuoteTick/TradeTick/Bar/OrderBookDelta nesnelerine çeviren DataLoader+Wrangler hattı; v1 Cython vs DataWrangler v2 (PyO3).
key_concepts:
  - parquet_data_catalog
  - data_engine
  - custom_data
  - precision_modes
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/data.md
related:
  - wiki/tutorials/tutorial_backtest_high_level.md
  - wiki/tutorials/tutorial_fx_mean_reversion_ax.md
  - wiki/tutorials/tutorial_loading_external_data.md
  - wiki/synthesis/v1_to_v2_migration_lessons.md
  - wiki/tutorials/tutorial_quickstart.md
last_updated: 2026-07-13
---

# Data Wrangler Ailesi

Data wrangler'lar, ham tabular veriyi (tipik olarak pandas DataFrame) Nautilus'un iç veri modeline — `QuoteTick`, `TradeTick`, `Bar`, `OrderBookDelta` — dönüştüren sınıf ailesidir. Standart boru hattı iki bileşenlidir: kaynak/formata özgü **DataLoader** ham dosyayı (CSV, DBN...) doğru şemalı bir `pd.DataFrame`'e okur; veri tipine özgü **DataWrangler** bu DataFrame'i `list[Data]` tipli Nautilus nesnelerine çevirir. Dönüşümün üç hedefi aynı süreci paylaşır: `BacktestEngine`'e doğrudan veri sağlamak, [[parquet_data_catalog]]'a `write_data(...)` ile Nautilus-Parquet formatında kalıcılaştırmak (sonradan `BacktestNode` ile kullanım) ve research ile backtest arasında veri tutarlılığı.

## v1 Wrangler'ları (v1.x)

`nautilus_trader.persistence.wranglers` modülünde, veri tipi başına bir sınıf bulunur:

- `OrderBookDeltaDataWrangler`
- `QuoteTickDataWrangler`
- `TradeTickDataWrangler`
- `BarDataWrangler`

Her wrangler ilgili enstrüman nesnesiyle kurulur; `process(df)` çıktısı Cython tabanlı legacy v1 nesneleridir:

```python
from nautilus_trader.adapters.binance.loaders import BinanceOrderBookDeltaDataLoader
from nautilus_trader.persistence.wranglers import OrderBookDeltaDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

df = BinanceOrderBookDeltaDataLoader.load(data_path)  # kaynak-özgü loader
wrangler = OrderBookDeltaDataWrangler(TestInstrumentProvider.btcusdt_binance())
deltas = wrangler.process(df)  # list[OrderBookDelta]
```

## v2 Wrangler'ları (PyO3 / Rust çekirdeği)

**DataWrangler v2** bileşenleri, tipik olarak sabit genişlikli Nautilus Arrow v2 şemasına sahip bir `pd.DataFrame` alır ve **PyO3** Nautilus nesneleri üretir; bu nesneler yalnızca geliştirilmekte olan yeni Rust çekirdeğiyle uyumludur. Kritik uyarı (v1.230.0): PyO3 nesneleri, v1 legacy Cython nesnelerinin beklendiği yerlerde — ör. doğrudan `BacktestEngine`'e ekleme — **kullanılamaz**. v2 modülü ayrıca `OrderBookDepth10DataWranglerV2` sağlar. (v2.0.0rc1'de `BarDataWrangler.process(df)` kaldırıldı; DataFrame ile çalışan projeler `Bar(...)` yapıcısını doğrudan kullanır — bkz. [[v1_to_v2_migration_lessons]].)

## Nanosaniye Timestamp Kuralları

Üretilen her nesne iki UNIX nanosaniye damgası taşır:

- `ts_event` — olayın gerçekte oluştuğu an (trade'in venue'da gerçekleşmesi; `Bar` için varsayılanda kapanış zamanı).
- `ts_init` — Nautilus'un nesneyi oluşturduğu an; "alım zamanı"ndan daha genel bir kavramdır ve alımın söz konusu olmadığı tiplerde (ör. komutlar) de bulunur.

Backtest'te veri `ts_init`'e göre **stable sort** ile sıralanır — wrangler'ın yazdığı damgalar replay sırasını doğrudan belirler. `ts_init - ts_event` farkı toplam sistem gecikmesini (ağ + işleme + kuyruk) verir; ancak damgaları üreten saatler senkronize olmayabilir ve clock skew nedeniyle `ts_init >= ts_event` garanti edilmez. Nautilus içinde üretilen veride iki damga aynı olabilir.

## Precision Etkileşimi

`Price`/`Quantity` fixed-point'tir; `from_raw()` raw değerleri `10^(FIXED_PRECISION - precision)`'ın tam katı olmalıdır (FIXED_PRECISION: standart 9, high-precision 16 — bkz. [[precision_modes]]), aksi halde panic oluşur. `int(value * FIXED_SCALAR)` gibi float hatalı üretimlerden gelen bozuk raw değerler Arrow decode yolunda otomatik olarak en yakın geçerli kata yuvarlanır (v1.230+, küçük decode maliyetiyle).

## Downstream Tüketim

Wrangler çıktısı [[data_engine]] tarafından tüketilir: engine veriyi [[cache|Cache]]'e koyar ve [[message_bus|MessageBus]] üzerinden abonelere yayınlar. `BarType` soneki `INTERNAL` olduğunda DataEngine barları gelen tick akışından kendisi toplar ([[bar_aggregation_and_type_syntax]]) ve önceden hazırlanmış bar verisine gerek kalmaz. Yerleşik tipler dışındaki kullanıcı verisi için aynı loader→wrangler örüntüsü [[custom_data]] sınıflarıyla genişletilir.

## Bilinen boşluklar

- `ts_init_delta`, `default_volume` gibi wrangler kurucu parametrelerinin resmi listesi concepts doc'unda yok; API referansı gerekli.
- Order book delta girişi için beklenen kolon şeması belgelenmemiş (loader çıktı şeması örtük).
- v2 wrangler modüllerinin tam paket yolları ve üye listesi (doc yalnızca `OrderBookDepth10DataWranglerV2`'yi anıyor); `QuoteTickDataWrangler`/`TradeTickDataWrangler`'ın v2 rc1'de `process(df)` davranışı hâlâ doğrulanmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[bar_aggregation_and_type_syntax]]
- [[custom_data]]
- [[tutorial_quickstart]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
