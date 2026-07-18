---
title: LiveNode ve TradingNode
type: entity
status: draft
summary: Backtest edilen bileşenleri kod değişikliği olmadan canlıya taşıyan çalışma zamanı — TradingNode kuruluşu, emir komut sonuç politikası ve reconciliation.
key_concepts:
  - nautilus_kernel
  - environment_contexts
  - venue_reconciliation
  - configuration
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/live.md
related:
  - wiki/tutorials/tutorial_delta_neutral_bybit.md
  - wiki/tutorials/tutorial_quickstart.md
  - wiki/tutorials/howto_get_started_lighter.md
last_updated: 2026-07-13
---

# LiveNode ve TradingNode

Canlı işlem düğümü, backtest edilmiş stratejileri **kod değişikliği olmadan** gerçek piyasalara
taşıyan üst düzey çalışma zamanıdır: aynı actor, [[strategy_and_actor|strateji]] ve execution
algorithm bileşenleri her iki ortamda da çalışır. Bu parite [[environment_contexts]] tasarımının
sonucudur; geri test eşdeğeri [[backtest_node]] ile aynı [[nautilus_kernel]] çekirdeğini sarar.

## Kuruluş ve konfigürasyon

Düğüm, `TradingNodeConfig` altında venue başına data client ve exec client konfigürasyonlarıyla
kurulur; istemciler [[adapters]] katmanından gelir, çoklu-venue kablolama aynı konfigde birleşir
(v1.x). Canlı motor seçenekleri `LiveDataEngineConfig` / `LiveExecEngineConfig` ile verilir; config
struct semantiği (`T` vs `Option<T>`, builder) için bkz. [[configuration]]. v1.230.0 dokümanında
`TradingNodeConfig` ile Rust-tabanlı `LiveNodeConfig` birlikte geçer (v2.x'te esas ad `LiveNode`'dur).

## Emir komut sonuç politikası

Canlı komut sonucu dört sınıftan biridir (v1.230):

- **Venue onayı** — emir kabul edildi.
- **Kesin ret** — `OrderRejected` / `OrderModifyRejected` / `OrderCancelRejected`; HTTP
  400/401/403/429 yalnızca venue semantiği non-acceptance'ı kanıtlıyorsa kesin sayılır.
- **Yerel reddetme** — submit sistemden çıkmadan `OrderDenied`; `OrderSubmitted` hiç üretilmez.
- **Unresolved** — transport hatası, timeout, parse hatası: emir in-flight state'inde bekletilir;
  WebSocket, open-order polling, in-flight check veya startup reconciliation durumu çözer.

In-flight emir `SUBMITTED`, `PENDING_UPDATE` ya da `PENDING_CANCEL` durumundadır;
`inflight_check_retries` tükenince `SUBMITTED` emir `REJECTED`'a çözülür, diğer ikisi unresolved kalır.

## Reconciliation

Sadece `LiveExecutionEngine` reconciliation yapar — backtest'te [[execution_engine]] her iki
tarafı da kontrol eder. `reconciliation` kapatılmadıkça açılışta her venue için state hizalanır:

- `reconciliation_lookback_mins` boş bırakılırsa venue'nun verdiği maksimum geçmiş istenir (önerilen).
- Adaptörler `generate_order_status_reports` / `generate_fill_reports` / `generate_position_status_reports`
  ile "mass status" üretir; cache'li state için eksik event türetilir, cache boşsa sıfırdan yaratılır.
- Sahiplenilmeyen dış emirler `EXTERNAL` strateji ID'si alır (tag: `VENUE` veya `RECONCILIATION`);
  `external_order_claims` ile bir strateji bunları devralıp yönetmeye devam edebilir.
- Pozisyon farkları `generate_missing_orders` (varsayılan True) ile, PnL'i koruyan hesaplanmış
  fiyatlı LIMIT emirleriyle kapatılır; reconciliation başarısız olursa sistem loglar ve **başlamaz**.
- Startup sonrası sürekli kontroller sürer: in-flight izleme, open-order poll, pozisyon check,
  own-books audit (`reconciliation_startup_delay_secs` gecikmesinden sonra başlar).

Senaryo tabloları, partial-window düzeltmesi ve dört invariant (pozisyon miktarı, ortalama giriş
fiyatı — tolerans %0.01, PnL bütünlüğü, deterministik sentetik ID'ler) için bkz.
[[venue_reconciliation]]. Tüm execution event'lerini [[cache]] veritabanına kalıcılaştırmak,
venue geçmişine bağımlılığı azaltır ve kısa lookback ile bile tam kurtarma sağlar.

## Hata durumunda kapanma

`LiveNodeConfig.shutdown_on_error=True` iken kernel, ilk Rust `log::error!` kaydında
[[message_bus]] üzerinden `ShutdownSystem` komutu yayımlar ve normal stop yolunu izler (trader
durur, client'lar disconnect edilir, engine'ler durur); process abort edilmez. Tetikleyici Rust
`log` kayıtlarını gözler, Python `logging.error(...)` çağrılarını değil. Motor-başına
`graceful_shutdown_on_error` kaldırıldı (v1.230); davranış [[crash_only_design]] ile uyumludur.

```python
from nautilus_trader.live import LiveNodeConfig

config = LiveNodeConfig(shutdown_on_error=True)
```

## Bilinen boşluklar

- Adım adım kurulum, strateji konfigürasyonu ve çoklu-venue kablolama
  `how_to/configure_live_trading.md` rehberinde; bu sayfa henüz snapshot'lanmadı.
- Yaşam döngüsü olayları (start/stop/dispose sırası) ve sinyal yönetimi concepts/live.md'de yok.
- `LiveNode` / `TradingNode` adlarının Python-Rust yüzey ayrımı upstream'de tek yerde belgelenmemiş.
- Runtime check parametrelerinin tam listesi `LiveExecEngineConfig` API referansındadır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtest_node]]
- [[configuration]]
- [[venue_reconciliation]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
