---
title: Precision Modes
type: concept
sources:
  - sources/01_readme_snapshot.md
last_updated: 2026-07-13
summary: High-precision (128-bit) ile standard (64-bit) fiyat/miktar temsili arasında derleme zamanı seçim — kripto için hassasiyet, geleneksel piyasalar için hız.
key_concepts:
  - rust_python_hybrid
  - adapters
  - data_engine
  - v1_to_v2_migration_lessons
---

# Precision Modes

Fiyat ve miktar temsili için iki mod.

| Mod | Integer | Ondalık |
|---|---|---|
| High-precision | 128-bit | 16 basamağa kadar |
| Standard-precision | 64-bit | 9 basamağa kadar |

## Ne Zaman Hangisi
- Kripto (özellikle satoshi-altı hassasiyet, DeFi token'lar): high-precision
- Geleneksel piyasa (hisse, futures): standard yeterli, daha hızlı

Bu bir derleme zamanı seçimidir; wheel/binary seçerken dikkat. Price/Quantity/Money değerlerinin fixed-point temsili ve aritmetik kuralları için bkz. [[value_types]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[continuous_futures]]
- [[data_wranglers]]
- [[getting_started_roadmap]]
- [[instruments]]
- [[v1_to_v2_migration_lessons]]
- [[value_types]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
