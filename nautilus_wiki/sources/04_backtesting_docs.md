---
source: https://nautilustrader.io/docs/latest/concepts/backtesting
retrieved: 2026-07-05
type: docs_snapshot
immutable: true
---

# Backtesting (Docs) Snapshot

## İki API Seviyesi

**Low-level API**: `BacktestEngine` üzerine kurulu, doğrudan kontrol.
Kullanım koşulları:
- Tüm veri belleğe sığıyor
- Ham veri formatlarını (CSV, binary) korumak istenir
- Bileşen değişimi/parametre ayarı üzerinde ince kontrol gerekir

**High-level API**: `BacktestNode`, birden fazla `BacktestEngine`'i orkestre eder.
Kullanım koşulları:
- Veri bellek kapasitesini aşıyor
- `ParquetDataCatalog` kolaylığı istenir
- Birden çok backtest konfigürasyonu paralel yönetilecek

## Performans İpucu
Büyük çok-instrument datasetlerde: her yüklemede sıralama yerine sonda tek sefer sırala. Ya da tüm veriyi topla ve tek batch olarak ekle.

## Execution Simülasyonu
Matching engine her veri noktası için üç faz:
1. Exchange order book'u günceller
2. Stratejiler veriyi alır, emir üretebilir
3. Venue aynı timestamp içinde emir zincirlerini settle eder

Bar verisinde OHLC sırayla işlenir; take-profit ve stop-loss aynı bar içindeyse adaptive high/low sıralama doğruluğu artırır.

## Veri Granülaritesi
Azalan detay sırası: L3 order book → L2 market-by-price → quote tick → trade tick → bar.
Venue'nun `book_type` konfigürasyonu veri seviyesiyle eşleşmeli; platform düşük seviye veriden yüksek seviye üretmez.
