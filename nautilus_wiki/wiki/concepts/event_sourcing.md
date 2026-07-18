---
title: Event Sourcing
type: concept
summary: Engine state'ini değiştiren mesajların kalıcı sıralı kaydı — event store 'nasıl gelindi'nin otoritesi, Cache write-through projeksiyon; boot recovery sweep.
status: draft
key_concepts:
  - events
  - cache
  - crash_only_design
  - event_driven_architecture
sources:
  - sources/06_concepts_docs_v1230.md
  - https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/docs/concepts/event_sourcing.md
last_updated: 2026-07-13
---

# Event Sourcing

Event sourcing, NautilusTrader'a engine state'ini değiştiren mesajların kalıcı ve sıralı bir kaydını verir. Temel ilke: **event store "buraya nasıl gelindi" sorusunun otoritesidir; [[cache|Cache]] ise "şu an ne doğru" sorusuna cevap veren bir write-through projeksiyondur** — source of truth değildir. Market data event store'a değil data catalog'a aittir; store yalnızca state'i etkileyen mesajları kaydeder.

(v1.230, alpha) Yakalama, replay, doğrulama ve recovery hedefli test kapsamına sahiptir; ancak API yüzeyi hâlâ evrilmektedir — buradaki kavramlar tasarım sözleşmesi olarak okunmalı.

## Neler kaydedilir

Store, tek trader instance'ının tek run'ı için state-etkileyen MessageBus trafiğini kaydeder:

- Execution komutları (submit, modify, cancel) ve data subscription komutları
- Ateşlenen `TimeEvent`'ler ve üretilen order/position/account [[events|event'leri]]
- Reconciliation öncesi ham venue raporları ve onlardan sentezlenen [[venue_reconciliation|reconciliation]] çıktıları
- `RunStarted` / `RunEnded` yaşam döngüsü girdileri

## Yakalama noktası: bus dispatch sınırı

Yakalama, [[event_driven_architecture]] gereği tüm trafiğin geçtiği [[message_bus]] dispatch sınırında, downstream handler'lar mesajı görmeden **önce** olur: state'i değiştirebilecek hiçbir handler, event store'un kabul etmediği bir mesajı göremez. Writer bounded bir kanal kullanır; eşiği aşan duraklamada Nautilus girdi düşürmek yerine kendini durdurur (halt). Aynı mantıksal mesaj birden çok bus sınırından geçse bile (örn. hem portfolio endpoint'ine hem strategy topic'ine yayın) mesaj kimliğiyle dedup edilir — replay aynı event'i iki kez uygulamaz.

## Entry modeli ve sıralama

Her girdi = yakalanan mesaj + metadata: `seq`, `ts_init`, `ts_publish`, `topic`, `payload_type`, `payload`, correlation/causation `headers`, `entry_hash`. **Replay sırasının otoritesi `seq`'tir**; timestamp'ler bağlam verir ama sırayı belirlemez. Correlation modeli üç kimlik düzeyi taşır: `correlation_id` (mantıksal iş akışı), `causation_id` (doğrudan ebeveyn mesaj) ve `command_id` / `event_id` / `report_id` (mesajın kendi kimliği).

## Run dosyaları ve manifest

Varsayılan backend `redb`: run başına bir dosya, `<base>/<instance_id>/<run_id>.redb`. Manifest run/build/config kimliğini (`binary_hash`, `config_hash`, opsiyonel seed) ve yaşam döngüsünü tutar; status dört değerden biridir: `Running`, `Ended`, `CrashedRecovered`, `Quarantined`.

## Crash recovery

[[crash_only_design]] ile uyumludur: tek recovery yolu bir sonraki boot'tur. Boot sweep'i manifest'i `Running` kalmış eski run dosyalarını tarar ve durable tail'e göre mühürler:

- Temiz tail, `RunEnded` yok → `CrashedRecovered` (sonraki run'a `parent_run_id` olabilen tek durum)
- Tail `RunEnded` ile bitiyor → `Ended`
- Hash uyuşmazlığı, gap veya yapısal bozulma → `Quarantined`

Sweep tek bozuk dosya yüzünden trader'ı açılamaz bırakmaz: hard-kill sonrası dosyalar redb repair pass'inden geçer, hâlâ okunamayanlar loglanıp atlanır.

## Snapshot-anchored replay

Cache snapshot'ları cache'e aittir; event store yalnızca çapayı (anchor: snapshot anındaki high-watermark + blob referansı) tutar. Yeniden inşa: en son snapshot yüklenir, `seq > anchor` girdileri sırayla uygulanır. Cache replay loader **state-only** çalışır — sentezlenmiş account/order/position event'lerini ve tam data yanıtlarını doğrudan [[cache|Cache]]'e uygular; strategy/actor kodu çalıştırmaz, venue sorgulamaz, reconciliation koşturmaz, replay girdilerini canlı bus'a yayınlamaz. [[nautilus_kernel|Kernel]]-yönetimli replay `EventStoreConfig::replay_from_run_id` ile tetiklenir. Not: canlı restart bugün hâlâ snapshot + reconcile yolunu kullanır; event-store recovery ancak yakalama kapsamı tamamlanınca canlı yol olacak.

Catalog-joined replay, [[parquet_data_catalog]] dilimlerini salt-okunur bir köprüyle girdilere ekleyebilir (bağlam analizi için); `seq` yine tek sıralama otoritesi kalır.

## Doğrulama ve DST ilişkisi

Her girdi kanonik `entry_hash` taşır; verifier hash'leri, manifest/high-watermark durumunu ve snapshot anchor'larını süreç-izole biçimde denetler (bozuk redb dosyası worker subprocess'i düşürür, çağıranı değil):

```bash
cargo run -p nautilus-event-store --bin verify -- ./event_store/trader-001/1700000000-cafe0001.redb
```

[[dst|DST]] ile iş bölümü: event store yakalanan girdi geçmişini, DST ise zamanlama/saat/seed determinizmini sağlar; `seed` + `binary_hash` + `config_hash` + `schema_version` ile run davranışı simülasyon kapsamı içinde yeniden üretilebilir.

## Bilinen boşluklar

- Data marker sidecar (opt-in market-data teslim sırası denetimi) ve retention planner modları (`Full` / `Bounded` / `SnapshotAnchored`) burada yalnızca anıldı; ayrıntı upstream'de.
- `EventStoreLifecycleOptions` / `MemoryBackend` ile simülasyon-içi yakalama ve encoder registry özelleştirmesi buraya alınmadı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[event_driven_architecture]]
- [[events]]
<!-- BACKLINKS:END -->
