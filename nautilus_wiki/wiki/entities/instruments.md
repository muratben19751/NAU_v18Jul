---
title: Instruments
type: entity
summary: Venue emir semantiğini birebir yansıtan enstrüman tanımları — tip hiyerarşisi, precision/increment grid'i, limitler, margin/fee ve InstrumentId sözdizimi.
status: draft
key_concepts:
  - data_engine
  - cache
  - value_types
  - precision_modes
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/instruments/index.md
last_updated: 2026-07-13
---

# Instruments

Nautilus'ta alınıp satılan ya da referans alınan her varlık, venue'nun emir semantiğini birebir yansıtan bir instrument tanımıyla temsil edilir. Tanım; kimlik, fiyat/miktar grid'i, para birimleri, limitler, margin/fee oranları ve adapter metadata'sını tek sözleşmede toplar. Yanlış tanım, fiyat/miktarların sessizce kesilmesine (truncation) ya da geçerli emirlerin venue tarafından reddine yol açar — bu yüzden tanımlar venue kurallarıyla eşleşmek zorundadır.

## Tip hiyerarşisi (v1.x, 19+ sınıf)

- **Spot / cash**: `Equity` (borsa hissesi veya ETF), `CurrencyPair` (fiat FX ya da kripto spot çifti, base/quote), `Commodity` (spot emtia), `IndexInstrument` (trade edilemeyen referans endeks), `TokenizedAsset`.
- **Futures**: `FuturesContract` (teslimatlı vadeli), `FuturesSpread` (exchange-defined çok bacaklı strateji), `CryptoFuture` (vadeli kripto), `CryptoFuturesSpread`. Vade zincirinin tek sürekli seriye dikilmesi için bkz. [[continuous_futures]].
- **Options**: `OptionContract`, `OptionSpread`, `CryptoOption`, `CryptoOptionSpread`, `BinaryOption` (0 ya da 1'e settle olur; ör. Polymarket).
- **Perpetual / CFD**: `CryptoPerpetual` (kripto perpetual futures), `PerpetualContract` (asset-class bağımsız perpetual), `Cfd` (contract for difference).
- **Diğer**: `BettingInstrument` (Betfair market seçimi) ve yalnızca yerel yaşayan, formülden türetilen `SyntheticInstrument` — ayrıntısı [[synthetics]] sayfasında.

## InstrumentId sözdizimi

Biçim `{symbol}.{venue}` — örn. `ETHUSDT-PERP.BINANCE`. Native semboller borsalar arasında çakışabilir; tekilliği sağlayan `symbol + venue` çiftidir ve bir Nautilus sisteminde eşsiz olmalıdır. `raw_symbol` alanı normalizasyon öncesi venue sembolünü korur.

## Precision ve increment alanları

`price_precision` / `size_precision` ondalık basamak sayısını, `price_increment` / `size_increment` en küçük geçerli adımı tanımlar; ikili birbiriyle tutarlı olmalıdır (precision 2 ↔ increment 0.01). Nautilus bu grid'i katı uygular çünkü venue aynı doğrulamayı yapar: [[risk_engine]] değerleri kendiliğinden yuvarlamaz, grid dışı bir Price ile gönderilen emir denied olur. Bu yüzden fiyat/miktar üretiminde instrument'ın factory metotları kullanılır; `Price`/`Quantity` temsili ve precision sınırları için bkz. [[value_types]] ve [[precision_modes]].

```python
instrument = self.cache.instrument(instrument_id)
price = instrument.make_price(0.90500)  # price_increment grid'ine oturtur
qty = instrument.make_qty(150)          # size_increment grid'ine oturtur
```

## Margin, fee ve limitler

`margin_init` / `margin_maint` ondalık oran olarak MarginAccount hesaplarına girer (taker fee ile birlikte). `maker_fee` / `taker_fee`'de pozitif değer komisyon, negatif değer rebate anlamına gelir. Opsiyonel sınırlar: `max/min_quantity`, `max/min_notional`, `max/min_price`. `multiplier` notional ve PnL çarpanı, `lot_size` yuvarlak lot büyüklüğüdür; `info` alanı venue'ya özgü ham metadata'yı JSON-uyumlu dict olarak taşır. `ts_event` / `ts_init` tanım olayının ve Nautilus'a girişin UNIX-nanosaniye damgalarıdır.

## Yükleme ve erişim

- Backtest ve testlerde `TestInstrumentProvider.default_fx_ccy("AUD/USD")` gibi hazır tanımlar kullanılır.
- Live'da her [[adapters|adapter]] bir `InstrumentProvider` sunar ve tanımları cache'ler: destekleniyorsa `InstrumentProviderConfig(load_all=True)`, bilinen setler için `load_ids`. Subscription ve emir metotları instrument'ın [[cache]]'te önceden var olmasını gerektirir.
- Strategy/Actor içinden erişim `self.cache.instrument(InstrumentId.from_str(...))` ile yapılır; `subscribe_instrument()` / `subscribe_instruments(venue)` aboneliklerinde tanım güncellemeleri [[data_engine]] üzerinden `on_instrument()` handler'ına düşer.

## Bilinen boşluklar

- Rust API yüzeyi (`nautilus_model::instruments`, `InstrumentAny`) burada özetlenmedi; Python tarafıyla aynı sözleşmeyi (kimlik, precision, limit, fee, metadata) taşır.
- `tick_scheme` (değişken tick şeması) alanının hangi venue'larda desteklendiği ve şema adları detaylandırılmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[continuous_futures]]
- [[data_engine]]
- [[option_greeks_pipeline]]
- [[synthetics]]
- [[value_types]]
<!-- BACKLINKS:END -->
