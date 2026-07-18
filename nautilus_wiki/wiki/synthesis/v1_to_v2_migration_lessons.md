---
title: v1.230.0 → v2.0.0rc1 Migration Lessons
type: synthesis
sources:
  - https://pypi.org/project/nautilus-trader/2.0.0rc1/
  - https://github.com/nautechsystems/nautilus_trader/blob/main/RELEASES.md
last_updated: 2026-07-06
summary: v1→v2rc1 port'unda modül düzleştirmesi, StrategyConfig plain-class kalıbı ve BarDataWrangler kaldırılışı gibi kırıcı değişiklikleri parite kanıtıyla belgeler.
key_concepts:
  - strategy_and_actor
  - order_flow_pipeline
  - precision_modes
  - nautilus_kernel
  - tutorial_loading_external_data
  - index_backtest_via_equity_proxy
---

# v1.230.0 → v2.0.0rc1 Migration Lessons

Bu sayfa, `nautilus_web_app` üzerinde yapılan canlı bir v1→v2rc1 port'unun kayıtlarıdır. Plan öncesi dokümanda yakalanmayan gerçek breaking-change'ler ve *çalıştı* pariteleri belgeler. v2 hâlâ RC fazında, dokümantasyonu `latest`'ta yayında değil — bu synthesis o boşluğu kapatmayı amaçlar. Kodun her modülünün wiki karşılığı için bkz. [[webapp_module_map]].

## Bağlam

- **Hedef repo**: `nautilus_web_app` (BTC/Index backtest + composer + custom-block orchestrator).
- **Migration şekli**: aynı `.venv` (Python 3.12), v1 kaldırıldı, v2 rc1 yüklendi (wheel, Rust toolchain gerekmez).
- **Parite kriteri**: 6 catalog spec üzerinde PnL 2 ondalık hane + n_trades tam eşleşme. **Hepsi geçti**, PnL/win_rate/max_dd/equity_last bit-identical.

## Modül düzleştirmesi (en yaygın sürprız)

v1'in derinlemesine iç içe modül yapısı v2'de büyük ölçüde flat hale getirildi. Bir sınıf paket kökünde yayımlanıyor; alt modül `ModuleNotFoundError` verir.

| v1 import path | v2 import path |
|---|---|
| `nautilus_trader.backtest.engine` | `nautilus_trader.backtest` |
| `nautilus_trader.config` (için `LoggingConfig`, `StrategyConfig`) | `nautilus_trader.common` (`LoggerConfig`), `nautilus_trader.trading` (`StrategyConfig`) |
| `nautilus_trader.model.data`, `.enums`, `.identifiers`, `.instruments`, `.objects`, `.currencies` | Hepsi düz `nautilus_trader.model` altında |
| `nautilus_trader.indicators.averages`, `.volatility` | Düz `nautilus_trader.indicators` |
| `nautilus_trader.persistence.wranglers` | `nautilus_trader.persistence` |

Kural: bir sembol import edilemiyorsa, önce paket kökünü dene (`from nautilus_trader.X import Y`); alt modül muhtemelen kaldırılmıştır. [[nautilus_kernel]] gibi çekirdek sınıflar da bu düzleştirmeden geçti.

## Kaldırılmış / imzası değişmiş API'ler

### `BTC`/`USD` currency sabitleri gitti

- v1: `from nautilus_trader.model.currencies import BTC, USD` (hazır singleton).
- v2: `Currency.from_str("BTC")`, `Currency.from_str("USD")`.

**Neden önemli**: bu sabitler webapp'de her yerdeydi (venue starting balances, `Money(..., USD)`). Migration'da modül seviyesi `BTC = Currency.from_str("BTC")` tanımlamak en temiz çözüm — nokta imzasını tüm çağrı sitelerinde değiştirmemek için.

### `StrategyConfig` artık msgspec Struct **değil**

- v1: `msgspec.Struct` tabanlı; `class MyCfg(StrategyConfig, frozen=True): field: type = default` yeterliydi.
- v2: `builtins.object` tabanlı plain class. `frozen=True` metaclass kwargs kabul etmiyor; sınıf-seviye type annotation'ları yok sayılıyor.
- **Düzeltme**: `__init__` yaz, `super().__init__(**kwargs)` çağır, kendi field'larını `self.foo = foo` ile ata:

```python
class MyCfg(StrategyConfig):
    def __init__(self, instrument_id, bar_type, fast=10, slow=30, **kwargs):
        super().__init__(**kwargs)
        self.instrument_id = instrument_id
        self.bar_type = bar_type
        self.fast = fast
        self.slow = slow
```

**Neden önemli**: bu Strategy Config kalıbı [[strategy_and_actor]] içindeki "config bir Struct'tir" iddiasını v2 için geçersiz kılar. Struct'un verdiği immutability + tip validasyonu artık user code sorumluluğunda.

### `BarDataWrangler.process(df)` **tamamen kaldırıldı**

v1'de bir `pandas.DataFrame` alıp `list[Bar]` döndüren en popüler ingest yolu. v2'de:

- `BarDataWrangler.__init__(bar_type: str, price_precision, size_precision)` — imza değişti (instrument yok).
- Tek method: `process_record_batch_bytes(data: bytes) -> list[Bar]`. **Arrow record batch bytes** bekliyor, DataFrame değil.

**Düzeltme**: Pandas pipeline'ı olan projelerin en pratik ports'u `Bar()` doğrudan kurmak:

```python
def _bars_from_df(bar_type, instrument, df):
    pp, sp = instrument.price_precision, instrument.size_precision
    ts_ns = df.index.astype("int64").to_numpy()  # UTC DatetimeIndex → ns
    return [
        Bar(bar_type,
            Price(o, pp), Price(h, pp), Price(l, pp), Price(c, pp),
            Quantity(v, sp),
            int(ts_ns[i]), int(ts_ns[i]))
        for i, (o, h, l, c, v) in enumerate(zip(
            df["open"], df["high"], df["low"], df["close"], df["volume"]))
    ]
```

**Neden önemli**: [[tutorial_loading_external_data]] sayfasındaki "CSV/DataFrame → `Bar`" workflow'u v2 için bu şekilde revize edilmeli. Arrow yolu artık production-grade ingest için (Parquet catalog), ama küçük veri için doğrudan `Bar()` construction daha ucuz.

### `engine.trader` gitti

- v1: `engine.trader.generate_positions_report()`.
- v2: `engine.generate_positions_report()` (report metodları doğrudan `BacktestEngine` üzerinde).

### `engine.portfolio.analyzer` gitti — [[portfolio]]

- v1: `engine.portfolio.analyzer.get_performance_stats_returns() -> dict[str, float]`.
- v2: `engine.portfolio.statistics() -> PortfolioStatistics(pnls, returns, general)`. Dict shape aynı; alan isimleri aynı ("Sharpe Ratio (252 days)" vs).

**Alternatif**: `engine.get_result() -> BacktestResult` — `stats_returns`, `stats_pnls`, `stats_general` property'leri var, aynı dict'i döner.

### `add_venue` kwarg sırası

- v1: `add_venue(..., base_currency=None, starting_balances=[...])`
- v2: `add_venue(..., starting_balances=[...], base_currency=None, ...)` — kwarg-order-sensitive olmayan kodlarda sorun yok; positional çağrılar kırılır.

## v2 rc1'de bilinen bug: Sharpe hesaplanmıyor — Asıl Neden Bulundu

Backtest sonrası `portfolio.statistics().returns` içinde:
- `Sharpe Ratio (252 days)` → **NaN**
- `Returns Volatility (252 days)` → **NaN**
- `Risk Return Ratio` → **NaN**
- `Sortino Ratio (252 days)` → doğru hesaplanıyor
- `Profit Factor`, `Average Win/Loss`, `Average (Return)` → doğru

**Asıl neden (2026-07-09 araştırmasıyla tespit edildi):** `PortfolioAnalyzer._calculate_portfolio_returns()` çok para birimli hesaplarda (len(balances) != 1) herhangi bir uyarı veya log olmaksızın `_empty_returns()` döndürür. Docstring bunu açıkça belgeliyor: "Multi-currency accounts are not yet supported." USDT+BTC çift bakiyeli hesap (CASH, base_currency=None) bu yolu tetikler.

**Çözüm (webapp'te uygulandı):**
1. BacktestEngine path'inde: `base_currency=USDT` ile tek-para-birimi hesap kullan — VEYA Sortino'yu kullan (downside-only, etkilenmiyor).
2. BacktestNode path'inde: stats yolu farklı olduğu için bu sorun yoktur (webapp'te doğrulandı: win_rate delta=0.000000, Sharpe=5.794).

Ayrıntı: [[portfolio]].

## Aynı kalanlar (rahatlatıcı)

Migration sürecinde *değişmeyen* API'ler:

- `Symbol`, `Venue`, `InstrumentId`, `TraderId` — construction ve string repr aynı.
- `BarType.from_str("SYMBOL.VENUE-STEP-UNIT-AGGREGATION-SOURCE")` — parser aynı.
- `Price.from_str`, `Quantity.from_str`, `Money(value, currency)` — aynı.
- `CurrencyPair`, `Equity` — kwarg listesi *azaldı* (v2'de fee/margin/max_quantity gibi alanlar opsiyonel), ama v1'de zorunlu olarak verilenler v2'de de kabul ediliyor. Geriye uyumlu.
- `engine.add_venue` / `add_instrument` / `add_data` / `add_strategy` / `run` — hepsi çalışıyor.
- `order_factory.market` / `.limit` / `.bracket` — imzalar korunmuş (v1 kwargs → v2 kwargs eşleşiyor). Plan öncesi endişelenen "builder-pattern zorunlu" iddiası **yanlış** çıktı — `bracket()` hâlâ 40+ kwarg alan geleneksel bir factory metodu.
- `Portfolio.is_net_long` / `.is_net_short` / `.is_flat` — aynı imzalar.
- `account.balance_total(currency) -> Money` — aynı.
- `Strategy.on_bar`, `.on_start`, `.on_stop`, `.subscribe_bars`, `.register_indicator_for_bars`, `.submit_order`, `.submit_order_list`, `.close_all_positions`, `.cancel_all_orders`, `.cache.instrument(...)`, `.cache.positions_open(...)` — hepsi çalışır durumda. Strategy runtime yüzeyi neredeyse dokunulmamış.
- `Indicators` (EMA, ATR, Bollinger, MACD, RSI, vb.) — construction ve `.value` / `.upper` / `.lower` / `.initialized` erişimi aynı.

Bu, migration'ın plan aşamasında beklenenden çok daha küçük olduğu anlamına gelir: yüzey yeniden yerleşimden ibaret, davranış değişmiyor.

## Migration şablonu

3 dosya, ~200 satır touch. Sıralama:

1. **Instrument construction dosyası** (webapp'te `backtest.py`): imports düzelt, `Currency.from_str`, `_bars_from_df` helper'ı yaz, `engine.trader` → `engine`, `portfolio.analyzer` → `portfolio.statistics()`.
2. **Strategy config dosyaları** (`strategies.py`, `composer.py`): `StrategyConfig` alt sınıflarını msgspec Struct kalıbından plain-class `__init__` kalıbına çevir. `frozen=True` metaclass kwarg'ını kaldır.
3. **Indicator modülünü flat import et**: `from nautilus_trader.indicators import EMA, BB, ATR, ...`

Parite kapıları için en az iki spec üzerinde koştur:
- Az sayıda trade veren bir strategy (webapp: supertrend → 1 trade / 6223.85 PnL) — trade emisyon parity'sini yakalar.
- Yüksek trade sayısı veren bir strategy (webapp: WebUI-Smoke → 53 trade / 1092.01 PnL) — fill logic parity'sini yakalar.

Her ikisi bit-identical eşleşirse migration temiz.

## Görmediğimiz şeyler (webapp scope dışı)

- [[message_bus|MessageBus]] encoding default değişikliği (MsgPack → JSON) — webapp mesaj bus'a doğrudan dokunmuyor.
- `bon` builder pattern — plan'da `CurrencyPair.builder()....build()` bekleniyordu; **RC1'de kwargs hâlâ çalışıyor**. Belki RC2/final'de gelecek, belki yalnızca Rust tarafına özel bir detay.
- `OrderRef` / `PositionRef` typed cache — plan'da endişe konusuydu; webapp cache.instrument()/positions_open()'i normal kullanıyor ve v2'de aynı obje shape'i geliyor (attribute access çalışıyor).
- Bracket order builder-pattern — hâlâ kwargs. [[order_flow_pipeline]] içindeki bracket örneği v2 için hâlâ geçerli.

## Cross-refs

- [[index_backtest_via_equity_proxy]] — Equity precision trap'ler v2'de de geçerli.
- [[tutorial_loading_external_data]] — CSV/DataFrame → Bar dönüşümü v2 için `_bars_from_df` şablonuyla revize edilmeli.
- [[strategy_and_actor]] — StrategyConfig plain-class kalıbı ilave edilebilir.
- [[precision_modes]] — v2'nin 128-bit price precision bacağı web app parite'sini bozmadı (webapp price_precision=2 zaten 64-bit yeterli).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[data_wranglers]]
- [[portfolio]]
- [[tutorial_quickstart]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
