---
title: nautilus_web_app DeepR Toplu Sertleştirme (2026-08-08)
type: synthesis
summary: 13-boyutlu çoklu-ajan DeepR review'unun ~80 ham bulgusundan 35 konsolide görev — auth eksikliği, chat XSS, sessiz hata yolları, ~90 yeni unit/E2E test; her bulgu koddan doğrulanıp sonra düzeltildi.
key_concepts:
  - crash_only_design
  - single_threaded_core
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/nau_guvenlik_dayaniklilik_duzeltmeleri.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-08
---

# DeepR Toplu Sertleştirme — 2026-08-08

[[nau_guvenlik_dayaniklilik_duzeltmeleri]] 2026-07-25'teki beş paralel incelemenin
bulgularını kapatmıştı; bu sayfa aynı desenin devamı — ancak farklı bir araçla:
kişisel `mDeep`/DeepR Claude Code skill'i, **13 boyutta** (performans, mimari, code,
security, brutal, end-user negatif/pozitif, edge-case, entegrasyon, statik analiz,
sessiz hata & gözlemlenebilirlik, e2e, unit) paralel ajanlarla NAU_v18Jul'u taradı,
her bulguyu adversarial verify ile doğruladı. Rapor: `deepr_report_2026-08-08_0038.md`
(repo kökü). ~80 ham bulgu 35 konsolide göreve indirgendi; **her biri düzeltmeden
önce gerçek kodda doğrulandı**, sonra düzeltildi, sonra test edildi.

Aynı gün öğleden sonra ikinci bir DeepR turu (bu kez Workflow tool ile, 152
ajan) tetiklendi — devamı [[nau_deepr_ikinci_tur_2026_08_08]].

## En kritik 3 bulgu

1. **Auth yok + internete açık + chat XSS.** Uygulama Cloudflare tüneli üzerinden
   internete açılıyordu (`serve.py` — bkz. [[webapp_module_map]] `server.py` satırı)
   ama sıfır kimlik doğrulama vardı. `server.py`'ye `NAU_ACCESS_TOKEN` tabanlı
   tek-operatör paylaşılan-secret middleware eklendi (`_require_auth`, `/login`
   GET/POST, `nau_auth` cookie = token'ın sha256'sı). Aynı geçişte chat balonu
   render zincirinin (`chat_thread.html` ailesi) XSS açığı kapatıldı: Jinja
   `Markup.replace()` kendi replacement argümanını da escape ediyor, bu yüzden
   naif `{{ x | e | replace('\n','<br>') | safe }}` zinciri gerçek `<br>` yerine
   `&lt;br&gt;` üretiyordu — ama saldırgan girdisi `\|e` adımını atlatabilecek bir
   yoldan geçerse (ör. sunucu tarafında zaten HTML üretilen bir alan) kırık zincir
   güvenli görünüp güvenli değildi. Çözüm: `markupsafe.escape()` + düz `str.replace()`
   kullanan özel bir `nl2br` Jinja filtresi.
2. **İki backtest motoru arasında ters işaretli max-drawdown sözleşmesi.** Doğrulandı
   ve raporlandı (kod değişikliği gerektirmedi — sözleşme belgelendi).
3. **`calc_rsi` negatif/sıfır `period`'da sessizce yanlış değer üretiyordu.**
   `indicators.py`'deki tüm `calc_*` fonksiyonlarına (`calc_rsi`, `calc_atr`,
   `calc_adx`, `calc_volume_change`, `calc_nadaraya_watson`) ve `chart_indicators.py`
   `_rsi`/`price_breakout`'a `period<=0`/`lookback<=0` guard'ı eklendi.

## Sessiz hata teması

Bu geçişin en yoğun tekrar eden bulgu sınıfı: geniş `except`'lerin sonucu sessizce
yutması. Düzeltilenler: `token_ledger.record()`/`summary()`, robustness (WFO/MC/OOS)
kayıt yazımı, `Composer.on_bar` MTM snapshot, `sessions._read_events` kısmi okuma,
AUTO forensic `.py` kopya yazımı (`audit_degraded` sinyali eklendi), `agent.py`
LLM client kurulum hatası (`_tag_degraded()`), `parallel_exec` worker stdout/stderr
`None` senaryosu, `download_massive.py` toplu indirmede kısmi hata (artık tek
ticker'ın hatası tüm batch'i düşürmüyor, `failed: dict[str, str]` özetiyle
loglanıyor). Ortak desen: **hata artık ya loglanıyor ya da UI'da görünür bir
degrade bayrağı** oluyor — sessizce "başarılı" görünüp aslında kısmi/bozuk sonuç
döndürmüyor.

## `/lab` cookie bug'ı — en beklenmedik bulgu

`GET /lab` sayfası hiçbir zaman `nautlab_sid` cookie'sini set etmiyordu — yalnızca
`/studio` yapıyordu. `web/routes/lab.py` ve `robustness.py`'ye `/studio`'nunkiyle
aynı server-side session-guard deseni eklendi (`_session_lab_busy`/
`_session_robustness_busy`, çift-gönderim koruması).

## Test tabanı: 849 → 931 geçen

Önceden test kapsamı sıfır olan pure/near-pure fonksiyonlara ~90 yeni unit test:
`agent.py`'nin retry/backoff/JSON-extraction/validate yardımcıları (45 test),
`codegate`'in loop-budget AST injector'ı + `IBFixedFeeModel.get_commission`
(13 test), `custom_block_store.is_valid_name`/`agent_block_identity` (21 test,
AUTO blok rol-çıkarımının tek doğruluk kaynağı), `parallel_exec.get_worker_count()`
CPU/RAM oto-hesap yolu (4 test — önceden yalnız env-override dalı test ediliyordu).
Artı gerçek Bybit ağ çağrısı yapan bir `/backtest/run→progress→result` uçtan-uca
testi (`pytest.mark.e2e`, ~2 dk — varsayılan koşumdan `addopts = "-m \"not e2e\""`
ile hariç, `pytest -m e2e` ile açık çağrılır; aksi halde suite süresi ikiye
katlanıyordu).

Kapsam dışı bırakılan (bilinçli): büyük god-module bölmeleri (`agent.py`,
`web/routes/agent_backtest.py`, `composer.py`) — test kapsamı olmadan riskli,
ayrı bir işe ihtiyaç var.

## İkinci tur küçük not: B904 (2026-08-08)

`raise ... from err`/`from None` — 23 nokta (`strategy_studio/mutations.py` 6,
`compiler.py` 2, `web/routes/strategy_studio.py` 11, `data.py` 1,
`web/routes/wiki.py` 1). Ruff'ın `select` listesinde B (bugbear) yok, yani
bu proje `ruff check .`'ta hiç görünmüyordu — davranış değişikliği yok, salt
okunabilirlik: orijinal exception zincirlendiğinde (`as e` + mesajda `{e}`)
`from e`, mesaj kendi kendine yeterliyken (`KeyError`/`ValueError` gibi
düşük-seviye hatanın metni zaten anlamsız) `from None`.

## Dördüncü tur — sessiz hata yüzeye çıkarma + test hijyeni (2026-08-11)

Aynı desenin devamı; bu turda dört sessiz yol ve üç test borcu kapandı. Ortak
ilke değişmedi: **bozulmayı bilen kod bunu SÖYLEMEK zorunda** — ama bozulma
koşuyu geçersiz kılmıyorsa terminal olmamalı. Referans duruş
`agent_backtest._session_log`: logla + `audit_degraded`/`audit_error` kur,
arayüz sarı bir şeritle göstersin.

* **Strategy Lab log yazımı** (`web/routes/lab.py`). `except Exception: pass`
  yüzünden disk dolu/kilit hatasında panel "✓ N trade · PnL +X" diye yeşil
  bitiyor, koşu `/reports`'a hiç düşmüyor ve tear sheet linki (`log_ts`) boş
  kalıyordu. Yazım `_record_backtest_log()`'a çıkarıldı; hata artık
  `audit_degraded` kuruyor ve hem canlı panelde hem KALICI sonuç ekranında
  görünüyor (sonuç ekranı poll etmiyor — orada gösterilmezse uyarı hiç
  görülmez).
* **Fills raporu → çıkış sebepleri** (`backtest.py`). Rapor alınamayınca her
  trade'in `exit_kind`/`exit_reason` alanı boşalıyor, tablo "strateji hiç SL/TP
  kullanmamış" diye okunuyordu. `_reason_fills_report()` artık `(frame, sebep)`
  döndürüyor; sebep `metrics["exit_reasons_error"]` olarak damgalanıp
  `iteration_row` üzerinden şablonlara taşınıyor — Exit hücresi `—` değil `?`,
  Exit Reason "alınamadı".
* **Blok önizlemesi** (`web/routes/strategy.py`). `evaluate()` çökmesi düz bir
  "sinyal yok" grafiği üretiyordu. `_preview_signals` artık üç ayrı sonuç
  veriyor: `{}` (veri yok), `{"error": ...}` (önizleme hiç koşamadı),
  `{..., eval_error, eval_error_bars}` (grafik çizildi ama N barda istisna).
* **Grafik göstergeleri** (`web/routes/chart.py` + `chart.js`). Hesap patlarsa
  mumlar yine çiziliyor, ama `indicators_error` payload'da taşınıyor ve grafiğin
  altına uyarı şeridi düşüyor — boş katman "bu stratejide gösterge yok" diye
  okunmuyor.

Test tarafı:

* **Depo izolasyonu.** `tests/test_guards_and_indicators.py`'deki üç test
  geliştiricinin GERÇEK `~/.cache/.../custom_blocks` deposunu kullanıyordu (biri
  oraya yazıyor, ikisi içeriğine göre farklı davranıyor) — temiz makinede FAIL,
  dolu makinede keyfi LLM kodunu exec. Depo `tmp_path`'e yönlendirildi, temsilci
  bloklar repo içinde sabitlendi, `assert checked >= 1` kesin sayıya çevrildi ve
  monkeypatch'in düşmesini yakalayan bir çıpa testi eklendi.
* **`run_multi_symbol` skorlaması** (testsizdi). Etiket eşikleri (0.7/0.4 dahil),
  `n_trades >= 5` filtresi, ölçülmemiş alfa (`excess_return_fraction is None`),
  `avg_sharpe`'ın yalnız geçerli sonuçlardan hesaplanması ve "veri yok" ile
  "hepsi patladı" ayrımının bedeli (`_ms_score_factor` → nötr 0.575 vs. asgari
  0.15) pinlendi; peer sepetinin dikiş-farkındalığı da (`_peer_exclusions`)
  aynı dosyada.
* **Clamp kopyası tek kaynağa indi.** `agent._coerce_catalog_blocks`,
  `_validate_composed`'ın clamp mantığının kopyasıydı ve testsizdi. Kopyalar
  gerçekten ıraksamıştı: taslak yolu declared-role denetimini yapmıyordu, yani
  meta'sı `role: "exit"` diyen bir custom block `role="entry"` ile spec'e
  girebiliyor ve çalışma anında composer'ın fail-closed sarmalayıcısı onu her
  barda `None`'a çevirdiği için blok sessizce hiç sinyal üretmiyordu. Ortak
  çekirdek `_coerce_block()` çıkarıldı; parite parametrik testle pinlendi.

## Süreç notu

Toplu iş onaylandıktan ("hepsini yap") sonra bir uygulama-detayı tasarım seçimi
için soru sorulup durulduğunda kullanıcı reddedip yalnızca "devam" dedi — onaylanmış
toplu/bulk çalışma sırasında tasarım-detayı sorularıyla durmamak, makul mühendislik
kararını verip not almak, yalnızca gerçekten engelleyici/kapsam-seviyesi kararlarda
sormak gerektiği anlamına geliyor.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_deepr_dorduncu_tur_2026_08_11]]
- [[nau_deepr_ikinci_tur_2026_08_08]]
- [[strategy_studio]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
