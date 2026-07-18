---
title: Plugins
type: concept
summary: nautilus-plugin crate'inin artifact sözleşmesi — sürümlü build metadata + manifest ile kendini tanıtan Rust cdylib'leri ve C-ABI boundary tipleri (v1.x).
status: draft
key_concepts:
  - rust_python_hybrid
  - nautilus_kernel
  - configuration
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/plugins.md
last_updated: 2026-07-13
---

# Plugins

Nautilus'un eklenti mimarisinin temeli `nautilus-plugin` crate'idir: bağımsız derlenmiş bir
Rust `cdylib`'inin kendini sürümlü build metadata'sı ve bir manifest ile tanıtmasını sağlayan
"artifact sözleşmesi"ni ve bu sınırda kullanılan C-ABI boundary primitive'lerini tanımlar.
Çekirdeğin Rust'a taşınması sürecinin ([[rust_python_hybrid]]) bir parçası olan bu katman,
üçüncü taraf bileşenlerin çekirdekten bağımsız derlenip dağıtılabilmesinin ön koşuludur.

## Eklenti nedir?

Bir eklenti (plug-in), tek bir `nautilus_plugin_init` giriş sembolü export eden Rust
`cdylib`'idir. `nautilus_plugin!` makrosu hem bu sembolü hem de build kimliğini (name,
vendor, version) taşıyan statik manifesti üretir:

```rust
nautilus_plugin::nautilus_plugin! {
    name: "example-plugin",
    vendor: "Nautech",
    version: env!("CARGO_PKG_VERSION"),
}
```

## Derleme gereksinimleri

- `Cargo.toml` içinde `crate-type = ["cdylib"]` ayarlanır.
- Eklenti, hedeflenen çekirdekle **eşleşen** `nautilus-plugin` sürümüne bağımlı olmalıdır.
- Boundary ve manifest tiplerinin ayrıntısı crate dokümanındadır (docs.rs/nautilus-plugin).

## Kapsam ve sınırlar

`nautilus-plugin` bilinçli olarak dar tutulmuştur: yalnızca **artifact kimliği** ve **sınır
tiplerini** kapsar. Eklentilerin yüklenmesi (loading), kayıt edilmesi (registration) ve
çalıştırılması bu crate'in işi değildir — upstream doc (v1.230.0) bu mekanizmaları
belgelemez. Dolayısıyla bir eklentinin [[nautilus_kernel]] yaşam döngüsüne hangi noktada
bağlandığı ve [[configuration]] üzerinden nasıl etkinleştirileceği resmi dokümantasyonda
şimdilik tanımsızdır; sözleşme yalnızca "kendini tanıtma" tarafını sabitler.

## Kararlılık

Plug-in ABI'si **early alpha** ve sözleşme kararsızdır (v1.230.0). Resmi öneri, eklenti
build'lerini kullanılan `nautilus-plugin` sürümüne sabitlemektir (pinning); aksi halde
ABI kırılmaları sessiz uyumsuzluk üretebilir.

## Bilinen boşluklar

- Kayıt/keşif (registration/discovery) ve yükleme akışı upstream'de belgelenmemiş; doc
  yalnızca artifact sözleşmesini anlatıyor.
- Eklentilerin Python katmanından görünürlüğü ve kernel'e bağlanma noktası açık değil;
  v2 kararlılaştıkça yeniden ingest gerektirir.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[configuration]]
<!-- BACKLINKS:END -->
