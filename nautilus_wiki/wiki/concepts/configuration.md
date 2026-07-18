---
title: Configuration
type: concept
summary: Her bileşenin tipli config struct'ıyla yapılandırılması — tasarım ilkeleri, Python/Rust desenleri, ortak adaptör alanları ve engine config örneği.
status: draft
key_concepts:
  - nautilus_kernel
  - live_node
  - logging
  - plugins
  - rust_python_hybrid
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/configuration.md
last_updated: 2026-07-13
---

# Configuration

NautilusTrader'da her bileşen — data client, execution client, engine, strategy — kendine ait **tipli bir config struct'ı** ile yapılandırılır. Config, bileşen davranışını kontrol eden tek sözleşmedir; kod içine gömülü ayar yoktur.

## Tasarım ilkeleri

- **Varsayılanlar config sınırında çözülür.** Timeout, retry sayısı, backoff gecikmesi gibi her zaman anlamlı bir varsayılanı olan alanlar düz tiptir (`u64`, `u32`); downstream kod çözülmüş değer alır, defaulting mantığını tekrarlamaz.
- **`Option<T>` = anlamsal yokluk.** `None` yalnızca gerçek bir anlam taşıdığında kullanılır: özellik kapalı, lookback sınırsız, değer runtime'da ortamdan geliyor. Her zaman somut değere çözülen alan `Option` ile sarılmaz.
- **Varsayılanların tek kaynağı.** Rust config'leri `bon::Builder` türetir; varsayılanlar `#[builder(default = value)]` ek açıklamalarıyla tek yerde durur, `Default` impl'i builder'a delege eder — ikinci bir kopya sürüklenip eskiyemez.
- **Bilinmeyen alan = hata.** Config decode aşamasında fazladan anahtar bug sayılır: yazım hatası, rename sonrası bayat isim ve kopyala-yapıştır kazaları node/client başlamadan yakalanır.

## Python tarafı: NautilusConfig ailesi

Tüm Python config sınıfları `NautilusConfig`'ten türer (v1.x). Taban sınıf msgspec `Struct` üzerine kuruludur ve `forbid_unknown_fields=True` ayarlar — bilinmeyen anahtar decode sırasında `msgspec.ValidationError` fırlatır. Config nesneleri oluşturulduktan sonra dondurulmuştur (frozen/immutable) ve `dict()` / `json()` ile serileştirilir; bir backtest ya da live oturumun yapılandırması olduğu gibi kaydedilip geri yüklenebilir (v1.x, API referansı).

Düz alanlarda `None` "varsayılanı kullan" demektir; `Option` alanlarda ise alanın opsiyonel anlamını (kapalı/sınırsız) korur:

```python
from nautilus_trader.config import LiveExecEngineConfig

config = LiveExecEngineConfig(
    reconciliation=True,
    open_check_interval_secs=30.0,   # açık emir polling'ini aç
    open_check_lookback_mins=60,     # 60 dakika geriye bak
    # position_check_interval_secs=None → özellik varsayılan olarak kapalı
)
```

## Üst-düzey config'ler: TradingNodeConfig ve BacktestEngineConfig

İki kök config aynı kernel sözleşmesini paylaşır: `TradingNodeConfig` bir [[live_node|TradingNode]]'u, `BacktestEngineConfig` bir BacktestEngine'i yapılandırır. Her ikisi de [[nautilus_kernel|NautilusKernel]]'in kuracağı bileşenlerin alt-config'lerini taşır — ör. `logging` alanına verilen `LoggingConfig` ([[logging]]), data/risk/exec engine config'leri ve venue client config'leri. Strategy ve actor'lar "importable" yol + config çifti olarak tanımlanır; [[plugins]] ile eklenen harici paketler de aynı config sözleşmesine uyar.

## Rust tarafı

Rust config struct'ları `bon::Builder` ile üç eşdeğer biçimde kurulur: builder zinciri (`Config::builder().http_timeout_secs(30).build()`), `..Default::default()` spread'li struct literal ve `Config::default()`. Serde ile deserialize edilenler ayrıca `#[serde(deny_unknown_fields)]` ayarlar. Python/Rust iş bölümü için bkz. [[rust_python_hybrid]].

## Ortak adaptör alanları

Çoğu adaptör config'i şu alanları paylaşır (v1.230): `http_timeout_secs` (60), `max_retries` (3), `retry_delay_initial_ms` (1000), `retry_delay_max_ms` (10 000), `heartbeat_interval_secs` ve `recv_window_ms` (venue'ya göre değişir), `update_instruments_interval_mins`. Adaptöre özgü alanlar (rate limit, polling, margin modu) ilgili adaptörün entegrasyon rehberindedir.

## Bilinen boşluklar

- Pinli upstream sayfa `TradingNodeConfig` / `BacktestEngineConfig` alan listelerini vermez; alan-düzeyi detay live/backtest doc'larında ve API referansındadır.
- Dondurma (frozen struct) ve `json()` / `dict()` / `parse()` serileştirme yüzeyi upstream concept sayfasında değil API referansında belgelidir; buradaki özet v1.x davranışıdır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[live_node]]
- [[logging]]
- [[nautilus_kernel]]
- [[plugins]]
<!-- BACKLINKS:END -->
