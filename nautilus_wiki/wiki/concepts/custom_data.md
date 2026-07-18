---
title: Custom Data
type: concept
summary: Python/Rust custom veri tiplerinin runtime, persistence ve query hattına katılımı — PyO3 mimarisi, publish/subscribe ve katalog kalıcılığı.
status: draft
key_concepts:
  - data_engine
  - message_bus
  - parquet_data_catalog
  - rust_python_hybrid
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/custom_data.md
last_updated: 2026-07-13
---

# Custom Data

Nautilus, Python veya Rust'ta yazılmış custom veri tiplerini platformun geri kalanının kullandığı runtime, persistence ve query hattından geçirir. Yerleşik tipler için ham veriyi Nautilus nesnelerine çeviren [[data_wranglers]] katmanının aksine, custom data nesnelerini kullanıcı kendisi üretir; kayıt sonrası aynı nesneler publish edilebilir, abone olunabilir, katalogda saklanıp sorgulanabilir. Bu sayfa PyO3 tabanlı sistemi anlatır; Cython `@customdataclass` ayrı (legacy) bir sistemdir (v1.x).

## İki yazım modu

| Mod | Tanım | Kayıt | Encode/decode |
|---|---|---|---|
| Pure Python | `@customdataclass_pyo3` sınıfı | `register_custom_data_class(...)` | Python callback + Arrow C FFI |
| Same-binary Rust | `#[custom_data]` / `#[custom_data(pyo3)]` | `ensure_custom_data_registered::<T>()` | Native Rust |

İki mod da FFI sınırında aynı dış `CustomData` wrapper'ında ve aynı `DataType` kimlik modelinde birleşir. `register_custom_data_class` önce native Rust kaydını dener, yoksa pure Python fallback'e düşer — main binary'nin tanıdığı tipler için en hızlı yol korunur.

## DataType kimliği

`DataType(type_name, metadata=None, identifier=None)` custom veriyi routing ve persistence için tanımlar. Eşitlik, hash ve message bus topic'i yalnızca `type_name` + `metadata`'dan türetilir; `identifier` sadece katalog yolunu (`data/custom/<type_name>/<identifier...>`) etkiler. Aynı type_name/metadata'lı iki `DataType`, identifier'ları farklı olsa da aynı topic'e publish eder.

```python
register_custom_data_class(MyMetric)             # JSON + Arrow handler kaydı
data_type = DataType("MyMetric", metadata={"venue": "BINANCE"})
data = CustomData(data_type, my_metric)          # dış wrapper: önce DataType, sonra payload
```

`CustomData` wrapper'ı bir `DataType` ile `CustomDataTrait` implemente eden iç payload taşır; `ts_event`/`ts_init` iç payload'dan delege edilir. JSON serileştirmede tek kanonik zarf kullanılır (`type`, `data_type`, `payload`) — kullanıcı struct'ları wrapper metadata'sıyla çakışmadan istediği alan adlarını kullanabilir.

## Runtime akışı: publish/subscribe

Kayıtlı bir custom tip diğer veri aileleriyle aynı runtime arayüzlerinden akar: data engine `CustomData`'yı [[message_bus]] üzerinden publish eder, switchboard topic'i `DataType`'tan türetir, actor abonelikleri custom veriyi yakalar ve [[strategy_and_actor|strategy]]'ye `on_data` ile ulaştırır. Backtest engine `Data::Custom`'ı exchange'e yönlendirilen değil, [[data_engine]] tarafından teslim edilen girdi olarak işler.

## Katalogda saklama

[[parquet_data_catalog|ParquetDataCatalog]] yerleşik tiplerin statik şemalarının aksine custom tipleri runtime'da çözer: merkezi `DataRegistry` (OnceLock ile başlatılan DashMap singleton'ları) JSON/Arrow encoder-decoder'ları ve Python extractor'ları `type_name` anahtarıyla saklar; eşzamanlı kayıtlar atomik `entry()` sayesinde yarışmaz.

- **Yazma**: `DataType`'tan type_name/metadata/identifier çıkarılır → encoder bulunur → `RecordBatch` üretilir → şemaya type_name/metadata eklenip `data/custom/...` altına Parquet yazılır.
- **Okuma**: şema metadata'sından `type_name` okunur → decoder bulunur → batch decode edilip orijinal `DataType` ile `CustomData` yeniden kurulur. Backtest sonrası Feather → Parquet dönüşümünde de custom-data dalı aynı yoldan geçer.

Pure Python tiplerde encode/decode, serileştirme maliyeti olmadan Arrow C FFI köprüsüyle (`_export_to_c` / `_import_from_c`) Python-Rust arasında taşınır; same-binary Rust tipler bu köprüyü kullanmaz, native handler'larla çalışır.

## SQL ve Redis cache

PostgreSQL custom veriyi `custom` tablosunda tam JSON payload ile saklar (`add_custom_data` / `load_custom_data`); Redis `custom:<ts_init>:<uuid>` anahtarları kullanır, `DataType` alanlarına göre filtreleyip `ts_init` sıralı sonuç döndürür. Bu katman [[cache]] persistence'ının parçasıdır.

## Bilinen boşluklar

- Upstream sayfa mimari odaklıdır; actor-level `publish_data`/`subscribe_data` kullanım örnekleri ve klasik Cython `@customdataclass` rehberi (v1.x kullanıcı yolunun büyük kısmı) bu sayfada yer almaz.
- Mermaid sequence/flow diyagramları ve `crates/...` dosya yolu ayrıntıları bu senteze alınmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[data_engine]]
- [[data_wranglers]]
- [[option_greeks_pipeline]]
<!-- BACKLINKS:END -->
