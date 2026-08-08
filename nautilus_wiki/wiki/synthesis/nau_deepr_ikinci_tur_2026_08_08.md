---
title: nautilus_web_app DeepR İkinci Tur (2026-08-08, öğleden sonra)
type: synthesis
summary: Aynı gün ikinci bir DeepR koşusu — 152 ajan, 0 hata, 33 doğrulanmış bulgu (deepr_report_2026-08-08_1204.md); 31'i düzeltildi/test edildi, 2'si (_agent_worker bölme, backtest.py domain-mantığı çıkarma) bilinçli olarak ayrı bir oturuma bırakıldı.
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

## Kapsam dışı bırakılanlar (bilinçli, kullanıcı onaylı)

- **#47 — `_agent_worker` bölme** (`web/routes/agent_backtest.py`, ~1750
  satır, 20 parametre, AUTO'nun canlı continuous-mode worker'ı) ve
  **#59 — `backtest.py` route'undan domain mantığını ayırma** (aynı desenin
  küçük ölçekli tekrarı, `web/routes/backtest.py`): her ikisi de büyük
  mimari çıkarma + gerçek regresyon riski taşıyor, DeepR'ın kendi raporunda
  bile "test kapsamı olmadan riskli" diye 35-görevlik ilk turda kapsam dışı
  bırakılmıştı. Kullanıcıya soruldu, "şimdilik atla, ayrı dikkatli oturumda
  ele alınacak" onayı alındı.
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
<!-- BACKLINKS:END -->
