---
title: Index Backtest via Equity Proxy
type: synthesis
sources:
  - sources/03_strategies_docs.md
  - sources/04_backtesting_docs.md
last_updated: 2026-08-11
summary: Tradable olmayan IndexInstrument yerine Equity proxy ile endeks backtest'i kurma; size_precision=0 ve cent-altı price_precision tuzakları, CASH venue ve tick→OHLCV resample deseni.
key_concepts:
  - execution_engine
  - risk_engine
  - order_flow_pipeline
  - backtesting_guide
  - adapters
---

# Index Backtest'i Equity Proxy ile Modelleme

Nautilus'ta bir hisse/emtia/kripto **endeksi** (S&P 500, sektörel Polygon indeksleri gibi) üzerinde backtest çalıştırmak istendiğinde ilk refleks `IndexInstrument` kullanmaktır. Ancak `IndexInstrument.__doc__` açıkça belirtir: **"not directly tradable"**. `order_factory.market(...)` çağrıları RiskEngine tarafından reddedilir ve backtest hiçbir pozisyon açmadan biter.

Pratik çözüm: endeksi **`Equity` instrument** olarak modellemek — semantik olarak "endeksi birebir izleyen bir ETF veya CFD alıyormuş gibi" varsayım. Nautilus için tradable bir instrument; kullanıcı açısından PnL yine endeks kapanışına göre hesaplanır.

## Zorunlu Alanlar

```python
Equity(
    instrument_id=InstrumentId(Symbol("I:AAVE100"), Venue("POLYGON")),
    raw_symbol=Symbol("I:AAVE100"),
    currency=USD,
    price_precision=2,                  # US-equity standardı (tick 0.01)
    price_increment=Price.from_str("0.01"),
    lot_size=Quantity.from_str("1"),   # ← integer share'ler
    ts_event=0, ts_init=0,
)
```

> Not: `backtest.py:_make_index_instrument` bu spec'i **price_precision=2 / tick 0.01**
> olarak pinler (NAU QLAB equity standardı: her equity USD, pp 2, tick 0.01, lot 1) —
> harici NAU_ev kataloğundaki 591 US-equity tanımıyla birebir aynı.

`Symbol` colon (`:`) kabul eder — Polygon konvansiyonunu bozmadan taşıyabilirsin. Sadece dosya-sistemi katmanında normalize et (`I:AAVE100` → `I_AAVE100.parquet`).

## Kritik Trap: `size_precision=0`

`Equity`'nin `size_precision` alanı **0**'dır (whole shares). Strategy `trade_size` olarak `0.1` verirse Nautilus quantity'i `Quantity(0)`'a yuvarlar → order silently reddedilir → backtest sıfır trade ile döner ve `pnl=0, n_trades=0` şeklinde başarılı görünür.

**Kural**: Index yolunda `trade_size` mutlaka `>= 1` olmalı. Sunucu tarafında `< 1` gelen değerleri `1.0`'a clamp et ve rationale'e not düş; kullanıcı UI'da fark etsin.

## Kardeş Trap: `price_precision` — cent altı semboller (2026-08-11)

Aynı sınıftan ikinci bir tuzak, `size_precision=0` düzeltildikten sonra **fiyat**
tarafında yaşamaya devam ediyordu. `backtest._BYBIT_SPECS` yalnız
BTC/ETH/SOL/XRP içeriyor; listede olmayan HER sembol
`_BYBIT_DEFAULT_SPEC = (2, 3, "0.01")` alıyordu ve `Price(0.000009, 2)` → `0.00`.
PEPEUSDT / SHIBUSDT / FLOKIUSDT gibi cent altı bir sembolde tüm OHLC serisi
sıfırlanıyor, `_compute_qty` `price <= 0` dalına düşüyor ve koşum **hata
vermeden** anlamsız bir sonucu "geçerli" diye raporluyordu. Grafik tarafı bunu
zaten biliyordu (`chart_indicators.py`: `round(v,4)` SHIB/PEPE çizgisini 0.0'a
düzlüyordu) — motor tarafı bilmiyordu.

Çözüm `size_precision=0` ile **aynı duruşta**, iki katman:

1. **Türet** — `backtest.price_precision_for(price)` büyüklük sırasından
   (5 anlamlı hane, `[2, 9]` aralığına kırpılmış) hassasiyeti hesaplar;
   `price_scale_hint(df)` çerçevedeki **en küçük pozitif** fiyatı verir (ortalama
   değil: sıfıra düşecek barlar tam onlar). `_make_bybit_instrument(...,
   price_hint=...)` bunu yalnız `_BYBIT_SPECS`'te OLMAYAN semboller için
   kullanır — sabitlenmiş semboller NAU `universe.yaml` kimliğini korur.
   İpucu `sandbox._build_instrument_bar_type(recipe, bars_df)`,
   `parallel_exec` ve `data.py`'nin iki katalog yazıcısından geçer.
2. **Reddet** — `_bars_from_df` (katalog yazımı ve motor barları için TEK huni)
   pozitif bir fiyatın dönüşüm sonrası 0'a düştüğünü görürse
   `PricePrecisionError` atar. Gerçek bir fiyatı hiçliğe yuvarlamak
   kullanıcının istediği bir sonuç değildir; uyarı değil hatadır.

**Genel kural**: kullanıcının seçmediği bir hassasiyet gerçek bir sayıyı 0'a
çeviriyorsa bu sessiz kalamaz. Precision varsayılanları "makul" değil,
**enstrümanın gerçek ölçeğinden türetilmiş** olmalı; türetilemiyorsa koşum
reddedilmeli.

## Venue Konfigürasyonu

BTC yolundaki multi-currency (BTC+USD) cash başlangıç bakiyesi index için gereksiz. Non-short Index backtest'i **tek para birimi USD cash** ile daha temiz:

```python
engine.add_venue(
    venue=Venue("POLYGON"), oms_type=OmsType.NETTING,
    account_type=AccountType.CASH, base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
)
```

`spec.allow_short=True` ise MARGIN gerekir (CASH short pozisyonu materialize etmez — bkz. [[execution_engine]]).

## BarType Sözdizimi

```
{instrument_id}-{step}-LAST-EXTERNAL
```

Örnek: `I:AAVE100.POLYGON-1-DAY-LAST-EXTERNAL` (günlük), `...-1-MINUTE-...` (dakika). `LAST` = last-price aggregation, `EXTERNAL` = data source is pre-computed (not synthesized by an aggregator).

## Tick → OHLCV Pipeline

Ham Polygon tick verisi tek `value` sütunundan ibaret (bid/ask/volume yok). Pandas resample yeterli:

```python
s.resample("1D").agg(
    open="first", high="max", low="min", close="last",
    volume="count",   # tick sayısı → volume proxy
).dropna(subset=["open"])
```

Volume yerine tick-count kullanmak, tick-based likidite filtreleri için yeterince temsili — gerçek volume gerekirse ayrı bir kaynak entegre edilmeli.

## Kapsam Dışı

- **Corporate actions / splits** — Equity proxy varsayımı endeks düzeyinde bunu ihmal eder.
- **Native index calendar / holiday filter** — Endeks 7/24 tick akışı varsayılıyor; borsa saatleri filtresi kullanıcının işi.
- **Multi-instrument portfolio** — Bu desen tek-instrument backtest içindir; portfolio-of-indices için ayrı bir yaklaşım gerekir.

## İlgili Sayfalar
- [[backtesting_guide]]
- [[execution_engine]]
- [[order_flow_pipeline]]
- [[us_equity_katalog_veri_butunlugu]] — bu proxy'nin beslendiği kataloğun
  ölçülmüş veri kusurları ve ingest düzeltmeleri
- [[nau_deepr_dorduncu_tur_2026_08_11]] — kapı kalibrasyonu, katalog onarımı,
  motor hızlandırması

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[ticker_kimlik_degil_o_gunun_etiketi]]
- [[us_equity_katalog_veri_butunlugu]]
- [[v1_to_v2_migration_lessons]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
