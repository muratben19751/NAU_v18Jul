---
title: Import anında yakalanan referans
type: concept
summary: Modül yüklenirken başka bir global'i değer olarak kopyalayan kod, o global sonradan değişince bayat kalır; monkeypatch/config değişikliği uygulandı sanılır ama koşan hep eski değerdir.
key_concepts:
  - crash_only_design
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-14
---

# Import anında yakalanan referans

Modül düzeyinde kurulan bir nesne, kurulurken başka bir modülün global'ini
**değer olarak** alırsa, o global sonradan değiştiğinde nesne eski referansı
tutmaya devam eder. Tek bir ayar için iki doğruluk kaynağı doğar — ve
genellikle **koşan**, kimsenin bakmadığı bayat olandır. Değişikliği yapan
(test yazarı, operatör) değişikliğin uygulandığını sanır; hata mesajı yoktur,
yalnızca eski davranış sessizce sürer.

## Bu depodaki vakalar

Üçü de aynı devralma turunda yüzeye çıktı: [[nau_devralma_turu_2026_08_14]].

- **`_static_version` takma adı (2026-08-14).** Templating refactor'ı
  `_static_version = static_version` yalın takma adını bırakınca fonksiyon
  `web.templating`'in namespace'inde koşmaya başladı; testlerin
  `monkeypatch.setattr(server, "BASE_DIR", tmp_path)` dikişi görünmez oldu ve
  hash sahte statik ağaca rağmen değişmedi. Çözüm ince sarmalayıcı: gövdesi
  çağrı anında `server.BASE_DIR`'i okuyan gerçek bir `def` — serbest değişken
  her çağrıda `server.__dict__`'ten çözülür, yama görünür.
- **`data.py` bölmesinin giriş şartı.** Test paketi 45 ayrı `data.*` adını
  monkeypatch ediyor. Bir ad alt modüle taşınıp `data.py`'de yeniden dışa
  verilirse yama yalnız kabuktaki bağı değiştirir; değeri okuyan alt modül
  kendi global'ini okur ve testler gerçek `~/.cache` kataloğuna yazmaya
  başlar — süit yeşilken. `nau_data/` bu yüzden sınırı iki yakadan AST ile
  tutar (bkz. [[webapp_module_map]] `nau_data/` satırı).
- **Patch seam'in ölmesi (loop_runner, 2026-08-14).** `_try_log` çağrı-anı
  import'u `web.routes.backtest._log_backtest` takma adından gerçek kaynak
  `web.shared.log_backtest`'e taşınınca testlerin eski yama hedefi ölü bir
  seam'e döndü: yama tutuyor ama o adı artık kimse okumuyor. Seam, değerin
  **okunduğu** ada bağlanmalı, takma ada değil.

## Kural

Yamalanabilir/ayarlanabilir bir değer, onu kullanan koddan **çağrı anında**
ve **sahibi olan modülün adıyla** okunmalı (`data.CACHE_DIR`, `server.BASE_DIR`);
import anında yerel bir kopyaya bağlanmamalı. Bunu yoruma değil teste bağla:
NAU'da `test_data_module_surface_is_stable.py` yamalanan fonksiyonların çağrı
yerlerinin `data.py`'de kaldığını, `test_data_layer_is_stateless.py` adların
alt pakete sızmadığını AST ile denetler.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_devralma_turu_2026_08_14]]
- [[webapp_module_map]]
- [[x_watch_izleyici]]
<!-- BACKLINKS:END -->
