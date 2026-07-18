---
title: BitMEX Ters Perpetual'da Grid Piyasa Yapıcılık ve Deadman's Switch
type: tutorial
sources:
  - https://nautilustrader.io/docs/latest/tutorials/grid_market_maker_bitmex
last_updated: 2026-07-06
summary: BitMEX ters perpetual grid piyasa yapıcı örneği; deadman's switch entegrasyonu, PerpetualContract inverse muhasebesi ve reconciliation ile canlı-backtest paritesi.
key_concepts:
  - adapters
  - execution_engine
  - risk_engine
  - tutorial_grid_market_maker_dydx
  - environment_contexts
---

Bu eğitim, BitMEX XBTUSD **ters (inverse) perpetual** sözleşmesi üzerinde bir grid piyasa yapıcı stratejisinin hem geçmiş veriyle backtest'ini hem canlı dağıtımını kapsar. Vurgu, BitMEX'in bağlantı kopmalarına karşı sunduğu sunucu-tarafı "deadman's switch" mekanizmasının Nautilus tarafından nasıl entegre edildiğidir.

Öne çıkan sınıflar: Python tarafında `BacktestEngine`/`BacktestEngineConfig`, `GridMarketMaker` stratejisi ve `GridMarketMakerConfig`; enstrüman için `PerpetualContract` (`is_inverse=True` bayrağıyla); veri yükleme için `TardisCSVDataLoader`. Canlı tarafta Rust `LiveNode` çalışma zamanı üzerinde `BitmexDataClientFactory` ve `BitmexExecutionClientFactory` kaydedilir; ayarlar `BitmexExecClientConfig` içinde toplanır ve `BitmexEnvironment` testnet/mainnet geçişini kontrol eder.

Ters perpetual muhasebesi standart perpetual'dan farklıdır — bu Nautilus'ta enstrümanın kendisinde ifade edilir:

```python
XBTUSD = PerpetualContract(
    is_inverse=True,              # fiyat USD, teminat BTC
    settlement_currency=BTC,
    multiplier=Quantity.from_int(1),  # sözleşme başı 1 USD nominal
)
```

Deadman's switch yapılandırması Rust tarafında birkaç satırla yapılır ve arka planda otomatik yenilenir:

```rust
BitmexExecClientConfig {
    deadmans_switch_timeout_secs: Some(60),
    submitter_pool_size: Some(2),
    ..Default::default()
}
```

Yenileme aralığı otomatik olarak `timeout / 4` (60s için 15s) alınır; ağ kopması bu pencereyi aşarsa BitMEX tüm açık emirleri sunucu-tarafı iptal eder. `submitter_pool_size` ise emir gönderim yolunu paralelleştirerek darboğazı azaltır.

Kalıcılık için `.with_reconciliation(true)` ve `.with_reconciliation_lookback_mins(2880)` (48 saat) kullanılır: yeniden başlatmada mevcut emir/pozisyon durumu venue'dan yeniden okunur.

Tasarım ödünleşimleri: (1) BitMEX GTC emir kullanır (dYdX'in blok-tabanlı süre dolumundan farklı olarak), bu yüzden mid hareket ettiğinde açık requote gerekir — API çağrısı ile taze fiyat arasında denge kurulur. (2) 60 saniyelik switch penceresi stranded emir riskini sınırlar ancak trafiği artırır; risk toleransına göre ayarlanmalıdır. (3) Ters muhasebede spread 42.000 USD üzerinde %1 olsa bile fill başına yalnızca ~1/42000 BTC kazanılır; `skew_factor` ile envanter yönetimi kritik hale gelir.

**İlgili sayfalar:**
- [[tutorial_grid_market_maker_dydx]]
- [[execution_engine]]
- [[risk_engine]]
- [[adapters]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_grid_market_maker_dydx]]
<!-- BACKLINKS:END -->
