---
title: Opsiyon Greeks Boru Hattı
type: concept
status: draft
summary: Opsiyon Greeks için iki yol — venue kaynaklı OptionGreeks akışı (Deribit/Bybit/OKX) ve cache'ten Black-Scholes hesaplayan GreeksCalculator; zincir abonelikleri.
key_concepts:
  - data_engine
  - cache
  - portfolio
  - strategy_and_actor
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/greeks.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/options.md
related:
  - wiki/tutorials/tutorial_options_data_bybit.md
  - wiki/tutorials/tutorial_delta_neutral_derive.md
  - wiki/tutorials/tutorial_delta_neutral_bybit.md
last_updated: 2026-07-13
---

# Opsiyon Greeks Boru Hattı

NautilusTrader opsiyon Greeks'leri için iki bağımsız yol sunar: venue kaynaklı `OptionGreeks` akışı (Rust/PyO3) ve cache'teki piyasa verisinden Black-Scholes hesaplayan yerel `GreeksCalculator`. İki yol ayrı ya da birlikte çalışır; venue akışı Deribit/Bybit/OKX gibi Greeks yayınlayan borsaları kapsar, yerel hesaplayıcı ise Greeks yayınlamayan venue'ları, backtest'i ve şok senaryolarını.

## Venue Kaynaklı Akış ve Zincir Toplama

`OptionGreeks` tek bir opsiyon sözleşmesinin delta/gamma/vega/theta/rho, mark/bid/ask IV, `underlying_price` ve open interest alanlarını taşır; `Data` enum'unun yerli üyesi olduğundan kataloğa yazılır ve backtest'te built-in market data olarak replay edilir. Aktör/stratejiden `subscribe_option_greeks(instrument_id, client_id)` ile abone olunur, güncellemeler `on_option_greeks` işleyicisine düşer.

Zincir düzeyinde [[data_engine]] abone olunan her `OptionSeriesId` için bir Rust `OptionChainManager` yaratır ve yaşam döngüsünün sahibidir: manager, quote ile Greeks'leri keep-latest semantiğiyle biriktiren `OptionChainAggregator`'ı ve ATM'yi türeten `AtmTracker`'ı sarar. Zamanlayıcı tetiklerinde (`snapshot_interval_ms`) ya da raw modda (`None`) her güncellemede tek bir immutable `OptionChainSlice` üretilir, [[message_bus]] üzerinden yayınlanır ve [[strategy_and_actor]] tarafında `on_option_chain` ile tüketilir. Quote'tan önce gelen Greeks `pending_greeks` tamponunda bekletilip ilk quote'a iliştirilir.

## StrikeRange ve Bootstrap

Aktif strike kümesi dört varyantla filtrelenir (v1.x): `Fixed` (açık strike listesi), `AtmRelative(n_üst, n_alt)` (ATM'nin üstünde/altında N strike), `AtmPercent(bant)` (ATM çevresinde yüzde bandı) ve `Delta(hedef, tolerans)` (delta büyüklüğü hedefe yakın strike'lar). ATM tabanlı varyantlar forward fiyat bilinene dek ertelenir; ATM, venue Greeks'lerindeki `underlying_price` alanından reaktif türetilir ya da HTTP forward fiyat yanıtıyla anında tohumlanır. ATM kaydıkça küme otomatik yeniden dengelenir — histerezis eşiği ve cooldown, strike sınırlarında titreşimi önler. `Delta` varyantı banda uyan Greeks yoksa ATM±5 strike'lık pencereye geri düşer.

## GreeksCalculator Akışı

Yerel hesaplayıcı [[cache]] ve clock ile kurulur ve beş adımda çalışır: enstrümanı ve dayanağını cache'ten bulur, güncel fiyatları alır (MID tercih, LAST yedek), yield curve'ü cache'ten arar (yoksa `flat_interest_rate`), piyasa fiyatından IV'yi `imply_vol_and_greeks` ile çıkarır ve `GreeksData` döndürür. Fiyat eksikse `None` döner — ısınma dönemi stratejide normal no-op sayılır. Cython sınıf `nautilus_trader.model.greeks` altındadır (v1.x); v2.x runtime yüzeyi aynı hesaplayıcıyı `nautilus_trader.common`'dan PyO3 olarak sunar.

```python
from nautilus_trader.model.greeks import GreeksCalculator  # v1.x Cython

calculator = GreeksCalculator(cache=self.cache, clock=self.clock)
greeks = calculator.instrument_greeks(
    instrument_id=option_id,
    flat_interest_rate=0.0425,
    spot_shock=10.0,   # şok senaryosu: dayanak +10 puan
    vol_shock=0.02,    # +2 puan mutlak vol artışı
)
```

`portfolio_greeks()` açık pozisyonları filtreleyip (`underlyings`, `venue`, `strategy_id`, `side`, `greeks_filter`) `PortfolioGreeks` (pnl, price, delta, gamma, vega, theta) toplar; [[portfolio]] düzeyindeki opsiyon riski buradan okunur. Beta ağırlıklandırma (`index_instrument_id` + `beta_weights`), yüzde Greeks ve `vega_time_weight_base` ile vade-normalize vega desteklenir. `GreeksData` bir `@customdataclass` olduğundan Arrow ile serileşir, cache'te ve katalogda saklanır; `to_portfolio_greeks()` çarpanla ölçekler, `signed_qty * greeks` pozisyon miktarını uygular.

## Backtest ve Kalıcılık

Zincir backtest'leri canlıyla aynı `OptionChainManager`/`OptionChainAggregator` yolunu kullanır; önkoşul, [[parquet_data_catalog]] içinde her sözleşme için `QuoteTick` + `OptionGreeks` kayıtlarının ve enstrüman tanımlarının hazır olmasıdır (Tardis replay'leri bu sözleşmeyi karşılar; koşu sırasında eksik veri indirilmez). [[backtest_node]] konfigürasyonunda iki veri akışı ayrı `BacktestDataConfig` girdileriyle verilir. Eşleştirme quote-driven'dır: market emirleri karşı BBO'ya taker olarak dolar, pasif limit emirleri sonraki BBO limit fiyatı içinden geçtiğinde maker olabilir. Yapısal ücretler simüle venue'da `CappedOptionFeeModel` / `TieredNotionalOptionFeeModel` ile yapılandırılır — venue adından çıkarsanmaz.

## Enstrüman Desteğinin Sınırları

Beş opsiyon enstrüman tipi vardır (bkz. [[instruments]]): `OptionContract`, `OptionSpread` (en çok 4 bacak; strike/kind bacaklardadır, spread'de değil), `CryptoOption` (inverse/quanto), `CryptoOptionSpread` (`is_inverse` + `settlement_currency`) ve `BinaryOption` (strike, option_kind ve underlying alanları yok). Sınırlar (v1.x):

- Venue Greeks aboneliği yalnız üç [[adapters|adaptörde]]: Deribit ve Bybit tekil + zincir, OKX yalnız tekil (zincir desteği yok).
- Opsiyon eşleştirmesinde L2 kuyruk pozisyonu simüle edilmez.
- Amerikan tipi opsiyonlar Greeks hesabında Avrupa tipi gibi fiyatlanır; vega 0.01, theta 1/365.25 ölçeklidir.
- `vanna`/`volga`/`charm` gibi model-özel değerler çekirdek şemada yoktur — [[custom_data]] ile taşınır; `convention` gibi yorum için zorunlu alanlar non-nullable'dır.

Venue farkları uygulamada sürer: IV bazlı emirde Bybit `orderIv`, OKX `px_vol` alanını kullanır ve `iv_param_key` bu farkı soyutlar (kaynak: [[tutorial_delta_neutral_bybit]]); Derive Greeks'i `option_pricing` yüklerinden türetir (kaynak: [[tutorial_delta_neutral_derive]]) ancak resmi adaptör destek tablosunda yer almaz.

## Bilinen boşluklar

- `GreeksConvention` numeraire varyantlarının listesi ve hangi venue'nun hangi konvansiyonu yayınladığı upstream'de detaylandırılmamış.
- `OptionSeriesId` kurucu parametrelerinin tam imzası doc'ta `OptionSeriesId(...)` olarak geçiştirilmiş.
- Cache borrowing disiplininin (`Rc<RefCell>`) Python bağlamındaki karşılığı hâlâ incelenmedi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[tutorial_delta_neutral_bybit]]
- [[tutorial_delta_neutral_derive]]
- [[tutorial_options_data_bybit]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
