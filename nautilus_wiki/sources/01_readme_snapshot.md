---
source: https://github.com/nautechsystems/nautilus_trader
retrieved: 2026-07-05
type: repo_readme_summary
immutable: true
---

# NautilusTrader — README Snapshot

## Genel Bakış
Rust-native, açık kaynak, çok varlıklı ve çok-venue destekli bir alım-satım motoru. Rust çekirdek performans/güvenlik sağlar; Python kontrol düzlemi strateji geliştirme için kullanılır. Aynı strateji kodu backtest'ten canlıya kadar değişmeden çalışır ("research-to-live parity").

## Özellikler
- **Performans**: Rust core, tokio async networking, mimalloc allocator
- **Güvenilirlik**: Rust type/thread safety, opsiyonel Redis state persistence
- **Esneklik**: Modüler adaptörlerle her REST/WebSocket entegre edilebilir
- **İleri emir tipleri**: IOC, FOK, GTC, GTD, DAY, OCO, OUO, OTO
- **Multi-venue**: Aynı anda birden çok borsada işlem
- **Backtesting**: Nanosaniye çözünürlükte tarihsel veri
- **AI/ML uyumlu**: RL/ES ajanları eğitecek kadar hızlı motor

## Entegrasyonlar (17+)
- Kripto: Binance, Coinbase, Kraken, OKX, Bybit, Deribit
- DEX: dYdX, Hyperliquid, Derive, Lighter
- Geleneksel: Interactive Brokers
- Veri: Databento, Tardis
- Bahis: Betfair, Polymarket

## Teknik Yığın
- Diller: Rust (%70.8), Python (%22.8), Cython (%5.4)
- Platformlar: Linux, macOS, Windows (x86_64, ARM64)
- Python: 3.12–3.14
- Rust: 1.96.1+ (stable takip)

## Kurulum
1. PyPI veya Nautech pkg index'ten önceden derlenmiş wheel
2. Kaynaktan derleme (Rust toolchain, clang, build deps)
3. Docker (JupyterLab varyantları dahil)

## Hassasiyet Modları
- **High-precision**: 128-bit integer, 16 ondalık basamak
- **Standard-precision**: 64-bit integer, 9 ondalık basamak

## Geliştirme
- cargo-nextest ile izole Rust testleri
- Makefile ile build/test/lint/docs otomasyonu
- Pre-commit hook'lar, GitHub Actions CI

## Güvenlik
İmzalı sürümler, immutable tag'ler, cryptographic checksum'lu bağımlılık pinleme, CodeQL/cargo-audit/OSV taramaları, SLSA build provenance, Sigstore imzalı Docker image'ları, OpenSSF Scorecard.

## Lisans & Topluluk
LGPL-3.0-or-later. CLA gerekli. Discord'da topluluk; güvenlik: GitHub Security Advisories veya security@nautechsystems.io.

## Durum
v2 release-candidate fazı, iki haftada bir sürüm. `master` = stabil, `develop` = aktif geliştirme.
