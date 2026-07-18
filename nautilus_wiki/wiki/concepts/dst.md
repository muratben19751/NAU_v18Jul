---
title: DST (Deterministic Simulation Testing)
type: concept
summary: Seed-kontrollü madsim runtime'ıyla zamanlama davranışını bitwise tekrarlanabilir kılan test tekniği; DST burada yaz saati değil deterministic simulation testing.
status: draft
key_concepts:
  - rust_python_hybrid
  - single_threaded_core
  - event_driven_architecture
  - data_engine
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/dst.md
last_updated: 2026-07-13
---

# DST (Deterministic Simulation Testing)

**Not:** Upstream'de `docs/concepts/dst.md` yaz saatini (daylight saving time) değil, **deterministic simulation testing** kısaltmasını anlatır. DST, NautilusTrader'ı seed-kontrollü bir runtime altında çalıştırarak zamanlamaya duyarlı davranışı tek bir tamsayıdan bitwise tekrarlanabilir kılan bir test tekniğidir: aynı seed + binary + konfigürasyon, aynı gözlemlenebilir davranışı üretir; bir property başarısız olduğunda seed'in kendisi reprodüksiyondur (v1.230, Rust çekirdeği).

Hedef sınıf, diğer test katmanlarının kaçırdığı bug'lardır: channel wakeup sıralaması, shutdown drain yarışları, reconciliation sıralaması, recovery-path doğruluğu. FoundationDB'nin popülerleştirdiği desendir; Rust tarafında deterministik scheduler'ı [madsim](https://github.com/madsim-rs/madsim) sağlar.

## İki katmanlı mimari

- **Katman 1 — runtime swap**: `nautilus-common` üzerindeki `simulation` Cargo feature'ı + `RUSTFLAGS="--cfg madsim"` aktifken dört `tokio` alt modülü (`time`, `task`, `runtime`, `signal`) `madsim` üzerinden yönlendirilir. Re-export'lar `nautilus_common::live::dst` altındadır; normal derlemede gerçek `tokio`'ya çözülür. `sync`, `io`, `select!`, `fs`, `net` her koşulda gerçek tokio'da kalır.
- **Katman 2 — nondeterminizm seam'leri**: async runtime dışındaki belirsizlik açık seam'lerden geçirilir. Wall-clock okumaları `nautilus_core::time::duration_since_unix_epoch` üzerinden (Unix-epoch semantiği emir/fill zaman damgaları için korunur); monotonik okumalar `dst::time::Instant` üzerinden; iterasyon sırası önemli koleksiyonlar `AHashMap/AHashSet` yerine `IndexMap/IndexSet`; her `tokio::select!` çağrısı `biased;` ile.

## Determinizm sözleşmesi

Koşullar sağlandığında `(seed, binary hash, configuration hash)` ile tanımlı bir koşu, aynı platformda bitwise-özdeş üretir: async task scheduling sırası, timer ateşlemeleri (sanal monotonik + sanal wall-clock), `madsim::rand` RNG çıktısı ve tokio primitive'lerinde channel teslim sırası. Sözleşme ancak sekiz koşulun tümüyle geçerlidir; kritik olanı, feature ve cfg bayrağından biri eksikse sistemin **hatasız ve sessizce** gerçek tokio'ya düşüp determinizmi bozmasıdır.

## Statik enforcement

`check-dst-conventions` pre-commit hook'u (CI'da da koşar) `nautilus-live`'ın geçişli kapanışındaki 16 workspace crate'inde yasaklı desenleri yakalar: ham `Instant::now()` / `SystemTime::now()` / `chrono::Utc::now()` okumaları, seam dışı RNG (`rand::thread_rng`, `fastrand`, `OsRng`, cfg'siz `Uuid::new_v4()`), `biased;` içermeyen `select!`, cfg-gate'siz thread spawn / `spawn_blocking` ve sıra-hassas dosyalarda `AHashMap`. İstisnalar satır içi `// dst-ok` marker'ı veya hook içindeki küçük dosya allowlist'iyle verilir. Clippy politikası da `getrandom` ve `tokio::task::LocalSet`'i workspace genelinde engeller.

Sıra-hassas koleksiyon geçişleri motorların içine kadar iner: order matching engine'in dokuz alanı, reconciliation manager, hesap bakiyeleri, portfolio PnL toplulaştırması ve [[data_engine]] içindeki `bar_aggregators` / `book_snapshot_counts` kayıtları (yani [[bar_aggregation_and_type_syntax]] ile tanımlanan bar akışlarının aggregator state'i) `IndexMap`'e taşındı — çünkü iterasyon sırası, yayınlanan event sırasını ve seed'li `FillModel` RNG'sinin tüketimini belirleyebiliyor.

## Ağ seed soak'ları

`nautilus-network` Turmoil testleri iki katmandır: nightly'de koşan sabit-seed senaryolar (connect/reconnect/partition/drop) ve durdurulana dek seed tarayan soak:

```bash
env NAUTILUS_TURMOIL_SOAK_COUNT=100 scripts/soak-network-turmoil.sh
```

## Kapsam sınırları

Sözleşme bilinçli olarak dardır:

- **Python kapsam dışıdır**: DST native Rust harness altında koşar; PyO3 binding'leri, `ffi/` ve Python paketleri sözleşmeye dahil değildir. `time.time()` çağıran bir Python stratejisi komut akışını koşudan koşuya değiştirebilir; Rust çekirdeği değişen akışı deterministik işler ama uçtan uca replay garanti edilmez.
- **Platforma bağlıdır**: madsim'in libc intercept'leri platform-spesifiktir; Linux x86_64'te üreyen bir seed macOS aarch64'te üremeyebilir.
- **Transport I/O simüle edilmez**: WebSocket/HTTP (`tokio-tungstenite`, `reqwest`, `redis`, `sqlx`) gerçek soketlerde koşar; ilk hedef transport fault injection değil emir yaşam döngüsü determinizmidir.
- **Logging gerçek OS thread'inde**: simülasyon altında writer thread açılmaz, log event'leri düşürülür; log çıktısı sözleşme dışıdır.
- **Adapter crate'leri kapsam dışıdır**; DST yoluna girmeden önce ayrı audit gerektirirler.

## Bilinen boşluklar

- Uçtan uca determinizm doğrulaması (aynı seed'le iki koşunun diff'i) henüz bir regression gate ile kanıtlanmıyor; yapısal koşullar zorlanıyor, iddia "seam tasarımından makul" düzeyinde.
- Upstream'in ayrıntılı implementation-notes envanteri (seam bazında dosya:satır listeleri, process-global lazy RNG tüketimi, `CacheView` kısıtı) bu sayfada özetlenmedi.
- Yaz saati (daylight saving) geçişlerinin bar/zaman damgalarına etkisini anlatan ayrı bir upstream doc v1.230.0'da bu yolda yok; zaman damgası disiplini için motorun her yerde UTC nanosaniye (`ts_event`/`ts_init`) kullandığı genel kuralı geçerlidir.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[bar_aggregation_and_type_syntax]]
- [[event_sourcing]]
<!-- BACKLINKS:END -->
