---
title: Devralma turu — kopan oturumun işi tamamlandı (2026-08-14)
type: synthesis
summary: Haftalık limitte kopan oturumun yarım işi transcript'ten devralındı: nau_data sınırı iki yakalı teste bağlandı, 7 kırık test 4 kümede tanı→onar→doğrula ile kapatıldı, süit 2190 yeşil; sunucu pm2 altına alındı.
key_concepts:
  - crash_only_design
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/concepts/import_aninda_yakalanan_referans.md
last_updated: 2026-08-14
---

# Devralma turu — 2026-08-14

13-14 Ağustos oturumu DeepR kampanyasının ortasında haftalık limitte koptu;
arayüz ~3 saat "çalışıyor" spinner'ı gösterdi ama motor ölmüştü. İş
transcript'ten (son todo listesi + son asistan niyeti + son tool sonucu)
devralındı. Kopuşun en öğretici izi: `nau_data/__init__.py` docstring'i
**var olmayan** bir koruma testine atıf yapıyordu — vaat ile kod, kopuş
anında ayrışmıştı.

## Tamamlananlar

1. **`nau_data` sınırı iki yakalı** — `test_data_layer_is_stateless.py`
   yazıldı (paket yakası: `data` import yasağı + yamalanan 45 adın AST
   yasağı); kasıtlı ihlal sondasıyla ısırdığı kanıtlandı. Gerekçe:
   [[import_aninda_yakalanan_referans]].
2. **Düz yerleşim gölgeleme koruması** —
   `test_root_modules_do_not_shadow_dependencies.py`: 43 kök ad × 147 kurulu
   tepe ad tarandı (bugün çakışma yok); jenerik adlar (`data`, `state`,
   `agent`…) ileride bir bağımlılığı gölgelerse süit dağıtım adıyla kırılır.
   Asıl paketleme taşıması bilinçli ertelenmiş durumda ("en riskli, en düşük
   getirili").
3. **7 kırık test, 4 küme** — 12 ajanlık tanı→onar→şüpheci-doğrulama hattı;
   dördü de `sound`. İkisi kod düzeltmesi (`_static_version` çağrı-anı
   dikişi; templating refactor'ının kaybettiği monkeypatch seam'i geri
   geldi), ikisi kanıtlı test güncellemesi (loop_runner seam'i gerçek
   kaynağa taşındı; promote-atomicity testinin yarışı `threading.Event` ile
   deterministik yapıldı, +2 sıkılaştırıcı assertion).
4. **Süit + hijyen** — 2190 passed / 0 failed; `ruff check .` ve
   `ruff format --check .` exit 0 (`.tmp` ruff dışlamasına, `.tmp*`/`work/`
   gitignore'a alındı).
5. **Ops** — sunucu pm2 altında (`nau-web`, `ecosystem.config.js`);
   makinedeki mevcut `pm2-resurrect` logon görevi + `pm2 save` zinciri
   uçtan uca doğrulandı (pm2 kill → görev tetikle → nau-web geri geldi).

## Dersler

- **Spinner kabuğun malı, motorun değil** — canlılık kanıtı transcript
  mtime'ı + süreç listesidir.
- **Kopan oturumun "koşuyor" sanılan son komutu çoğu kez bitmiştir** —
  yalnız raporlanmamıştır; transcript'teki son tool sonucu okunmadan iş
  tekrarlanmamalı.
- **Vaat/kod ayrışması** devralmanın ilk arama hedefidir: docstring'in
  anlattığı ama diskte olmayan dosyalar.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[import_aninda_yakalanan_referans]]
<!-- BACKLINKS:END -->
