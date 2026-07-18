---
title: Logging
type: entity
summary: Rust tabanlı ortak logging alt sistemi — MPSC kanalıyla ayrı thread'e akış, seviye/renk/bileşen filtreleri, dosya-stdout hedefleri ve bypass_logging.
status: draft
key_concepts:
  - configuration
  - nautilus_kernel
  - single_threaded_core
  - rust_python_hybrid
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/logging.md
last_updated: 2026-07-13
---

# Logging

Backtest ve live tarafında aynı **Rust tabanlı logging alt sistemi** kullanılır; `log` crate'inin standart facade'ı üzerine kuruludur. Log mesajları bir **MPSC (multi-producer single-consumer) kanalı** üzerinden ayrı bir logging thread'ine akar — string formatlama ve dosya I/O'su ana thread'i bloklamaz. `BacktestEngine` veya `TradingNode` gibi bir [[nautilus_kernel|NautilusKernel]] kuran nesneler logging'i otomatik başlatır.

## Seviyeler

`OFF` → `TRACE` → `DEBUG` → `INFO` (varsayılan eşik) → `WARNING` → `ERROR`. `TRACE` yalnızca Rust bileşenlerince üretilir; Python kodu TRACE yayamaz ama filtre seviyesi olarak seçip Rust trace'lerini yakalayabilir.

## LoggingConfig

Yapılandırma, [[configuration]] sözleşmesindeki `LoggingConfig` ile yapılır ve `TradingNodeConfig` / `BacktestEngineConfig`'in `logging` alanına verilir (her ikisinde aynı seçenekler geçerlidir):

```python
from nautilus_trader.config import LoggingConfig, TradingNodeConfig

config_node = TradingNodeConfig(
    trader_id="TESTER-001",
    logging=LoggingConfig(
        log_level="INFO",
        log_level_file="DEBUG",
        log_file_format="json",
        log_component_levels={"Portfolio": "INFO"},
    ),
)
```

Öne çıkan alanlar (v1.230): `log_level` (stdout/stderr eşiği), `log_level_file`, `log_file_format` (`None` = düz metin `.log`, `"json"` = `.json`), `log_directory`, `log_file_name`, `log_file_max_size`, `log_file_max_backup_count` (varsayılan 5), `log_component_levels`, `log_components_only`, `log_colors` (ANSI renkleri; desteklemeyen ortamlarda kapatılır), `use_pyo3` (Rust bileşenlerinin log'larını yakala), `use_tracing`, `clear_log_file` (başlangıçta mevcut dosyayı sıfırla) ve `bypass_logging` (alt sistemi tamamen atlar).

## Dosya çıktısı ve rotasyon

- Varsayılan adlandırma: `{trader_id}_{%Y-%m-%d}_{instance_id}.{log|json}`; boyut limiti yoksa dosya **UTC gece yarısında** günlük döner.
- `log_file_max_size` verilirse boyut-tabanlı rotasyon devreye girer; ad kalıbı milisaniyeli timestamp içerir ve özel isimle birleştirilse bile boyut rotasyonu önceliklidir.
- Özel `log_file_name` + boyut limiti yok → aynı dosyaya sürekli append (rotasyon yok).
- `log_file_max_backup_count` limiti aşılınca en eski yedekler otomatik silinir.

## Bileşen filtreleme ve NAUTILUS_LOG

`log_component_levels` bileşen bazında eşik atar (`{"RiskEngine": "DEBUG"}`); `log_components_only=True` yalnız listelenen bileşenleri geçirir — liste boşsa **hiç log üretilmez**. Alternatif olarak `NAUTILUS_LOG` ortam değişkeni noktalı virgülle ayrılmış spec alır: `NAUTILUS_LOG="stdout=Info;fileout=Debug;RiskEngine=Error;is_colored"`. `::` içeren anahtarlar Rust modül-yolu filtresi olarak prefix eşleşir (en uzun prefix kazanır); Python `log_component_levels` yalnız tam ad eşleşmesi yapar.

## LogGuard ve çoklu engine

Süreç başına logging alt sistemi **bir kez** `init_logging()` ile kurulur; sonrasında `Logger("MyLogger")` nesneleri Python'un built-in `logging` API'sine benzer şekilde her yerden kullanılabilir. `LogGuard` referans sayacıyla logging thread'ini canlı tutar (süreç başına en çok 255 guard); son guard düştüğünde thread join edilir ve bekleyen mesajlar flush edilir. Aynı süreçte ardışık backtest'ler koşarken ilk engine'den `engine.get_log_guard()` alınıp saklanmazsa, ilk `engine.dispose()` kanalı kapatır ve sonraki engine'ler "Error sending log event" hatası üretir.

## Tracing subscriber

`tracing` crate'i kullanan harici Rust kütüphaneleri (hyper, tokio...) için `use_tracing=True` ya da `init_tracing()` ayrı bir fmt-layer açar; filtreleme yalnızca `RUST_LOG` ortam değişkeniyle yapılır (varsayılan `warn`) ve çıktı Nautilus log dosyalarına girmez, doğrudan stdout'a gider. `log` crate kullanan kütüphaneler (ör. rustls) ise Nautilus logger'dan geçer ve `LoggingConfig` ile filtrelenir.

## Bilinen boşluklar

- Windows'ta interpreter kapanışı sırasında GC gecikmesi son `LogGuard` düşüşünü geciktirirse log'lar kırpılabilir (upstream issue #3027); deterministik kapanış mekanizması hâlâ açık.
- `bypass_logging`'in tam kapsamı (hangi yolların atlandığı) pinli sayfada tek satırdır; detay API referansındadır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[configuration]]
- [[nautilus_kernel]]
<!-- BACKLINKS:END -->
