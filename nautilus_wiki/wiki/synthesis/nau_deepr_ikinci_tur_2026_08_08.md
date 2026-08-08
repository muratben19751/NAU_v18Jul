---
title: nautilus_web_app DeepR İkinci Tur (2026-08-08, öğleden sonra)
type: synthesis
summary: Aynı gün ikinci bir DeepR koşusu — 152 ajan, 0 hata, 33 doğrulanmış bulgu; 31'i aynı oturumda düzeltildi, #47 (_agent_worker bölme) ayrı oturumda kademeli olarak A0-A9 adımlarıyla sürüyor (A11 en riskli adım, henüz yapılmadı), #59 sırada.
key_concepts:
  - crash_only_design
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/nau_deepr_toplu_sertlestirme_2026_08.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/strategy_studio.md
  - wiki/synthesis/nau_performans_denetimi.md
  - wiki/synthesis/kesilme_ve_degrade_gorunurlugu.md
  - wiki/synthesis/auto_360_canli_review_iyilestirmeleri.md
last_updated: 2026-08-08
---

# DeepR İkinci Tur — 2026-08-08 (öğleden sonra)

[[nau_deepr_toplu_sertlestirme_2026_08]] sabah 35 görevi kapatmıştı (rapor
`0038`). Aynı gün öğleden sonra kullanıcı `/mDeep`'i tekrar (argümansız, tam
proje) tetikledi — bu kez **Workflow tool ile**, ikinci beyin vault'undaki
`deepr_skill` sayfasının belgelediği additional-working-directory sızıntısı
düzeltilmiş haliyle: 152 ajan, **0 kapsam sızıntısı**, 0 hata. Rapor:
`deepr_report_2026-08-08_1204.md`. 33 doğrulanmış bulgu 33 göreve (#45–#77)
indirgendi, sırayla ("hepsini yap" onayının devamı) ele alındı.

## En kritik 3 bulgu ve düzeltmeleri

1. **Strategy Studio varsayılan olarak tamamen uydurma backtest sonuçları
   veriyordu, kullanıcı hiçbir uyarı görmüyordu** (`StubBacktestAdapter` —
   bkz. [[strategy_studio]] "SİMÜLE rozeti" bölümü). Düzeltme:
   `web/routes/strategy_studio.py` `_ctx()`'e `engine_is_stub` bayrağı;
   footer + sonuç panelinde her zaman görünen amber "⚠ SİMÜLE" rozeti.
2. **AUTO'nun robustness kapısı eksik bir alfa alanı yüzünden temiz adayları
   da reddediyordu — 2 regresyon testi FAIL durumundaydı.** Kök neden test
   fixture'ının bir önceki (2026-08-07) sertleştirmeyle (a6ddb5a) senkronsuz
   kalmasıydı, kapı mantığı değişmedi — `_clean()` fixture'ı yeni sözleşmeye
   (excess_return_fraction + max_dd_p95) güncellendi.
3. **Otonom loop (`/loop/start`) piyasa verisini asla yenilemiyordu** —
   `server._context["bars"]` yalnız `lifespan()`'da bir kez doluyordu.
   `loop_runner.run_loop` artık her iterasyonda `load_bybit_bars` çağırıyor;
   fetch başarısız olursa önceki bars korunup loglanıyor, döngü çökmüyor.

## Tema: "önceki sertleştirme testle senkronsuz kaldı"

En kritik 3'ün 2.'si aslında yeni bir bug değil — 2026-08-07'deki bilinçli
bir sertleştirmenin (a6ddb5a) test fixture'ını güncellemeyi unutmasıydı.
Ders: production mantığını sıkılaştıran bir commit, o mantığı sınayan
regresyon testlerini de AYNI commit'te güncellemeli; aksi halde test suite'i
CI kırmızı çıkana kadar sessizce referans değerini kaybediyor.

## #47 — `_agent_worker` bölme: kademeli, test-önce çıkarım (2026-08-08, devam ediyor)

#47 ve #59 önce "şimdilik atla, ayrı dikkatli oturumda ele alınacak" onayıyla
kapsam dışı bırakılmıştı; kullanıcı sonra "ona devam et → ikisi birden,
sırayla" dedi. Plan mode ile 2 Explore + 2 Plan ajanı kod haritası çıkardı;
onaylanan strateji: **aynı dosyada** (yeni bir alt-paket DEĞİL —
`tests/test_lock_nesting.py`'nin AST taraması sabit olarak yalnız
`agent_backtest.py`'yi hedefliyor, taşınan kilit-dokunan kod bu tek
regresyon testinin kör noktasına düşerdi) düz modül fonksiyonlarına, her
adım kendi commit'i + kendi testiyle, "önce test yaz, davranış korunsun"
disipliniyle.

Tamamlanan adımlar (A0-A9): `_market_for`/`_iv_for`/`_recipe`, `_WorkerState`
dataclass (round-persistent durum), `_cleanup_generated`/`_winless_bump`/
`_winless_stop`, `_make_llm_control` (bütçe/iptal kapısı — önceden SIFIR test
kapsamı), `_rank_and_filter`, `_propose_initial_strategy`, `_scan_one_candidate`
(effective-score işaret-güvenliği mutasyon testiyle doğrulandı),
`_run_promotion_gate` (sealed-holdout finansal bütünlük kapısı — planın en
dikkatli incelenen adımı, sadece yeşil testle değil elle diff okuyarak da
gözden geçirildi), `_run_backtest_iteration`.

**Aynı hata sınıfı 4 kez tekrarlandı ve her seferinde yakalandı:** ruff'ın
otomatik-fix hook'u, bir import'u kullanan kod satırı henüz eklenmeden önceki
ara kayıt anında "kullanılmıyor" sanıp sessizce siliyor (`dataclasses`,
`Callable`, `web.shared.log_robustness`, `web.shared.log_backtest`) — her
adımdan sonra tam test suite'i koşturma disiplini olmasa fark edilmezdi. Ayrı
bir gerçek davranış hatası da yakalandı: closure→fabrika çıkarımında değerin
erken bağlanması (bkz. ikinci beyin: `closure_fabrika_erken_baglama`).

**Kapsam dışı (bu turda bilinçli ertelendi):**
- **A10 — Faz 2'nin "sıradaki spec'i üret" bloğu**: `spec`'in aynı loop
  değişkenine yeniden atanması + 3 katmanlı exception hiyerarşisi (iptal/
  bütçe re-raise, generic'te önceki spec'le devam) — yanlış çıkarım ya
  iptal/bütçeyi sessizce yutar ya geçici bir hata koşumu bitirir.
- **A11a/A11b — Faz 0 veri/holdout yükleyici** (`_load_tf`/`_load_tf_uncached`):
  planın **en riskli adımı**, bilinçli olarak en sona bırakıldı — henüz
  yapılmadı.
- **#59 — `backtest.py` route'undan domain mantığını ayırma**: #47 bitince
  sırada.
- **#69 (`download_grouped_daily.py`), #60/#61 (`repair_massive_intraday.py`
  — bkz. finding'in kendi metni)**: bu dosyalar untracked (`git status: ??`)
  — kullanıcının bu projeyle ilgisiz kendi ayrı script'leri, önceki 35-görevlik
  turda da aynı gerekçeyle commit dışı bırakılmıştı.

## Test tabanı büyümesi

931 (35-görevlik ilk turun sonu) → bu ikinci turda **~15 yeni test dosyası**,
150+ yeni/genişletilmiş test. Öne çıkanlar:
- `test_wiki_helper_and_route.py`: path-traversal guard'ının **gerçekten**
  yakaladığını mutasyon testiyle (guard'ı geçici kaldır → test kırılsın →
  geri koy → test geçsin) doğruladı — ayrıca httpx'in `..` segmentini
  istemci tarafında normalize ettiğini (TestClient üzerinden traversal test
  etmenin yanıltıcı olduğunu) keşfetti, route fonksiyonunu doğrudan
  çağırarak düzeltti.
- `test_login_rate_limit.py` / `test_auth_cookie_logout.py` /
  `test_require_auth_middleware.py`: auth cookie `secure=True` olunca
  httpx'in Secure çerezi `http://testserver` üzerinden geri göndermediğini
  keşfetti (`base_url="https://testserver"` fixture'ı gerekti) — aksi halde
  birçok "authenticated" assertion sessizce yanlış nedenle geçerdi.
- `test_agent_run_form.py::test_learned_ceiling_survives_a_concurrent_smaller_write`:
  gerçek `threading.Barrier` ile iki thread'i aynı okuma noktasında
  senkronize edip, `max()` guard'ının kaldırıldığı mutasyonda testin
  gerçekten kırıldığını doğruladı.
- `test_backtest_run_worker_logging.py`: `/backtest/run`'ın arka plan
  worker'ındaki log/snapshot-yazma except'lerinin artık loglandığını,
  `sandbox.run_backtest_guarded`'ı kaynağında mock'layıp (worker'ın kendi
  `from sandbox import ...` yerel import'u yüzünden) gerçek engine/subprocess
  hiç çalıştırmadan doğruladı.

Genel desen: bu turun testlerinin çoğu **önce yanlış varsayımla yazıldı,
çalıştırılıp gerçek davranışla karşılaştırıldı, sonra düzeltildi** —
`_pick_best_exit_from_history`'nin "en yüksek skor" değil "skora göre
sıralı satır satır ilk eşleşme" mantığı, `calc_nadaraya_watson`'ın pencere
kesmesi, `_deduplicate_candidate`'ın family-fingerprint'in parametreleri
yok sayması gibi. Kod her seferinde doğru kaynak kabul edildi, test ona göre
düzeltildi — tam tersi değil.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_deepr_toplu_sertlestirme_2026_08]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
