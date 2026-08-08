---
title: NAU Test Araçları Kararı
type: synthesis
summary: NAU için kapsamlı code-to-performance testte Claude Code'un built-in skill'leri yerine harici araç seçildi: Semgrep (SAST) kuruldu, ilk tarama 10 bulgu verdi (1 gerçek: CDN script'lerinde SRI eksik).
status: draft
key_concepts:
  - semgrep
  - static_analysis
sources:
  - https://semgrep.dev
related:
  - nau_wiki/wiki/synthesis/nau_guvenlik_dayaniklilik_duzeltmeleri.md
  - nau_wiki/wiki/synthesis/nau_performans_denetimi.md
last_updated: 2026-08-07
---

## Karar

NAU (Nautilus web app, FastAPI + HTMX backtesting lab) için kapsamlı test/tarama
ihtiyacı doğunca, Claude Code'un built-in skill'leri (`code-review`,
`security-review` vb.) yerine **açıkça harici** bir araç istendi — proje
Claude Code'a bağımlı olmayan, bağımsız çalışan bir tarama katmanı istiyor.

**Seçim: [[semgrep]]** (SAST) — Python/FastAPI için hazır kural setleri var,
`semgrep --config=auto` ile CI'dan/CLI'dan bağımsız çalışır, hem correctness
hem güvenlik bulur.

Kapsamı "code'dan performansa" genişletmek için önerilen tamamlayıcılar
(henüz kurulmadı, ihtiyaç oldukça değerlendirilecek):
- **SonarQube/SonarCloud** — kod kalitesi metrikleri, karmaşıklık, duplication.
- **Locust** — FastAPI endpoint'lerinde yük/performans testi.
- **py-spy / scalene** — backtest motorunun profiling'i (statik analiz yavaş
  noktayı göstermez, profiling gösterir).

## Neden

Built-in Claude Code skill'leri (`code-review`, `security-review`) Claude
Code oturumuna bağımlı ve billed/tetikli (`/code-review ultra`); NAU için
tekrarlanabilir, CI'a bağlanabilir, araç-bağımsız bir tarama isteniyor —
bu yüzden Semgrep gibi bağımsız bir CLI tercih edildi.

## İlk tarama (2026-08-07)

`pip install semgrep` (v1.172.0) sonrası `semgrep --config=auto` ile
`studio_app strategy_studio web scripts tests` üzerinde 456 kural / 122
dosya tarandı (Windows'ta JSON çıktı yazarken cp1254 codec hatası
çıkıyor — `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` ile çözüldü).

10 bulgudan sadece **1 tanesi gerçek aksiyon gerektiriyor**:
`web/templates/base.html` — htmx.org ve jsdelivr CDN script'lerinde SRI
(`integrity`) hash'i yok.

Geri kalanı incelenip elenmiş:
- `web/routes/strategy.py:240` `exec()` — bilinçli sandbox (safe_builtins,
  read-only module proxy, loop-budget guard), aksiyon gerekmiyor.
- `strategy_studio/store.py:171,173,554` "SQL injection" uyarıları — f-string
  ile kurulan SQL'in içindeki değerler ya sabit iç sabitlerden (`_ADDED_COLUMNS`)
  ya da zaten `?` parametreli; yanlış pozitif.
- `scripts/check_auto_cockpit.py:53` `urllib` http uyarısı — hedef sabit
  localhost, yerel test scripti; yanlış pozitif.

**Ders:** Semgrep `--config=auto`'nun ham bulgu sayısı yanıltıcı — 10
bulgudan 9'u context okunca değersiz çıktı. Gelecekte NAU üzerinde çalıştırırken
doğrudan sayıya değil, her bulguyu koda bakıp triaj ettikten sonraki sonuca
güven.

Semgrep'e ek olarak, çok-boyutlu ajan tabanlı taramada `mDeep`/DeepR skill'i
kullanılıyor (2026-08-07'de gözden geçirilip 7 değişiklik onaylandı — ayrıntı
global ikinci beyinde `wiki/entities/deepr_skill.md`, bu proje-özel wiki'nin
dışında).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[semgrep]]
<!-- BACKLINKS:END -->
