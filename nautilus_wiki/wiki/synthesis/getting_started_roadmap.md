---
title: Nereden Başlamalı? — Yeni Kullanıcı Yol Haritası
type: synthesis
sources:
  - sources/01_readme_snapshot.md
  - sources/02_architecture_docs.md
  - sources/03_strategies_docs.md
last_updated: 2026-07-05
summary: Kurulumdan canlıya beş adımlı akış — kavramsal zemin, low-level BacktestEngine, ParquetDataCatalog geçişi, sandbox doğrulama; tek-thread ve precision tuzakları uyarısı.
key_concepts:
  - event_driven_architecture
  - environment_contexts
  - strategy_and_actor
  - backtesting_guide
  - single_threaded_core
  - precision_modes
---

# NautilusTrader'a Başlangıç Yol Haritası

## 1. Kurulum
En hızlı yol PyPI wheel:
```
pip install nautilus_trader
```
Docker'da JupyterLab varyantı örnek notebook'larla gelir; deneme için ideal.

## 2. Kavramsal Zemin
Şu üç sayfa önce okunmalı:
- [[event_driven_architecture]]
- [[environment_contexts]]
- [[strategy_and_actor]]

## 3. İlk Backtest
- [[tutorial_quickstart]] ile beş dakikada uçtan uca çalıştır
- Low-level `BacktestEngine` ile başla (küçük veri, öğrenme kolaylığı)
- Bar verisiyle basit bir momentum stratejisi
- Sonra [[parquet_data_catalog|ParquetDataCatalog]] ve [[backtest_node|BacktestNode]]'a geç
- Rehber: [[backtesting_guide]]

## 4. Sandbox
Gerçek zaman veriyle simüle venue'da stratejini çalıştır — canlıya geçmeden davranışı doğrula.

## 5. Live
Önce paper hesapla, sonra küçük sermayeyle. Aynı strateji kodu çalışır.

## Yaygın Tuzaklar
- Venue `book_type` ile veri granülaritesi uyumsuzluğu → sessiz yanlış fill
- Actor içinde blocking iş yapmak → tek thread'i durdurur ([[single_threaded_core]])
- Precision modu yanlış seçimi ([[precision_modes]])

## Uygulamalı örnek
Nautilus wiki'sinin yaklaşımını canlı bir kod tabanında görmek istersen [[webapp_module_map|nautilus_web_app modül haritası]] ile her Python dosyasının hangi wiki sayfasına karşılık geldiğine bak.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtest_node]]
- [[tutorial_loading_external_data]]
- [[tutorial_quickstart]]
<!-- BACKLINKS:END -->
