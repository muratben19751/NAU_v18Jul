# NautilusTrader Wiki — Schema (CLAUDE.md)

Bu wiki, Karpathy'nin **"LLM Knowledge Base / LLM Wiki Pattern"** yaklaşımına göre kurulmuştur (bkz. https://karpathy.bearblog.dev/llm-knowledge-bases/ ve https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Amaç: NautilusTrader (https://github.com/nautechsystems/nautilus_trader) hakkında bilinen her şeyi, bir LLM ajanının **hem okuyabileceği hem de bakımını yapabileceği** düz-metin bir bilgi tabanında canlı tutmak. Kullanıcı wiki'yi doğrudan düzenlemez — sorular sorar, çıktılar üretir, boşlukları raporlar; LLM sayfaları yazar/günceller.

## Katmanlı Yapı

```
nautilus_wiki/
├── CLAUDE.md              # Schema — bu dosya
├── index.md               # Kategorik katalog (üretilen, elle düzenlenmez)
├── log.md                 # Append-only işlem günlüğü
├── sources/               # Katman 1 — Ham, değiştirilmez kaynak snapshot'ları
├── wiki/                  # Katman 2 — LLM'in sahibi olduğu sentezlenmiş sayfalar
│   ├── entities/          # Somut şeyler: bileşenler, adaptörler, veri kaynakları
│   ├── concepts/          # Soyut fikirler: mimari desenler, iş akışları
│   ├── synthesis/         # Sentez: karşılaştırmalar, tavsiyeler, migration rehberleri
│   └── tutorials/         # Resmi öğreticilerin sentezleri
├── lint/                  # Health-check raporları (YYYY-MM-DD_health.md)
├── tools/                 # wiki_tools.py — CLI: index, backlinks, lint, search, stub, resolve
└── .obsidian/             # Obsidian workspace ayarları (frontend olarak kullanılır)
```

### Katman 1 — `sources/`
- **Sadece okunur.** LLM asla düzenlemez.
- Her dosya frontmatter içermeli: `source`, `retrieved`, `type`, `immutable: true`.
- URL değişse bile içerik korunur; yeni sürüm ayrı dosya olarak eklenir (`05_...`).

### Katman 2 — `wiki/`
- LLM tam sahibidir.
- **Zorunlu frontmatter:** `title`, `type`, `summary`, `sources`, `last_updated`.
- **Opsiyonel:** `status: stub | draft | frozen`, `key_concepts`.
- Sayfalar arası bağ **bare-name wikilink** biçiminde: `[[message_bus]]`, `[[message_bus|MessageBus]]`. Path'li `[[wiki/entities/message_bus.md]]` deprecated — `tools/wiki_tools.py backlinks` ve FastAPI renderer her ikisini de kabul eder ama yeni sayfalarda bare form kullanılır.

### Katman 3 — Bu dosya (`CLAUDE.md`)
- Şemayı, adlandırma kurallarını ve iş akışlarını tanımlar.

## Frontmatter Referansı

```yaml
---
title: MessageBus                             # Human-friendly
type: entity                                  # entity | concept | synthesis | tutorial
summary: >-                                   # 1 cümle, <=180 karakter — index.md için
  Bileşenler arası pub/sub, req/res, cmd/event omurgası; opsiyonel Redis
  state persistence ile crash-only tasarıma uyar.
status: draft                                 # opsiyonel: stub | draft | frozen
key_concepts:                                 # opsiyonel: bare-name slug listesi
  - event_driven_architecture
  - crash_only_design
sources:                                      # ZORUNLU — Layer 1 anchor'lar (sources/*.md veya URL)
  - sources/02_architecture_docs.md
  - https://nautilustrader.io/docs/latest/concepts/architecture
related:                                      # opsiyonel — Layer 2 wiki cross-reference'ları
  - wiki/tutorials/tutorial_backtest_low_level.md
  - wiki/synthesis/v1_to_v2_migration_lessons.md
last_updated: 2026-07-07
---
```

- **`summary` alanı zorunlu.** `tools/wiki_tools.py index` katalog satırlarını buradan üretir; boşsa sayfa katalogda kısa görünür.
- **`sources` her zaman Layer 1 kaynak izlenebilirliği içindir.** Yalnızca `sources/*.md` snapshot dosyaları ve URL'ler kabul edilir. Wiki sayfası başka bir wiki sayfasını kaynak olarak gösteremez — o Layer 2 türetmedir.
- **`related` opsiyoneldir**; sayfanın kavramsal komşusu olan (ancak Layer 1 iddia sağlamayan) wiki sayfalarına referanslar burada tutulur. `sources` ile karıştırılmaz.

## Adlandırma Kuralları

- `entities/` — `snake_case` isimler: `message_bus.md`, `execution_engine.md`
- `concepts/` — Kısa fikir adı: `event_driven_architecture.md`, `crash_only_design.md`
- `synthesis/` — Açıklayıcı başlık: `backtest_vs_live_parity.md`, `v1_to_v2_migration_lessons.md`
- `tutorials/` — `tutorial_<slug>.md` (resmi öğreticiler için `tutorial_` prefix'i)
- **Bare slug'lar globalde eşsiz olmalı** — wikilink çözümlemesi stem üzerinden yapılıyor. Yeni sayfada mevcut bir stem'i tekrar kullanma.

## Backlinks

- Her `wiki/` sayfasının sonunda otomatik oluşturulan bir bölüm bulunur:

  ```
  <!-- BACKLINKS:BEGIN -->
  ## Referenced by
  - [[strategy_and_actor]]
  - [[order_flow_pipeline]]
  <!-- BACKLINKS:END -->
  ```
- Bu bölüm `tools/wiki_tools.py backlinks` çağrısı ile idempotent şekilde yeniden yazılır. **Elle düzenlemeyin.** Yeni sayfa eklendikten sonra veya wikilink değişiminde bu komutu çalıştırın.

## Temel Operasyonlar

### Ingest (Yeni kaynak ekleme)
1. Kaynağı `sources/NN_slug.md` olarak indir, frontmatter ile.
2. Yeni fikir/varlıkları çıkar → ilgili wiki sayfalarını güncelle veya `tools/wiki_tools.py stub <slug> <kind> "Title"` ile stub oluştur.
3. Her sayfanın `summary`, `sources`, `last_updated` alanlarını yenile.
4. `python tools/wiki_tools.py backlinks && python tools/wiki_tools.py index` çalıştır.
5. Çelişkiler + yeni gap'ler `log.md`'ye append.

### Query (Sorgu)
1. `python tools/wiki_tools.py search "query"` ile kaba sıralama al.
2. `python tools/wiki_tools.py show <slug>` veya `resolve <slug>` ile sayfaya git.
3. Cevapta citation kullan (`(kaynak: sources/02_architecture_docs.md)`).
4. Yeterince değerli sentezleri `synthesis/`'e yeni sayfa olarak dosyala; `summary` ve `sources` doldur; backlinks/index tazele.

### Lint (Periyodik sağlık kontrolü)
- `python tools/wiki_tools.py lint` → konsol raporu (broken_links, orphans, missing_summary, missing_frontmatter, stubs).
- `python tools/wiki_tools.py lint --write --date=2026-07-07` → `lint/2026-07-07_health.md` yazar.
- Karpathy-tarzı LLM health-check (contradictions, stale claims, underlinked terms, next-ingest önerileri) `Workflow` üzerinden çalıştırılır; çıktısı `lint/<date>_health.md`'ye append edilir.

### Stub (Bilinmezi işaretle)
- `python tools/wiki_tools.py stub parquet_data_catalog entity "ParquetDataCatalog"` → boş iskelet.
- Stub sayfaları `status: stub` frontmatter'ıyla üretilir; `index.md`'de `*(stub)*` badge'i ile görünür.

### Frontend
- **Obsidian**: `nautilus_wiki/` klasörünü Obsidian vault olarak aç. Bare-name wikilinks, graph view, backlinks paneli hepsi native çalışır. `newLinkFormat: shortest`, `useMarkdownLinks: false` — vault ayarları buna göre kalıcıdır.
- **Web (FastAPI)**: `/wiki` route'u aynı içeriği HTML olarak sunar. `[[bare]]` wikilinks otomatik `/wiki/wiki/{section}/{slug}.md` URL'lerine dönüşür (bkz. `wiki_helper.py`, `web/routes/wiki.py`).

## Konu Sınırları (Scope)

Bu wiki NautilusTrader'ın kendisi hakkındadır: mimari, bileşenler, API'ler, entegrasyonlar, iş akışları, sürüm-arası farklar. Genel trading teorisi değil, spesifik strateji önerisi değil.

## Sürüm Notu

NautilusTrader v2 release-candidate fazındadır (Temmuz 2026). Sürüme özgü iddialar `v1.x` / `v2.x` etiketleriyle işaretlenmelidir. `sources/` içindeki `retrieved` tarihi 180 günden eskise sayfa `status: stale` olarak `lint` tarafından işaretlenir.
