# Wiki Log

Append-only. Her ingest, query veya lint operasyonu bir satır bırakır.

## 2026-08-09 (9) — agent.py decomposition Faz 3 kapandı: dispatch çekirdeği → llm_dispatch.py

- **commit** — Adım 8 (`ee9aacc`, test-önce: `_ledger_record`'un 4 karakterizasyon
  testi + `test_llm_observer_sees_each_actual_provider_response`'un gerçek
  deftere yazma hatasının düzeltilmesi) + Adım 9 (`8c5c5d9`, taşımanın kendisi)
  — `agent.py` 2824→~2470 satıra indi, yeni `llm_dispatch.py` (443 satır)
  dispatch çekirdeğinin (istemci kurulumu/seçimi, kredi-tükenme fallback'i,
  `TruncatedResponse`+öğrenilen-tavan retry, `_create_message`/
  `_create_message_once`) tek evi oldu. `openrouter_backend.py`'nin (Adım 6)
  kanıtlanmış deseniyle birebir aynı şekil: `_client`/`_client_lock` ve
  `_ledger_record` KASITLI re-export edilmedi.
- **doğrulama** — bir Workflow (test-önce ajanı → taşıma ajanı → 4 paralel
  çekişmeli doğrulama ajanı) implementasyonu yaptı; ana oturum kendi başına
  BAĞIMSIZ tekrar doğruladı: `_create_message_once`'un eski/yeni gövdesi
  programatik olarak byte-for-byte identik bulundu, 7 isim için identity-check
  (`agent.X is llm_dispatch.X`) True, monkeypatch-kayması taraması (11 aday
  isim) temiz, tam suite 2 kez bağımsız koşuldu (1414 geçti, 1 skip, 0 fail —
  ikisi de aynı sayı). Workflow'un flagledigi yeni bir gevşek test
  (`test_fallback_proposal_carries_a_machine_readable_degraded_marker`, ~%25
  flake) hem taşıma-sonrası hem `git stash` ile geri alınmış taşıma-öncesi
  durumda AYNI oranda başarısız bulundu — taşımadan bağımsız, önceden var olan
  bir sorun (unseeded `random`, Domain C'nin `_fallback_composed`'ında),
  düzeltilmedi.
- **commit-bölme tekniği** — `test_llm_backend_selection.py` her iki adımda da
  değişti (Adım 8 yeni `TestLedgerRecord` sınıfını ekledi, Adım 9 onu da dahil
  tüm dosyayı `llm_dispatch`'e repoint etti); iki temiz commit için ara durumu
  (Adım 8 bitmiş, Adım 9 başlamamış) `git stash` ile agent.py/llm_dispatch.py/
  diğer 2 test dosyasını geçici olarak HEAD'e döndürüp o ara durumun kendi
  başına yeşil olduğu doğrulanarak commit edildi, sonra stash geri alınıp
  Adım 9 commit'lendi.
- **wiki** — `webapp_module_map.md`'nin `agent.py` satırı (Faz 3 tamamlandı,
  satır sayısı ~2470) + yeni `llm_dispatch.py` satırı eklendi.

## 2026-08-09 (8) — wiki-sync (bağlam eşiği %53) — bir bayat referans bulundu, düzeltildi

- **rapor** — otomatik wiki-sync eşiği tetiklendi; son commit (8e32105,
  Adım 10) zaten wiki'yi kendi içinde güncellemişti, bu geçiş doğrulama
  amaçlı.
- **bulgu** — `webapp_module_map.md`'nin `agent_backtest.py` satırı hâlâ
  "Faz 2'nin lookahead-generation bloğu... bilinçli olarak plan dışı"
  diyordu — (7)'de tamamlanmıştı, satır bayattı. Düzeltildi + `_propose_next_strategy`/`degraded_terminal`
  notu eklendi, `last_updated` tazelendi.
- **doğrulama** — tüm wiki'de "Adım 10"/"Faz 2 lookahead" geçen 3 sayfa
  tarandı (webapp_module_map + iki DeepR sayfası) — üçü de artık tutarlı.
  Lint temiz (0 broken_links/orphans/missing_summary/stale/stubs).

## 2026-08-09 (7) — main'e merge, branch silindi; DeepR #47'nin son adımı (A10) kapandı

- **merge** — `fix/auto-gate-and-artifact-identity` (58 commit, 37 DeepR
  bulgusu) main'e fast-forward merge edildi (1312 test yeşil), branch
  local+origin'den silindi.
- **keşif** — "başka bir düzeltme var mı" sorusu üzerine agent_backtest.py'nin
  docstring'i taranırken bir yanlışlık bulundu: Faz 0 veri/holdout
  yükleyicisinin (DeepR #47'nin A11a/A11b, planın en riskli adımı) hâlâ
  ertelendiğini söylüyordu — kod ve testler zaten tamamlandığını gösteriyordu
  (`_load_timeframe_bars`+`_TfLoader` mevcut, `TestContinuousCircuitBreaker`
  yeşil). Docstring düzeltildi (davranış değişmedi).
- **fix (kullanıcı onayıyla)** — #47'nin GERÇEKTEN tek açık kalemi olan Adım
  10 (Faz 2 lookahead-generation bloğu) `_propose_next_strategy` olarak
  çıkarıldı — literal taşıma, davranış korundu. Test yazımı iki ince
  pre-existing (değiştirilmeyen) davranışı ortaya çıkardı:
  `degraded_terminal` Faz-1'in aksine burada propagate olmuyor (önceki
  spec'e düşülebildiği için genel except'e düşüp yutuluyor); ve o alt-durumda
  `spec` raise'den önce zaten yeni teklife atanmış oluyor. 11 yeni test, 2'si
  mutasyon testiyle doğrulandı (cancel/bütçe re-raise'i geçici kaldırıp
  testlerin kırıldığı, geri alınca yeşile döndüğü kanıtlandı). Tam suite:
  1323 yeşil. main'e doğrudan commit+push edildi (repo'nun alışılmış akışı).
- **wiki** — [[nau_deepr_ikinci_tur_2026_08_08]] ve
  [[nau_deepr_ucuncu_tur_2026_08_09]] çapraz-linkli eklerle güncellendi.

## 2026-08-09 (6) — DeepR üçüncü tur kapandı: 37/37 bulgu karara bağlandı

- **rapor** — (5)'te başlayan "hepsini yap" geçişinin devamı: kalan 18 ORTA
  (#99-116), 10 DÜŞÜK (#117-126), 1 BİLGİ (#127) tek tek aynı disiplinle
  (fix → test → davranışsal olanlarda mutasyon testi → ruff → commit →
  periyodik push) tamamlandı.
- **iki bulgu ayrı iş gerektirmedi** — rapordaki 19. ORTA
  (`repair_massive_intraday.py` sıfır test kapsamı) YÜKSEK #3'ün (TF-only
  fix) test dosyası + sonraki ORTA düzeltmeleriyle turun sonunda zaten
  fiilen karşılanmış bulundu; 2. BİLGİ ("ruff tüm proje genelinde temiz")
  zaten saf olumlu doğrulamaydı, düzeltilecek bir şey yoktu. İkisi de
  doğrulandı, ayrı commit gerekmedi.
- **bilinçli non-fix kararları (2)** — `agent_backtest.py` boyutu: DeepR
  #47 planı (2026-08-08) bu turdan önce büyük ölçüde tamamlanmış (~40
  yardımcı çıkarılmış, 53 test yeşil) bulundu, dosya BİLİNÇLİ büyük
  kalıyor (`test_lock_nesting.py`'nin AST hedefi); kod değişmedi, docstring
  güncellendi. Composer'ın "💬 AI ile düzenle" butonları: Türkçe bir
  LLM-chat özelliğinin giriş noktası, yalnız butonu İngilizce'ye çevirmek
  özelliği DAHA tutarsız yapardı — dokunulmadı, `catalog_list.html`'in
  ilgisiz 3 stray tooltip'i çevrildi.
- **untracked script'ler** — `repair_massive_intraday.py` (+ testi) ve
  `download_grouped_daily.py` (+ testi) kullanıcının kendi scriptleri;
  diskte düzeltildi + test edildi, hiçbiri commit edilmedi (proje
  başından beri süregelen kapsam kararı).
- **wiki** — `nau_deepr_ucuncu_tur_2026_08_09.md` frontmatter özeti ve
  ilerleme listesi 37/37 tamamlanmış hâle güncellendi.

## 2026-08-09 (5) — Kullanıcı "hepsini yap" dedi: kalan 31 ORTA/DÜŞÜK/BİLGİ, sırayla (kod → wiki)

- **rapor** — 6/6 YÜKSEK bulgu kapandıktan sonra kullanıcı ORTA listesini
  istedi, sonra "hepsini yap" dedi — kalan 31 bulgu tek tek, aynı
  disiplinle (fix → test → mutasyon testi gereken yerde → commit → push)
  ele alınıyor.
- **fix (bu geçiş)** — NAU_ACCESS_TOKEN fail-open, atr_period/commission_pct
  server-side doğrulama, codegate LShift DoS, _terminal_message run_id
  doğrulaması, _index_rows TTL cache, _env_float/_env_int konsolidasyonu,
  backtest.py'de ~467 satır ölü kod silindi.
- **beklenmedik bulgu** — ölü kod silinirken `webapp_module_map.md`'nin
  "Uçtan uca akış" bölümünün, kaldırılmış bir mimariyi (otomatik
  BacktestNode/BacktestEngine motor seçimi) hâlâ AKTİF diye belgelediği
  ortaya çıktı — `sandbox.run_backtest_guarded` bu iki fonksiyonu repo
  genelinde sıfır yerde çağırıyordu, wiki muhtemelen sandbox.py'nin
  tek-yol mimarisine geçişte hiç güncellenmemiş. Akış bölümü düzeltildi;
  [[backtest_node]] (Nautilus'un kendi API'si, hâlâ geçerli) "webapp'te
  ölçüldü" notlarına netleştirme eklendi.
- **wiki** — `nau_deepr_ucuncu_tur_2026_08_09.md`'ye kompakt ilerleme
  listesi eklendi (kalan öğeler için tek satır, yalnız gerçek sürprizler
  detaylandırılıyor — sayfa boyutu kontrol altında tutuluyor).

## 2026-08-09 (4) — Route modülleri private-state erişimi kapandı; DeepR üçüncü turun 6/6 YÜKSEK'i tamam (kod → wiki, wiki-sync skill)

- **rapor** — Bağlam kullanımı otomatik wiki-sync eşiğini aştı; bu geçiş
  önce yarım kalan kodu bitirip (kritik 3'ün sonuncusu) tutarlı bir noktaya
  getirdi, sonra wiki-sync skill'i tetiklendi.
- **fix (kod)** — `studio.py`'nin `backtest.py`/`strategy.py`'den 6
  `_`-önekli fonksiyon içe aktarması alt çizgisiz yapıldı (`session_drafts`
  — bare `drafts` değil, yerel değişken gölgelemesi riski yüzünden).
  `agent_backtest.py`'de yeni `newest_active_run_id()`: `studio.py`'nin VE
  bu modülün kendi `page()`'inin aynı ham `_AGENT_LOCK` taramasını tek
  fonksiyona indirdi — `studio.py` artık kilide hiç dokunmuyor
  (2026-07-14 deadlock geçmişi olan bir kilit). `SESSION_LOG_DIR`
  `web/shared.py`'ye taşındı, tek sahip oldu.
- **test-önce disiplin** — Refactor'dan ÖNCE `tests/test_studio_page.py`
  yazıldı (`GET /studio` daha önce hiç test edilmiyordu). Tam suite iki kez
  koşuldu: ilk koşum yeni testlerin kendi kusurunu yakaladı
  (`_AGENT_PROGRESS` process-global paylaşılan durum, iki test yanlışlıkla
  boş başladığını varsaymıştı) — snapshot/clear/restore fixture'ıyla
  düzeltildi. İkinci koşum: 1170 geçti, 0 kaldı. Lock-touching kısım
  mutasyon testiyle de doğrulandı.
- **sonuç** — DeepR üçüncü turun kullanıcının seçtiği 6/6 YÜKSEK bulgusu
  tamamlandı (composer, AUTO E2E, repair script, delete_custom_batch
  testi, CI e2e retry, route-modülü private-state erişimi). 31
  ORTA/DÜŞÜK/BİLGİ henüz ele alınmadı.
- **wiki** — `synthesis/nau_deepr_ucuncu_tur_2026_08_09.md`'ye madde 3 +
  süreç notu eklendi, `summary` "6/6" olarak güncellendi.
  `webapp_module_map.md` `agent_backtest.py` satırına `newest_active_run_id`
  notu eklendi. Backlinks + index + lint yenilendi (0 broken_links/
  orphans/stale/stubs).

## 2026-08-09 (3) — E2E CI adımı: continue-on-error yerine retry (kod → wiki)

- **rapor** — DeepR'ın kalan listesindeki 2. YÜKSEK madde: tek gerçek E2E
  testi (`test_backtest_run_progress_result_e2e.py`) CI'da
  `continue-on-error: true` — gerçek bir regresyon olsa bile PR yine merge
  edilebiliyordu. Bu ayar 2026-08-08'de bilinçli eklenmişti (canlı Bybit ağ
  hatası pipeline'ı bloklamasın diye); DeepR aynı ödünleşimi ikinci kez
  sorun olarak işaretledi.
- **karar** — CI/CD pipeline değişikliği olduğu için (kod değişikliğinden
  farklı blast radius) kullanıcıya 3 seçenek soruldu; "retry (3x) +
  continue-on-error kaldır" seçildi.
- **fix** — `.github/workflows/ci.yml`: adım artık PowerShell döngüsüyle en
  fazla 3 kez deniyor, biri geçerse yeşil, üçü de düşerse adım gerçekten
  başarısız olup PR'ı bloklar. Yeni bağımlılık yok (düz `pwsh` döngüsü,
  üçüncü parti retry action değil). Mantık gerçek CI koşumu olmadan
  PowerShell tool'uyla simüle edilip doğrulandı.
- **wiki** — `synthesis/nau_deepr_ucuncu_tur_2026_08_09.md`'ye madde 2
  eklendi, `summary` tazelendi. Backlinks + index + lint yenilendi.

## 2026-08-09 (2) — DeepR üçüncü tur: kalan bulgular, sırayla (kod → wiki)

- **rapor** — Kullanıcı kalan 34 bulgunun tam listesini istedi, sonra "devam"
  dedi. Kalan 3 YÜKSEK bulgudan en düşük riskliyle başlandı.
- **fix** — `delete_custom_batch` hiçbir testte yoktu (kardeşi
  `save_custom_batch` rollback dahil test edilmişti). 5 yeni doğrudan test
  `tests/test_auto_360_fixes.py`'ye eklendi; rollback testi mutasyon
  testiyle doğrulandı (`_registry_transaction`'ın except bloğu geçici devre
  dışı bırakılınca hem yeni test hem kardeşinin var olan testi kırıldı —
  ortak mekanizmayı paylaştıklarının kanıtı). Production kod değişmedi.
- **wiki** — `custom_block_store.py` Wiki References'a
  `[[nau_deepr_ucuncu_tur_2026_08_09]]` eklendi; o sayfa ilerlemeyi
  yansıtacak şekilde güncellendi. Backlinks + index + lint yenilendi.

## 2026-08-09 — DeepR üçüncü tur: kritik 3 kapandı (kod → wiki)

- **rapor** — Kullanıcı `/mDeep`'i argümansız tekrar tetikledi; ilk koşum (203
  ajan) `nautilus_wiki/`'ye kapsam-sızdı (yeni Workflow sızıntı varyantı —
  envanterde dışlama amacıyla anılan bir alt-dizin paradoksal biçimde dikkat
  çekti), "NAU app'e odaklayarak tekrar tetikle" ile düzeltilip 164 ajanla
  yeniden koşuldu → 37 bulgu, 0 sızıntı. Kullanıcı: "kritik 3 maddeyi
  düzeltmeye başla".
- **fix 1** — `composer._load_custom_blocks()`: `custom_block_store.list_custom()`
  import-zamanı `RegistryUnavailable`/herhangi bir istisna fırlatırsa artık
  `import composer`'ı düşürmüyor, uyarı loglayıp devam ediyor. 3 yeni test,
  mutasyon testiyle doğrulandı.
- **fix 2** — `tests/test_agent_worker_e2e.py` (yeni dosya): `_agent_worker`'ı
  yalnız 4 dış sınırı (`load_bybit_bars`/`propose_composed_strategy`/
  `run_backtest_guarded`/`run_robustness_guarded`) mock'layıp gerçek Faz
  0→1→2→3→4→5 boyunca bir winner/promotion'a ve ayrı bir promotion-red'ine
  kadar sürüyor — sealed-holdout kapısının ilk uçtan-uca kapsamı. Red testi
  `_holdout_promotion_verdict`'i geçici olarak her zaman `True` dönecek
  şekilde bozup mutasyon testiyle doğrulandı.
- **fix 3 (untracked, commit dışı)** — `repair_massive_intraday.py`: 1-MINUTE
  kaynağı da düzeltiyor artık (önceden yalnız türetilmiş TF'yi düzeltiyordu,
  `ingest_equities.build_tf_bars()`'ın bir sonraki rutin koşusu sessizce geri
  alıyordu); ayrıca gerçek bir pyarrow `ChunkedArray | ChunkedArray` ve bir
  pandas 3.0.3 ns-çözünürlük bug'ı bulundu/düzeltildi. Kullanıcının kendi
  script'i — diskte düzeltildi, testi eklendi, git'e hiç girmedi.
- **doğrulama** — Tam suite: 1160 geçti / 1 atlandı / 1 deselected, 1 flake
  (`test_promote_draft_atomicity` — 2026-07-27'de de aynı testte aynı flake
  loglanmıştı, ilgisiz değişiklikle; izole 3/3 geçti). Ruff temiz.
- **wiki** — Yeni sayfa `synthesis/nau_deepr_ucuncu_tur_2026_08_09.md`;
  `nau_deepr_ikinci_tur_2026_08_08.md`'ye ileri-referans; `webapp_module_map.md`
  `agent_backtest.py` satırına bölüm eklendi; test dosyasına Wiki References.
  Backlinks + index + lint yenilendi (0 broken_links/orphans/stale/stubs).

## 2026-07-27 (2) — Strategy Builder ana kabuğun içine alındı (kod → wiki)

- **rapor** — Kullanıcı: "strategy builder diğer sayfalar gibi solda link ve frame ile açılsın". Sayfa kendi başına duran bir HTML belgesiydi; nav linki vardı ama tıklayınca kabuk (sidebar + topbar) kayboluyordu.
- **fix** — `web/templates/studio/page.html` artık `base.html`'i extend ediyor; `base.html`'e `{% block head %}` + `{% block title %}` eklendi, body sınıfı `page-builder` oldu. Route `active="studio_builder"` + `page_title` geçiriyor (nav highlight + topbar başlığı).
- **asıl iş** — `studio.css` global yazılmıştı (`:root`, `body`, `header`, `footer`, `.btn`, `.metric`, `.tab`, `--panel`…) ve kabuğu yeniden renklendirirdi. **Tüm kurallar `.studio-embed` altına kapsandı** (mekanik dönüşüm + elle düzeltilen `body` kuralı). Özgüllük de lehe çalışıyor: `.studio-embed .btn` app.css'in `.btn`'ini yener.
- **incelik** — htmx artık yalnız `base.html`'den geliyor (sayfanın cdnjs kopyası silindi, çift dinleyici riski); `rem` kökten çözüldüğü için 15px kök ölçüsü sayfa-yerel `<style>:root{font-size:15px}</style>` ile korundu (app.css'te `rem` yok). Sayfa içi "StrategyBuilder" logosu kaldırıldı — topbar zaten adı yazıyor.
- **doğrulama** — `/studio/wt-funding-v3`: `<body class="page-builder">`, nav linki `active`, topbar "Strategy Builder", tek `.studio-embed`, tek htmx. **544 geçti / 3 atlandı**, ruff check temiz. (`test_promote_draft_atomicity` bir kez düştü, yeniden koşumda 5/5 geçti — eşzamanlılık testinde flake, bu değişiklikle ilgisiz.)

## 2026-07-27 — Kurucunun sayfa markası nav'dan ayrıştırıldı (kod → wiki)

- **rapor** — Kullanıcı (ekran görüntüsüyle): "bunun adı ile sol menüdeki ad karışıyor". Kurucu sayfası `Strategy<span>Studio</span>` logosunu taşıyordu; soldaki nav'da ise farklı bir sayfaya (`/studio`, Composer+Backtest) giden "Strategy Studio" linki vardı. Nav 2026-07-26'da ayrışmıştı ama sayfanın kendi markası hizalanmamıştı.
- **fix** — `web/templates/studio/page.html`: logo → `Strategy<span>Builder</span>`, `<title>` → "Strategy Builder — {ad}". Nav etiketiyle ("Strategy Builder") artık birebir aynı.
- **karar** — Yalnızca kullanıcı yüzeyi değişti; `web/routes/strategy_studio.py`, `strategy_studio/` paketi ve `/studio/{id}` rota öneki tarihsel adlarını korudu (modül docstring'i bu ayrımı açıkça yazıyor).
- **wiki** — `synthesis/strategy_studio.md` nav-çakışması bölümü sonuçla güncellendi, `summary` + `last_updated` tazelendi.

## 2026-07-26 (3) — İndikatör kütüphanesi paneli: dekoratiften işlevliye (kod → wiki)

- **rapor** — Kullanıcı: "strategy studio da sol frame yok oluyor". Kök neden çökme değil: `studio.css` `@media (max-width:1100px){ .library{display:none} }` — dar pencerede panel geri getirilemeden kayboluyordu.
- **asıl bulgu** — Panel **zaten işlevsizdi**: `studio.js`'te tek referansı yoktu (tıklama/sürükleme yok, arama kutusu ölü). Kural ekleme yalnızca blokların kendi `add-rule-form`'undan yapılabiliyordu. Gizleyen CSS kuralı, panelin işlevsizliğini de gizliyordu.
- **karar** — Kullanıcı "işlevli hale getir" dedi. **Yeni endpoint açılmadı**: drag-drop + tıkla-ekle hedef bloğun DOM'daki `add-rule-form`'unu doldurup submit eder → "＋ Add condition" ile aynı sunucu yolu, aynı doğrulama. Sunucu tarafı değişmedi.
- **incelik** — regime bloğu tek `.block-body`'de üç kural listesi tutuyor → üç ayrı dropzone (`regime`/`sub_entry`/`sub_exit`). `data-dropzone` htmx swap sonrası HTML'de de bulunmalı, yoksa ilk bırakmadan sonra sürükleme sessizce ölür (doğrulandı). Panel artık gizlenmiyor, daralıyor.
- **doğrulama** — 3 dropzone + 15 draggable öğe render; `POST blocks/exit/rules` RSI'ı doğru bloğa ekledi; swap sonrası `data-dropzone` korunuyor; taslak discard ile temizlendi. **207 geçti / 1 atlandı**, ruff temiz.
- **wiki** — `synthesis/strategy_studio.md`'ye yeni bölüm. Backlinks + index + lint yenilendi.

## 2026-07-26 (2) — Nav'a "Strategy Builder" linki + bayat doküman keşfi (kod → wiki)

- **bulgu** — Kullanıcı `studio_app`'i ana app'e APIRouter olarak taşıma isteğiyle geldi; iş **zaten 2026-07-25'te tamamlanmıştı** (`web/routes/strategy_studio.py`, `server.py` include_router, paylaşılan templates/static/store). Talep bayat bir öncüle dayanıyordu.
- **fix** — `web/templates/base.html`: kurucuya (`/studio/{strategy_id}`) giden hiçbir nav linki yoktu — var olan "Strategy Studio" linki farklı bir sayfaya (`/studio`, Composer+Backtest) gidiyordu. Yeni link ayrı etiketle eklendi: **"Strategy Builder" → `/studio/wt-funding-v3`** (kullanıcı onayı ile).
- **doc-fix** — `studio_app/wiki/strategy-studio.md` merge-öncesi durumda dondurulmuş kalmıştı (hâlâ "pending merge" + "82 test" diyordu) → frontmatter + TL;DR + Integration Points bölümü düzeltildi, [[strategy_studio]]'ya yönlendiren bir üst-not eklendi.
- **doc-fix** — `wiki/synthesis/strategy_studio.md`: nav çakışması notu genişletildi (Strategy Studio vs Strategy Builder), stale "161 test" → "196 geçti / 1 atlandı" (2026-07-26'da yeniden ölçüldü), `last_updated` tazelendi.
- **wiki** — `synthesis/webapp_module_map.md`'ye yeni değişiklik-günlüğü maddesi eklendi, `last_updated` tazelendi. Backlinks + index yenilendi, lint temiz (0 broken_links/orphans/missing_summary/missing_frontmatter/stale/stubs).
- **karar** — StrategyStore'un ayrı `studio.db`'si **korundu** (kullanıcıya soruldu): uygulamanın paylaşılacak "ana SQLite"si yok, bu zaten bilinçli bir tasarımdı.

## 2026-07-20 (8) — Robustness polling fix + AI Plan sekme sırası + plan cache refactor (kod → wiki)

- **fix** — `robustness_progress.html`: `hx-swap="outerHTML"` → `hx-target="#robustness-result" hx-swap="innerHTML"`. `#robustness-result` DOM'dan kalkınca polling duruyordu.
- **ux** — `✨ AI Plan` sekmesi `Sonuç`'un önüne alındı (sıra: AI Plan · Sonuç · Robustness · Geçmiş). Sayfa ilk açılışında aktif sekme AI Plan; backtest tamamlanınca `btTab('result')` otomatik Sonuç'a geçer.
- **fix** — `web/routes/backtest.py` plan cache refactor: `propose_refined_description` artık hiç cache'lenmez — her "✨ Önce AI ile iyileştir" basışında taze AI çağrısı. `bd` (blok planı) `(desc, allow_short)` key ile cache'de kalır. İkinci kez basınca öneri gelmeme sorunu çözüldü.
- **fix** — `agent.py`: `propose_refined_description` exception'ları `agent.refine` logger'a yazılır.
- **wiki** — `synthesis/webapp_module_map.md` değişiklik günlüğüne yeni madde eklendi. Backlinks + index yenilendi.

## 2026-07-20 (7) — Robustness "Analizi Çalıştır" polling duruyordu (kod → wiki)

- **bug/fix** — `POST /robustness/run` progress fragment'ı (`robustness_progress.html`) `#robustness-result` div'inin **içine** (`innerHTML`) yazılıyordu. Fragment kendi kendini `hx-target="this" hx-swap="outerHTML"` ile replace edince `#robustness-result` elementi DOM'dan kalkıyor, sonraki polling isteği hedefi bulamıyor, analiz görünmez şekilde duruyordu. **Düzeltme**: `hx-target="this" hx-swap="outerHTML"` → `hx-target="#robustness-result" hx-swap="innerHTML"`. `web/templates/fragments/robustness_progress.html`.
- **wiki** — `log.md` güncellendi. Mekanik sync (backlinks+index) çalıştırılacak.

## 2026-07-19 — Fix: strateji üretim paneli --reload kesintisi + zarif düşüş (kod → wiki)

- **root-cause (deneysel)** — Kullanıcı raporu: `/backtest` doğal-dil üretiminde sağ-üstteki "✨ Generating strategy from description" paneli üretim ORTASINDA ("Writing blocks" fazında) aniden kayboluyor. İki kontrollü koşumla kanıtlandı: **`--reload` AÇIK** → 8. sn'de poll "Generation record not found" (panel silindi), log'da `WatchFiles detected changes in 'composer.py'. Reloading... → Shutting down → Started server process`; **`--reload` KAPALI** → 20. sn'de üretim tamamlandı, panel sağ kaldı → backtest'e zincirlendi. Kök neden: `uvicorn --reload` proje kökündeki HER `*.py` değişiminde sunucuyu yeniden başlatır; üretim ~15-20 sn süren worker thread'de çalışıp durumu BELLEKTE tutar (`_GEN_PROGRESS`), o sırada izlenen bir `.py` değişince (eşzamanlı editör kaydı / ruff format) worker + durum uçar, poll `state=None` alır, panel eskiden sessizce kaybolurdu. Üretimin kendi disk yazımları (`save_custom`/`append_to_catalog`) `~/.cache/`'e (proje dışı) gider — reload'ı tetiklemez (in-process probe ile doğrulandı).
- **fix** — (1) **Zarif düşüş**: `describe_progress` route + `fragments/describe_progress.html` `state=None` durumunu artık boş div ile paneli yok etmek yerine aynı `.bt-gen-float` overlay'de "⚠ Kesildi — üretim yarıda kesildi, sunucu yeniden başladı" mesajıyla gösterir; `hx-trigger` üretmez → polling durur. (2) **Reload watch daraltma**: `server.py` docstring + `README.md` çalıştırma komutu `--reload-exclude "$PWD/nautilus_wiki" "$PWD/.claude" "$PWD/tests"` (MUTLAK yol — uvicorn göreli adı/glob'u eşleştirmez, yalnız var olan dizini `path.parents` ile recursive dışlar; deneysel olarak `FileFilter` ile doğrulandı) + uzun üretim için `--reload`'sız çalıştırma önerisi.
- **verify** — 3 durum Jinja render testi (state=None → Kesildi + polling yok; running → `every 1s`; done → backtest chain). End-to-end: `--reload-exclude "$PWD/..."` ile `nautilus_wiki/`+`tests/` altındaki `.py` değişimi reload TETİKLEMEDİ, üretim tamamlandı, panel sağ kaldı; regresyon: `web/` altındaki `.py` değişimi reload'ı HÂLÂ tetikliyor. Ruff temiz. **NautilusTrader kütüphanesine dokunulmadı** — yalnız web katmanı + dev çalıştırma komutu. (Not: 3 golden + 1 parallel test fail'i BENDEN bağımsız — golden'lar `composer.py`'daki önceden var olan uncommitted `vol_target` çalışmasından, parallel test macOS'ta `ctypes.windll` Windows-API'sinden; dosyalarım stash'liyken de aynı fail'ler.)

## 2026-07-18 — Sync: birleşik backtest UI + çoklu-TF + MTF trend filtresi (kod → wiki)

- **update** — `wiki/synthesis/webapp_module_map.md`: bu oturumun UI/akış değişiklikleri işlendi. `last_updated: 2026-07-18`.
  - `web/routes/backtest.py`: **/backtest "01" paneli tarif-odaklı tek akışa indirildi** — kayıtlı-strateji dropdown'ı ve instrument-kind (Bybit/Index/External) seçici kaldırıldı. `POST /backtest/describe` çoklu-TF alır; 2+ TF → `POST /backtest/sweep` (aynı spec her TF'de, karşılaştırma tablosu, cache-yok atlanır), 1 TF → `POST /backtest/run` (tam sonuç + equity). `_normalize_intervals` ortak helper; `intervals_csv` zincir yedeği. Sembol datalist typeahead. Index/External route'ları korundu ama panelden çıktı.
  - `web/routes/strategy.py` + `composer.py`: **2-TF trend filtresi** manuel composer'a açıldı — `trend_filter`/`trend_interval`/`trend_ema_period`; `ComposedStrategy` üst-TF (`secondary_bar_type`) bar feed'ini subscribe eder, EMA trend onayıyla ana-TF girişlerini kapıya sokar (look-ahead güvenli — üst-TF barı yalnız kapandığında).
- **verify** — Sayılar kaynaktan doğrulandı: pytest **289 test** (collect), backtest route'ları (`/run`, `/describe`, `/sweep` + progress), `trend_filter`/`secondary_bar_type` composer'da mevcut. Canlı: `/backtest` yeni panel render, eski öğeler yok, POST'lar 422'siz bağlanıyor. README `/backtest` satırı + Advanced Options MTF bülteni güncellendi.
- **note** — Backtest hesap yolu (engine seçimi, metrikler) değişmedi; yalnız UI akışı ve strateji-giriş yüzeyi değişti. Sembol evreni hâlâ 3 (`BYBIT_SYMBOLS`); datalist genişledikçe otomatik büyür. `wiki/{concepts,entities,tutorials}/**` (upstream Nautilus sentezleri) bu sync'te değişmedi.

## 2026-07-17 — Sync: denetim sağlamlaştırması + NAU per-trade sharpe (kod → wiki)

- **update** — `wiki/synthesis/webapp_module_map.md`: bu oturumdaki ~14 commit işlendi. `last_updated: 2026-07-17`.
  - `composer.py`: builtin blok **9 → 13** (`adx_threshold`, `stoch_rsi_cross`, `wave_trend_cross`, `donchian_channel` — NAU parite indikatör kütüphanesi `indicators.py`/`ind`, bloklar `highs`/`lows` OHLC serileri görür); **NAU_WINDOW=260 sabit pencere** (recursive bloklar için buffer ≥260); flip yolu (`allow_short`, `_cancel_working` + `close_all_positions(tags=['flip'])`, flat girişte GTC-limit iptali).
  - `wfo_optimizer.py`/`agent_backtest.py`: **NAU per-trade sharpe hizalaması** — composite skorun 0.3 terimi annualized 252-gün yerine per-trade ((mean/std)×√n); calmar DD_FLOOR/CALMAR_CAP, `MIN_VALID_FOLDS_FRAC=0.6`, embargo 2 gün.
  - `sandbox.py`: graceful-exit atexit koruması; `codegate.py`: loop-budget yalnız `evaluate()`'te reset (helper içi sonsuz döngü artık yakalanıyor); `data.py`: katalog yazım kilidi + force_refresh cache-merge.
  - Yeni **Sağlamlaştırma & regresyon** bölümü: NAU-uyum denetimi (çekirdek sadık), 262-test regresyon süiti.
- **verify** — Sayılar kaynaktan doğrulandı: builtin blok **13** (`BLOCK_REGISTRY builtin:True`), route dosyası 13, pytest **262 test**, `nautilus_trader==1.230.0`, NAU_ev 591 US-equity. README built-in blok listesi 9 → 13 güncellendi.
- **note** — Backtest metrikleri (pnl/n_trades/sharpe_nautilus) değişmedi; yalnız NAU composite skorun kazanan-seçimi değişti — bu tarihten önceki mutlak skorlar kıyaslanamaz. `wiki/{concepts,entities,tutorials}/**` (upstream Nautilus sentezleri) bu sync'te değişmedi.

## 2026-07-15 — Sync: webapp özellikleri (kod → wiki)

- **update** — `wiki/synthesis/webapp_module_map.md`: 5410393 sonrası (~15 commit) uygulama değişiklikleri modül haritasına işlendi. `last_updated: 2026-07-15`.
  - `composer.py`: **9 builtin blok** (`volume_spike` eklendi — bloklar artık `indicators["volumes"]` hacim serisi görüyor); **karar günlüğü** (`_decision_log` — her giriş/çıkış kararında ateşleyen blok + params + indikatör değeri, emirlere `dr:`/`xr:`/`sl`/`tp`/`flip`/`eob` tag'i); perf: `_closes`/`_volumes` deque→düz-list buffer + `_current_equity` hızlı-yol cache (sonuç-birebir, parite testli).
  - `backtest.py`: `_extract_trades` positions↔fills tag join'i ile trade başına giriş/çıkış sebebi (fills lookup tek-geçiş dict).
  - `web/routes/reports.py`: istemci sayfalama + kolon filtreleri + hızlı sıralama + kalıcı görünüm (`reports_layout.json`); **`GET /reports/detail`** — log satırından deterministik yeniden-koşum (sandbox child, `run_in_executor`) → grafik + sebepli trade tablosu + sadakat rozeti.
  - `web/routes/agent_backtest.py`: canlı Gantt zaman çizelgesi (span track, /sessions replay); kesilen run için dürüst terminal mesajı.
  - `web/routes/chart.py`: pencere/TF fizibilite guard'ı (>60k mum reddi).
- **verify** — Sayılar kaynaktan doğrulandı: builtin blok 9 (`BLOCK_REGISTRY builtin:True`), route dosyası 13, `/reports/detail` endpoint mevcut, sebep alanları (`entry_reason`/`exit_reason`/`exit_kind`) `backtest.py`'de.
- **flag** — BacktestEngine yolu artık per-trade karar sebebini yakalıyor; BacktestNode yolu (strateji instance tutmaz) hâlâ yalnız özet metrik — "Bilinen boşluklar" güncellendi.
- **note** — `wiki/{concepts,entities,tutorials}/**` (upstream Nautilus doküman sentezleri) bu sync'te değişmedi; yalnız uygulama-özel `webapp_module_map.md` etkilendi.

## 2026-07-05

- **init** — Wiki iskeleti kuruldu: `sources/`, `wiki/{entities,concepts,synthesis}/`, `CLAUDE.md`, `index.md`, `log.md`.
- **ingest** — README snapshot alındı → `sources/01_readme_snapshot.md` (kaynak: https://github.com/nautechsystems/nautilus_trader).
- **ingest** — Architecture docs özeti alındı → `sources/02_architecture_docs.md`.
- **ingest** — Strategies docs özeti alındı → `sources/03_strategies_docs.md`.
- **ingest** — Backtesting docs özeti alındı → `sources/04_backtesting_docs.md`.
- **pages** — Oluşturulan wiki sayfaları:
  - entities: nautilus_kernel, message_bus, data_engine, execution_engine, risk_engine, cache, adapters, strategy_and_actor
  - concepts: event_driven_architecture, single_threaded_core, crash_only_design, environment_contexts, order_flow_pipeline, precision_modes
  - synthesis: backtesting_guide, rust_python_hybrid, getting_started_roadmap
- **flag** — Sürüm bilgisi: NautilusTrader v2 release-candidate fazında; sürüme özgü iddialar (`v2.x`) etiketlenmeli. Docs URL'leri `latest` üzerinden alındı; ileride sürüm sabitlemeli belge yakalanmalı.
- **gap** — Şu konular kaynak snapshot'ında henüz yok, sonraki ingest'lerde eklenecek:
  - Model paketi (Order, Position, Instrument, Currency) ayrıntısı
  - Indicators paketi
  - Persistence / ParquetDataCatalog API'si
  - Live node başlatma ve reconciliation davranışı
  - Adaptör yazma rehberi

## 2026-07-06

- **synthesis** — `wiki/synthesis/index_backtest_via_equity_proxy.md` eklendi. Motivasyon: `nautilus_web_app`'e US index tick backtest desteği eklendi (Polygon-format CSV.gz'lerden OHLCV). `IndexInstrument` "not directly tradable" olduğu için tradable `Equity` proxy'sine dönüldü.
- **flag** — Equity `size_precision=0` trap'i: kesirli `trade_size` sessizce sıfır quantity üretir; strateji seviyesinde clamp gerektirir. Sayfada belgelendi.
- **flag** — Polygon tick verisinde volume yok; `resample.count()` tick-count proxy olarak kullanılıyor. Gerçek volume gerekirse ayrı kaynak lazım.
- **gap** — Corporate actions, multi-instrument portfolio, exchange-calendar filter hâlâ scope dışı.
- **ingest** — Resmi tutorials (`https://nautilustrader.io/docs/latest/tutorials/`) taranıp sentezlendi → `wiki/tutorials/` altına 19 sayfa: quickstart, backtest_low_level, backtest_high_level, backtest_fx_bars, backtest_orderbook_binance, backtest_orderbook_bybit, book_imbalance_betfair, loading_external_data, data_catalog_databento (placeholder), fx_mean_reversion_ax, gold_book_imbalance_ax, grid_market_maker_bitmex, grid_market_maker_dydx, hurst_vpin_kraken, options_data_bybit, delta_neutral_bybit, delta_neutral_derive, lighter_rwa_composite_mm, howto_get_started_lighter (placeholder).
- **flag** — URL yapısı değişmiş: `/tutorials/*` altındaki çoğu path 404 dönüyor; içerik `/getting_started/*` + `/nightly/tutorials/*` + GitHub `docs/tutorials/*.py` kaynak dosyalarına dağılmış. Her sayfanın `sources:` alanında gerçek yakalandığı URL kayıtlı.
- **flag** — 2 sayfa placeholder: `tutorial_data_catalog_databento.md` (databento catalog rendered olarak yayında değil, kaynak `.py`) ve `howto_get_started_lighter.md` (hiçbir varyantta bulunamadı).
- **gap** — Tutorial'lardan sürekli referans alınan ama wiki'de dedicated sayfası olmayan konular: `ParquetDataCatalog` (entity), data wrangler ailesi (`QuoteTick/TradeTick/OrderBookDelta/BarDataWrangler` — concept), bar aggregation modes (`TIME/TICK/VOLUME/VALUE × INTERNAL/EXTERNAL` — concept), options greeks pipeline (`OptionChainSlice`, `OptionSeriesId`, `StrikeRange` — concept), `ImportableStrategyConfig` + `BacktestNode` orkestrasyonu (synthesis), BitMEX `deadman's switch` gibi resilience pattern'leri (synthesis).
- **synthesis** — `wiki/synthesis/v1_to_v2_migration_lessons.md` eklendi. Motivasyon: `nautilus_web_app` canlı olarak v1.230.0 → v2.0.0rc1 port edildi (6 catalog spec bit-identical parite). Plan öncesinde öngörülmeyen breaking-change'ler belgelendi: modül düzleştirmesi (`nautilus_trader.model.data|enums|identifiers|instruments|objects|currencies` hepsi flat), `StrategyConfig` artık msgspec Struct değil (plain class + `__init__`/`**kwargs`), `BarDataWrangler.process(df)` kaldırıldı (Arrow-only ingest; DataFrame projelerinde `Bar()` direkt construction gerekli), `engine.trader` gitti, `portfolio.analyzer` → `portfolio.statistics()`, `BTC`/`USD` sabitleri gitti (`Currency.from_str`).
- **flag** — v2 rc1 stat bug: `portfolio.statistics().returns` içinde Sharpe/Volatility/RiskReturnRatio nan dönüyor (Sortino, Profit Factor, Avg Win/Loss doğru hesaplanıyor). Trading davranışı etkilenmiyor — istatistik hesaplama boşluğu; RC final'de düzelmesi bekleniyor.
- **flag** — Plan'da beklenen ama v2 rc1'de **gelmemiş** değişiklikler: `bon` builder pattern (`CurrencyPair.builder().build()`) — rc1'de hâlâ kwargs kalıbı çalışıyor. Bracket order builder-chain — hâlâ 40+ kwarg alan klasik factory metodu. `OrderRef`/`PositionRef` typed cache — attribute access aynı, snapshot gerekmedi. Belki RC2/final'de gelecekler.
- **gap** — v2 rc1 için hâlâ eksik: adaptörlerin (Bybit/Binance/etc.) v1'e kıyasla env-based config'e taşınıp taşınmadığı (RELEASES.md flag'ledi ama webapp test etmedi), Live TradingNode reconciliation davranışının v2'de nasıl değiştiği, `ParquetDataCatalog` write/read API'sinde precision-mode etkileri.

## 2026-07-07

- **reorg** — Karpathy'nin *"LLM Knowledge Bases"* yaklaşımına göre wiki yeniden yapılandırıldı:
  - **Bare-name wikilinks**: 35 sayfa, 119 wikilink `[[wiki/section/name.md]]` → `[[name]]` biçimine dönüştürüldü. Obsidian graph view artık native çalışır.
  - **Per-page summaries**: 38 mevcut sayfaya `summary:` (<=180 karakter, tek satır) ve `key_concepts:` (3–8 slug) frontmatter alanları eklendi; Workflow'da paralel LLM özet çağrılarıyla üretildi (~150k token, 40 subagent).
  - **Auto-backlinks**: Her wiki sayfasının sonunda `<!-- BACKLINKS:BEGIN -->` bloğu, `tools/wiki_tools.py backlinks` çağrısıyla idempotent şekilde yeniden yazılır. 48 sayfada güncel.
  - **Frontmatter-driven `index.md`**: Elle bakım yerine `tools/wiki_tools.py index` her sayfanın frontmatter'ından üretir. `*(stub)*` badge'i, kısa özet, dosya yolu.
  - **10 yeni stub sayfası** consolidated candidate + `log.md` gap listesinden LLM ile draft edildi, adversarially verify edildi (10/10 issue=[], 1 fabricated_claim yakalanıp repair pass ile düzeltildi):
    - entities: `parquet_data_catalog`, `data_wranglers`, `backtest_node`, `live_node`, `order_emulator`, `execution_algorithms`, `portfolio`
    - concepts: `bar_aggregation_and_type_syntax`, `option_greeks_pipeline`, `venue_reconciliation`
  - **`lint/` klasörü**: İki dosya üretildi — `lint/2026-07-07_health.md` deterministic tarama (broken_links/orphans/missing_summary/stubs) ve `lint/2026-07-07_llm_audit.md` Karpathy-tarzı LLM audit (contradictions, stale, underlinked, next_ingest). `tools/wiki_tools.py lint --write` sadece deterministic olanı üretir; LLM audit workflow'dan gelir.
  - **Tools**: `nautilus_wiki/tools/wiki_tools.py` — subcommands: `index`, `backlinks`, `lint`, `search`, `resolve`, `show`, `stub`.
  - **Web frontend**: `wiki_helper.py` yeniden yazıldı; `[[bare]]` wikilinks HTML'e dönüştürülürken `/wiki/wiki/section/slug.md` URL'lerine çevrilir. `web/routes/wiki.py` eklendi (`/wiki`, `/wiki/slug/<slug>`, `/wiki/<path>` route'ları); sidebar'a Wiki nav girişi eklendi.
  - **CLAUDE.md güncellendi**: Karpathy referansı, `lint/` katmanı, frontmatter zorunlu alan olarak `summary`, backlinks bölümü, tools dokümantasyonu.
- **flag** — Health-check bir çelişki yakaladı: `tutorial_quickstart.md` v1 varsayımıyla `BarDataWrangler.process(df)` çağrısını anlatıyordu; v2 rc1'de bu API kaldırılmış (bkz. `v1_to_v2_migration_lessons`). Satır güncellendi: v1/v2 ayrımı ve `Bar()` doğrudan yapıcısına yönlendirme + `[[data_wranglers]]` cross-link eklendi.
- **flag** — 10 stub sayfası hâlâ `status: stub`; sonraki ingest'lerde v2 nightly docs ve `nautilus_trader` repo'sundaki README/CHANGES üzerinden doldurulmalı. `lint/2026-07-07_health.md` bunlar için önerdiği kaynakları listeler.
- **gap** — Wiki'yi Obsidian vault olarak açıp Marp slaytları/plugin bazlı görselleştirme akışını denemek Karpathy pattern'inin diğer yarısı (`Output` bölümü); ayrı bir tur.
- **review** — Karpathy-tarzı 7-boyutlu full-wiki adversarial review (`Workflow(wiki-full-review)`, 98 subagent, ~2.3M token, 690 tool call). 91 raw finding → adversarial verify sonrası 44 confirmed, 47 rejected (verifier false-positive elimine etti). Ana bulgular:
  - **blocker**: `tools/wiki_tools.py cmd_backlinks` outgoing-link tarama sırasında zaten yazılmış `<!-- BACKLINKS:BEGIN -->` bloğunu strip etmiyordu — sonuç: backlink kendini besliyor, `lint` orphan sayısı 0 raporluyordu ama 7 sayfa gerçekten orphan'dı. `_strip_backlinks()` helper eklendi, `cmd_backlinks` ve `cmd_lint` her ikisi de body'yi tararken bloğu strip ediyor. Ayrıca code-region (fenced ``` ve `inline`) hariç tutuluyor.
  - **schema**: 11 sayfada `sources:` alanı Layer 2 wiki/**/*.md yollarına işaret ediyordu — Layer 1 kaynak izlenebilirliği kuralına aykırı. Hepsi `sources: → sources/*.md + upstream URL` ve `related: → wiki/**/*.md` olarak ayrıştırıldı. `CLAUDE.md` şemaya yeni `related:` alanı eklendi.
  - **link-graph**: 7 stub gerçekten orphan'dı (backlinks kendini besliyordu). `strategy_and_actor`, `execution_engine`, `order_flow_pipeline`, `environment_contexts`, `rust_python_hybrid`, `v1_to_v2_migration_lessons`, `getting_started_roadmap`, 3 opsiyon tutorial'ı — hepsine gerçek body `[[slug]]` wikilinks eklendi.
  - **v1/v2**: `data_wranglers` stub'ında BarDataWrangler.process kaldırılışı v1/v2 ayrımı olmadan yazılmıştı; `v1 → v2rc1` başlıklı bölüm eklendi. `backtesting_guide.md` `key_concepts` içindeki resolve-etmeyen `backtest_engine` ve `book_type` slug'ları düzeltildi.
  - **code/security**: `wiki_helper._rewrite_wikilinks` — regex `[[...]]` içinde `[]` reddediyor; label HTML-escape ediliyor; fenced/inline code bloğu içindeki wikilink syntax'ı rewrite edilmiyor. `wiki_tools.py`: UTF-8 encoding tüm read/write'larda, `lint --write` `--date` yoksa bugünkü tarihi kullanıyor (önceki: 'latest' overwrite), `stub tutorial` `tutorial_` prefix'ini otomatik ekliyor, docstring güncel.
  - **obsidian**: `.gitignore` genişletildi; `workspace.json` ve `graph.json` per-user UI state olduğu için exclude edildi.
  - **log/lint**: LLM audit output ayrı dosyada (`lint/2026-07-07_llm_audit.md`), deterministic lint çıktısı (`lint/2026-07-07_health.md`) ile karışmıyor.
- **e2e-run** — Bybit BTCUSDT USDT-perp 1m, 7-day pencere (2026-06-30 → 2026-07-07 UTC) uçtan uca test edildi. Ingest: `load_bybit_bars` (`data.py:272`) — v5/kline üzerinden ~8s'de 10,080 satır, 0 boşluk, parquet cache (`~/.cache/nautilus_web_app/bybit/linear_BTCUSDT_1m.parquet`, 41 KB). Backtest: `run_backtest("ma_crossover", {fast:10, slow:30})` — 0.31s, 191 trade, +$286.16 PnL, win-rate 35.6 %, max_dd -0.02 %.
- **flag** — `[[v1_to_v2_migration_lessons]]` sayfasında belgelenmiş **v2 rc1 Sharpe nan bug'ı canlı olarak reproduce oldu**: `run_backtest` metriklerinde `sharpe: nan` döndü; aynı çalıştırmada Sortino ve win-rate doğru hesaplanıyor. Sayfadaki iddia bit-identical çıktı — dokümantasyon güncel.
- **flag** — `run_backtest` (Yahoo BTC-USD instrument) Bybit ham DataFrame'ini kabul ediyor çünkü instrument sadece precision + venue metadata'sı sağlıyor; OHLCV shape aynı olduğu için tag mismatch bar builder'da bir sorun üretmiyor. Gerçek Bybit sembolü + venue eşleşmesi için `run_composed_backtest` yolunun `_make_bybit_instrument` ile çağrılması gerekir — canlı emir gönderimi/adaptör test edilmeyecekse mevcut yol yeterli.

## 2026-07-08

- **reorg** — Wiki'ye göre kod tabanı yeniden düzenlendi (Karpathy loop'un kod→wiki bacağı):
  - Repo'nun 17 Python modülünün üstüne `Wiki References` bloğu enjekte edildi (deterministic pass, `python3 -c ...`). Her modül artık ait olduğu wiki sayfalarını bare-name wikilink olarak listeliyor.
  - Yeni: `ARCHITECTURE.md` repo kökünde — katman diyagramı + webapp↔wiki karşılık tablosu + uçtan uca akış.
  - Yeni: `wiki/synthesis/webapp_module_map.md` — ters yönden aynı köprü; her Python dosyasının wiki karşılığını tek yerde. `[[getting_started_roadmap]]` ve `[[v1_to_v2_migration_lessons]]` sayfaları buradan cross-link ediyor.
- **flag** — Yeni sayfanın `sources:` alanında ilk denemede absolute local path (`/Users/.../ARCHITECTURE.md`) vardı; schema kuralı (Layer 1 = `sources/*.md` veya URL) gereği repo-relative `sources/02_architecture_docs.md` + upstream repo URL'siyle değiştirildi.
- **verify** — 49 sayfa üzerinde `tools/wiki_tools.py backlinks` + `lint`: 0 broken, 0 orphan, 0 missing summary, 10 stub (beklenen). `.venv/bin/python -c "import ..."` 10 modül için hata olmadan geçti.
- **feature** — `/data` "Instrument Catalog" ekranı eklendi. `web/routes/data.py` (6 endpoint: GET /data, POST /data/refresh/{yahoo,bybit,index}, POST /data/index/discover, GET /data/fragments/row/{source}/{key}). `data.py`'ye `list_catalog()` + `refresh_row()` public API'si eklendi; her satır Nautilus enstrüman metadata'sını (`InstrumentId`, `price_precision`, `size_precision`, `price_increment`, `min_quantity`, `asset_class`, `base/quote currency`) `backtest.py`'deki `_make_*_instrument` factory'lerinden okuyor — precision fields hiç duplicate edilmiyor. Wiki-flagged tuzaklar rozet + cross-link olarak render ediliyor: `size_precision=0 ⚠` → [[index_backtest_via_equity_proxy]]. BarType DSL string'i (5 alan: `SYMBOL.VENUE-STEP-AGG-SRC-ORIGIN`) her satırda gösteriliyor — [[bar_aggregation_and_type_syntax]] tarafından tanımlı.
- **flag** — Bybit UI grid'i Symbol × Category × Interval (3 × 3 × 6 = 54 hücre × sembol) matrisini tam gösteriyor, ama `data.py`'deki `_BYBIT_MS` sözlüğü şu an sadece `"1"`/`"5"` içeriyor. Grid'de desteklenmeyen hücreler dashed border ile "n/a" olarak render — kullanıcı önce `_BYBIT_MS`'yi genişletmeli. Wiki referansı gerekiyor: BybitCategory Literal'ının canlıdaki mapping'i (spot vs linear vs inverse için farklı endpoint semantic'i) hâlâ [[adapters]] altında dokümante değil.
- **flag** — Index sayfası 10,490 ticker'ı server-side filtreleme + top-50 rendering ile veriyor; UI virtual scroll değil. Full listing için `?q=` query param'ı ekleniyor.
- **feature** — ParquetDataCatalog entegrasyonu tamamlandı. `data.py`'ye `write_to_nautilus_catalog(source, **kw)` + `nautilus_catalog_bar_state(bar_type_str)` + `get_nautilus_catalog()` eklendi. Catalog `~/.cache/nautilus_web_app/nautilus_catalog/` altında. `backtest.py`'ye `run_backtest_node()` eklendi — `BacktestNode` + `ParquetDataCatalog` high-level yolu. `web/routes/data.py`'ye `POST /data/catalog/write` endpoint'i eklendi; `/data` ekranında cached hücrelerde "→ Catalog" butonu gösteriliyor. BacktestEngine vs BacktestNode karşılaştırması: `win_rate delta = 0.000000` (tam parite). Sharpe `BacktestNode` yolunda NaN değil (v2 rc1 BacktestEngine stats bug bu yolda yok).
- **flag** — `_bars_from_df` timestamp bug düzeltildi: `df.index.astype("int64")` **ms** veriyordu (Bybit cache `datetime64[ms, UTC]`); Nautilus `ts_event` nanosaniye bekliyor. Fix: `idx.tz_localize(None).astype("datetime64[ns]").astype("int64")`. Bu hata `BacktestEngine` yolunda (kendi `add_data` çağrısı) sessizdi ama katalog yolunda `1970-01-01` backtest üretiyordu.
- **flag** — `strategies.py` — `cache.instrument(str)` v2 rc1'de `InstrumentId` bekliyor; `ImportableStrategyConfig` serialization `Decimal`'ı str'e çeviriyor → `make_qty(str)` patliyor. Düzeltme: `_iid()` / `_bt()` yardımcı metodları ve `float(self.config.trade_size)`.
- **flag** — `_prepare_df` OHLC validity filter eklendi: yfinance bazen günlük kısmi bar üretip `low > close` döndürüyor; Nautilus bunu `ValueError` ile reddediyor. Filter drop ediyor.
- **flag** — `write_to_nautilus_catalog` idempotent: `catalog.delete_data_range(type_name="bars", instrument_id=str(bar_type))` ile mevcut aralık temizlendikten sonra yazıyor. v2rc1'de `type_name="bars"` (lowercase) kullanılmalı; `"Bar"` desteklenmiyor.
- **gap** — `BacktestNode` `stats_general` sözlüğü `"Total Trade Count"` içermiyor (sadece `"Long Ratio"` görüldü); trade count `stats_pnls` içinde de yok. v2rc1 BacktestNode'da trade istatistikleri ayrı bir mekanizmadan alınacak — henüz çözülmedi.

## 2026-07-09

- **ingest** — `https://nautilustrader.io/docs/latest/` derin araştırması tamamlandı (`deep-research` workflow, 98 subagent, 2.1M token, 924 tool call). 6 adversarial-doğrulanmış bulgu → `sources/05_latest_docs_research.md` olarak kaydedildi.
- **update** — 8 wiki sayfası güncellendi:
  - `single_threaded_core.md`: "Within a node, the kernel consumes and dispatches messages on a single thread" — resmi alıntı; background services ayrı thread model dokümante edildi.
  - `venue_reconciliation.md`: status stub→draft; "Only the LiveExecutionEngine performs reconciliation" kesin ayrım eklendi.
  - `backtest_node.md`: status stub→draft; resmi öneri alıntısı ("recommended path for production workflows"), BacktestEngine vs BacktestNode karşılaştırma tablosu, v1.229.0 Python binding eklentisi.
  - `parquet_data_catalog.md`: Dual-backend (Rust 7 tip + PyArrow fallback) resmi olarak belgelendi; `ts_init` = kapanış zamanı zorunluluğu + look-ahead bias uyarısı; v1.231.0 bar aggregation first-tick bug fix (develop branch).
  - `execution_algorithms.md`: status stub→draft; TWAP zorunlu parametreler (`horizon_secs`, `interval_secs`) doğrulandı; v1.229.0 `add_native_exec_algorithm` binding.
  - `portfolio.md`: status stub→draft; **Kritik: multi-currency hesaplarda PortfolioAnalyzer sessizce `_empty_returns()` döndürür** — Sharpe NaN bug'ının asıl nedeni. v1.227.0 `PortfolioSnapshot` event + `subscribe_portfolio_snapshot` API eklentisi.
  - `backtesting_guide.md`: Resmi öneri alıntısı; ts_init look-ahead bias kuralı eklendi.
  - `v1_to_v2_migration_lessons.md`: Sharpe NaN asıl neden güncellendi — multi-currency hesap root cause, BacktestNode path'inde bu sorun olmadığı (webapp'te doğrulandı).
- **flag** — `PortfolioAnalyzer` multi-currency sessiz bug'ı: webapp'te `base_currency=None` USDT+BTC çift bakiyeli hesap bu yolu tetikliyordu. BacktestNode path'inde Sharpe doğru hesaplanıyordu çünkü stats yolu farklı. BacktestEngine path için `base_currency=USDT` ile tek-currency hesap önerilir.
- **flag** — v1.231.0 (develop branch, henüz release edilmedi): bar aggregation'da ilk tick dahil edilmiyordu; bu bug fix yakın zamanda gelecek.

## 2026-07-12

- **feature** — Monokrom siyah tema (design2 handoff): `app.css` `:root` token'ları, `chart.js`, `app.js`, tüm template inline renkleri prototipin saf-siyah/beyaz paletine çekildi. `--accent` mavi → `#f2f2f2` beyaz; `btn-primary` gradient → düz beyaz-zemin/koyu-metin; nav.active beyaz sol-şerit. `static_version` hash'i artık `chart.js+app.css+app.js` üçünü kapsıyor.
- **feature** — Reports sayfası büyük güncelleme: (1) sütun göster/gizle + sürükle-bırak sıralama + sunucu tarafı kalıcılık (`~/.cache/nautilus_web_app/reports_layout.json`, `GET/POST /reports/layout`); (2) Yatırım Sermayesi (`starting_cash`) sütunu + filtre; (3) her sütun başlığına hover tooltip (tanım, hesaplama, yorum); (4) freze header+filtre barı (sticky); (5) expand ok kaldırıldı, satıra tıklama ile açılıyor; (6) varyant filtresi (Kârlı/Zararlı/Sharpe≥1/WinRate≥50); (7) Süre ve Enstrüman sütunları kaldırıldı.
- **flag** — `robustness_log.jsonl` sembol bilgisi kaydetmiyor: `_log_robustness` (robustness.py) `symbol`/`category`/`interval` almıyor; aynı stratejiyi farklı sembollerde test edince `spec_id`'ye göre join yapan reports "son ezil" prensibinden dolayı yalnızca en son robustness sonucunu gösteriyor. Cross-instrument test sonuçları reports'ta ayrı satır olarak görünmüyor — bilinen gap, sonraki oturumda çözülecek.

## 2026-07-12

- **feature** — Backtest UI: işlemler tablosu grafiğin **yanına** taşındı (flex-wrap layout; grafik `flex:1 1 560px` solda, işlemler `flex:0 1 396px` sağda, dikey scroll'lu). `chart.js` zaten `ResizeObserver` + `container.clientWidth` ile daralan konteynıra otomatik uyum sağladığı için yan-yana yerleşim ek iş gerektirmedi — [[single_threaded_core]] deterministik render'la ilgisiz, saf frontend. Dar panele sığması için trade tablosu 8→5 kolona indirildi (çıkış detayları satır `title` tooltip'ine).
- **feature** — Reports sayfasına iki süre kolonu eklendi: **Test Periyodu** (`bars.start → bars.end` farkından türetiliyor; tüm geçmiş kayıtlarda çalışır) ve **Çalışma Süresi** (wall-clock elapsed). `web/routes/reports.py`'ye `_fmt_test_period()` + `_fmt_elapsed()` helper'ları eklendi.
- **flag** — Backtest wall-clock süresi artık loglanıyor: `web/routes/backtest.py` `_worker` başında `time.perf_counter()`, `_log_backtest(..., elapsed_sec=...)` ile `backtest_log.jsonl`'e yazılıyor. **Retroaktif değil** — eski kayıtlarda `elapsed_sec` yok, reports'ta `—` görünür. Bu, Nautilus'un `avg_duration_mins` (trade ortalama açık kalma süresi, `stats_pnls` benzeri metrik) ile karıştırılmamalı: biri backtest'in ne kadar sürdüğü, diğeri işlemlerin ne kadar açık kaldığı.
- **feature** — Backtest sonuç metrik bloğu (Realized PnL → Slippage, 18 metrik) yeniden tasarlandı: düz 4-satır KPI grid → hiyerarşik hero kart (Realized PnL öne çıkan büyük değer, kâr/zarara göre renkli) + 3 etiketli grup (Risk · İşlemler · P&L/Maliyet). Saf sunum; metrik hesapları (`Portfolio.equity()`, PnL%, max_dd) değişmedi — bkz [[portfolio]].
- **flag** — Cache-bust hash'i (`server.py` `_static_version`) sadece `chart.js`'i md5'liyordu; `app.css` değişiklikleri tarayıcı cache'inde takılı kalıyordu. Fix: hash artık `chart.js` + `app.css`'i birlikte md5'liyor. Nautilus ile ilgisiz, saf webapp asset-versioning tuzağı.

## 2026-07-13

- **fix** — Agent altyapısı derinlemesine kod analizi: 4 CRITICAL + 8 HIGH + 9 MEDIUM + 6 LOW olmak üzere 27 bulgu tespit edildi ve düzeltildi. Dosyalar: `agent.py`, `web/routes/agent_backtest.py`, `backtest.py`, `web/routes/backtest.py`, `web/routes/robustness.py`, `loop_runner.py`.
- **fix C1** — `agent.py:_test_execute_generated` — `exec()` smoke call thread timeout yok; `while True: pass` AST whitelist'ten geçiyor, server kalıcı hang edebiliyordu. `threading.Thread(daemon=True)` + `join(timeout=2.0)` ile sandbox edildi → `GeneratedCodeError("timed out")`.
- **fix C2** — `agent.py:_fallback_composed` — `random.choice(types)` exit-only bloğu (`atr_stop`) seçince `_validate_composed` ValueError fırlatıyordu (~%1.3 ihtimal). Entry seçim havuzundan `exit_only` set'i çıkarıldı.
- **fix C3** — `agent.py:propose_composed_strategy` — `hint` parametresi eklendi; `agent_backtest.py` içindeki tüm çağrılar (faz 1 + iterasyon loop fallback'leri) artık `hint=hint` ile çağrılıyor. Önceden hint sadece metadata `description` alanına yazılıyor, Claude prompt'una hiç ulaşmıyordu.
- **fix C4** — `web/routes/agent_backtest.py:progress` — `_winner_narrative()` sync LLM çağrısı async route içinden yapılıyordu, event loop ~1-5s donduruyordu. `await asyncio.to_thread(_winner_narrative, ...)` ile event loop dışına taşındı.
- **fix H1** — `web/routes/backtest.py` + `web/routes/robustness.py` — `json.dumps(record, default=str)` NaN float'ları `NaN` token olarak yazıyordu (RFC 8259 ihlali; `JSON.parse` ve `jq` reddeder). `_sanitize_floats()` helper ile `NaN/Inf → None` dönüşümü eklendi.
- **fix H2** — Concurrent backtest thread'leri aynı JSONL dosyasına lock'suz yazıyordu. `_BACKTEST_LOG_LOCK` ve `_ROBUSTNESS_LOG_LOCK` (`threading.Lock`) eklendi.
- **fix H3** — `web/routes/backtest.py:_worker` — `_log_backtest()` I/O hatası outer except tarafından yakalanıp backtest sonucunu gizliyordu. Sıra değiştirildi: result önce `_RUN_PROGRESS`'e yazıldı, sonra `_log_backtest` ayrı try/except içine alındı.
- **fix H4** — `backtest.py` — 4 runner fonksiyonunun hepsinde `LoggerConfig(bypass_logging=True)` hardcode'du; strateji `on_bar()` exception'ları, order rejection'ları tamamen sessizdi. `_BYPASS_LOGGING = not os.getenv("NAUTILUS_DEBUG_LOG")` sabiti eklendi; `NAUTILUS_DEBUG_LOG=1` env var ile debug loglaması açılabiliyor.
- **fix H5** — `web/routes/agent_backtest.py:_score` — `m.get("pnl_pct")` her zaman `None` dönüyordu (log'da anahtar `"pnl"`); 0.3-ağırlıklı PnL terimi hiçbir zaman score'a katkıda bulunmuyordu. `m.get("pnl_pct") or m.get("pnl") or 0.0` ile düzeltildi.
- **fix H6** — Phase 3 sıralama ekranında `r.metrics.get()` çağrısı — başarısız backtest'te `metrics=None` olabiliyordu, `AttributeError` crash. `(r.metrics or {}).get(...)` ile guard eklendi.
- **fix H7** — `_add_step()` adım listesini sınırsız büyütüyordu; continuous mode'da memory leak. `steps[-500:]` cap eklendi.
- **fix H8** — `propose_custom_block` retry loop (`for attempt in range(2)`) — API hatasında `raise` yapıyordu, `continue` ile retry etmiyordu. Transient 429/503'te retry yok sorunu düzeltildi.
- **fix M1** — `continuous_mode` `while True:` — stop sinyali yalnızca round başında kontrol ediliyordu; uzun round'larda (30dk+) fark edilmiyordu. `_MAX_CONTINUOUS_ROUNDS = 100` guard + her backtest iterasyonu başında stop check eklendi.
- **fix M2** — `rob_scan_log` robustness exception'ında kayboluyordu (loop sonunda tek sefer flush). Her candidate sonrası progress dict'e flush + `_run_full_robustness` per-candidate try/except ile sarıldı.
- **fix M3** — `_score()` `math.isinf` kontrolü eksikti; sonsuz Sharpe +inf score üretiyordu. `if math.isinf(sharpe): sharpe = 0.0` eklendi.
- **fix M4** — Continuous round reset'te `s["error"]` temizlenmiyordu; eski hata mesajı yeni round'da görünüyordu. Reset block'a `s["error"] = None` eklendi.
- **fix M5** — `agent.py:_get_client` — iki thread aynı anda `_client is None` görüp çift `Anthropic(...)` örneği oluşturabiliyordu. Double-checked locking (`_client_lock`) eklendi.
- **fix M6** — `rob_scan_log` entry'sinde key `"mc_dd_p95"` p50 (medyan) değeri tutuyordu. `"mc_dd_p50"` olarak düzeltildi.
- **fix M7** — Agent worker thread `daemon=True`; server restart'ta `finally` block çalışmıyor, custom block'lar (`agnt_e_*`/`agnt_x_*`) diskte kalıcı oluyordu. `atexit.register(_cleanup_all_agent_blocks)` eklendi.
- **fix M8** — `robustness.py:_log_robustness` MC error alanı whitelist'te yoktu; `mc_result = {"error": "Trade verisi yok."}` durumu log'a `{}` yazılıyordu. `if "error" in mc_raw: mc_clean["error"] = mc_raw["error"]` eklendi.
- **fix M9** — `_recent_runs()` her `GET /backtest` isteğinde tüm JSONL'i belleğe okuyordu (şu an 2.3 MB, büyüyor). Son 32 KB tail-read ile değiştirildi.
- **fix L1-L7** — `propose_strategy` JSON extraction (`_extract_json_object` kullanıyor), proxy URL `rationale`'dan çıkarıldı, `_propose_agent_strategy_idea` sessiz exception `logging.warning`, `_log_backtest` try/except guard, `loop_runner` bars_info parametreli, `content[0].text` AttributeError guard.
- **update** — `wiki/synthesis/webapp_module_map.md` güncellendi: `web/routes/agent_backtest.py` ve `web/routes/robustness.py` modül tablosuna eklendi; `agent.py` ve `loop_runner.py` satırları yeni davranışı yansıtacak şekilde güncellendi; `backtest.py` satırına `NAUTILUS_DEBUG_LOG` notu eklendi. `last_updated: 2026-07-13`.
- **flag** — `backtest.py:bypass_logging` varsayılan olarak `True` (prod güvenli); `NAUTILUS_DEBUG_LOG=1` sadece geliştirme/debug için. Nautilus iç hatalarını görünür kılmanın tek yolu bu — [[portfolio]], [[strategy_and_actor]] gibi Nautilus bileşenlerindeki sessiz hataları izlemek için önerilen pattern.
- **flag** — `_score()` formülü `pnl_pct` yerine `pnl` (mutlak USDT) kullanıyor; bu ölçek uyumsuzluğu potansiyel sorun. Sharpe (birimsiz) ve win_rate (0-1) ile `pnl` (USDT) bir arada ranklama yapılıyor; normalize edilmesi gerekebilir — bilinen gap.
- **gap** — `_recent_runs` tail-read 32 KB; çok uzun tek-satırlı kayıtlar (büyük equity curve) hâlâ kesilme riski taşıyor. Uzun vadede log rotation veya equity_curve ayrı dosyaya taşıma önerilir.

## 2026-07-13 — paralel motor + harici katalog senkronu

- **update** — `wiki/synthesis/webapp_module_map.md`: yeni **Motor katmanı** bölümü eklendi (`sandbox.py` guarded-subprocess izolasyonu, `backtest_robustness.py` `run_many` fan-out, `parallel_exec.py` ProcessPoolExecutor ~8.7x, `wfo_optimizer.py` deterministik aday üretimi). `data.py` satırına harici salt-okunur katalog (`NAUTILUS_EXTERNAL_CATALOGS`, NAU_ev 591 US-equity, `load_external_bars`) eklendi. `backtest.py` satırına `_BYBIT_SPECS` per-symbol precision + kategori-venue'lar (`BYBIT_SPOT/LINEAR/INVERSE`) eklendi. `agent.py` satırına `NAUTILUS_LLM_BACKEND=auto|api|claude-cli` aboneli̇k backend'i eklendi. Route tablosuna `chart.py`, `lab.py`, `reports.py`, `sessions.py` eklendi. `agent_backtest.py` satırı guarded-subprocess + `_IPC_Q` relay ile güncellendi.
- **update** — `wiki/synthesis/index_backtest_via_equity_proxy.md`: `Equity(...)` örneği koddan sapmıştı (`price_precision=4/0.0001`) — `price_precision=2 / tick 0.01` olarak düzeltildi (NAU QLAB standardı; `backtest.py:_make_index_instrument` pini). `last_updated: 2026-07-13`.
- **flag** — Paralel yol determinizmi teste bağlı: `tests/test_parallel_exec.py` parite testleri sıralı ve paralel WFO kazananlarının birebir aynı olduğunu doğrular; `NAUTILUS_PARALLEL=0` kill-switch her fonksiyonu dokunulmamış sıralı yola döndürür.

## 2026-07-13 — Ingest: docs/concepts @ v1.230.0

- **ingest** — `sources/06_concepts_docs_v1230.md`: resmî repo `v1.230.0` tag'inden (kurulu paketle birebir) 23 `docs/concepts` dokümanının yoğunlaştırılmış TR snapshot'ı. Her türetilen sayfanın frontmatter'ında 06 + kendi pinli raw URL'i (`raw.githubusercontent.com/.../v1.230.0/docs/concepts/...`). Önceki "latest/nightly URL sürüm-sabitleme" flag'i bu batch için çözüldü.
- **pages** — Stub → draft (5): [[live_node]], [[order_emulator]], [[data_wranglers]], [[bar_aggregation_and_type_syntax]], [[option_greeks_pipeline]]. Yeni sayfa (17) — entities: [[orders]], [[instruments]], [[positions]], [[order_book]], [[synthetics]], [[logging]]; concepts: [[accounting]], [[events]], [[event_sourcing]], [[configuration]], [[custom_data]], [[continuous_futures]], [[value_types]], [[plugins]], [[dst]], [[visualization]], [[reports]]. Hub sayfalarına (execution_engine, data_engine, portfolio, nautilus_kernel, event_driven_architecture, precision_modes, backtesting_guide) yeni sayfalara giden gövde linkleri eklendi. Lint: broken_links 0, orphans 0, **stubs 0**. Katalog 49 → 66 sayfa.
- **fix** — Upstream `dst.md` yaz saati değil **Deterministic Simulation Testing** anlatıyor; sayfa doğru başlıkla yazıldı, [[bar_aggregation_and_type_syntax]]'daki yanlış "yaz saati" bağlamlı link düzeltildi. [[execution_engine]] TIF listesine eksik AT_THE_OPEN/AT_THE_CLOSE eklendi. [[events]] tablo içi `\|` kaçışlı wikilink'ler bare forma çevrildi (lint bunları broken sayıyor — tabloda alias'lı link kullanma).
- **flag** — [[order_flow_pipeline]] ve [[execution_engine]] emir akışı Risk Engine'i tek aşama gösteriyor; upstream `orders/emulated.md`'ye göre pre-trade kontrol emulator tutuşundan ÖNCE ve release'te İKİNCİ kez çalışıyor — diyagrama iki-aşamalı risk notu eklenmeli (not şimdilik order_emulator'da).
- **flag** — [[venue_reconciliation]] "Only the LiveExecutionEngine performs reconciliation" atfını architecture'a veriyor; v1.230'da kaynak `concepts/live.md`. Sayfanın "Bilinen boşluklar" kalemlerinin çoğu (partial-fill recovery, lookback, retry politikası, 4 invariant) artık live.md'de belgeli — bu kaynaktan güncellenebilir.
- **flag** — StrikeRange'e v1.230'da 4. varyant eklendi: `Delta(hedef, tolerans)` + ATM±5 fallback; [[tutorial_options_data_bybit]] üç-varyant iddiası taşıyorsa güncellenmeli. Derive adaptörü resmî Greeks destek tablosunda yok (yalnız Deribit/Bybit/OKX) — [[tutorial_delta_neutral_derive]]'a uyumsuzluk notu düşülmeli.
- **gap** — Kullanım-düzeyi custom data rehberi (Cython `@customdataclass`, actor `publish_data/subscribe_data` örnekleri) v1.230 concepts doc'unda yok — API referans ingest'i gerektirir. Aynı şekilde `configuration.md` TradingNodeConfig/BacktestEngineConfig ayrıntılarını ve wrangler kurucu parametrelerini (`ts_init_delta` vb.) kapsamıyor.
- **gap** — [[events]] sayfası upstream `positions.md`'nin tanımladığı `PositionAdjusted` event'ini kapsamıyor; [[adapters]] sayfasında `MarginAccount.apply()` replace-not-merge konvansiyonuna atıf yok; [[crash_only_design]] event-store recovery (boot sweep) ile güncellenebilir; [[rust_python_hybrid]]'e `nautilus-plugin` crate cross-ref'i eklenebilir.
- **gap** — Gelecek ingest adayı: `how_to/configure_live_trading.md` (adım adım TradingNodeConfig kurulumu, çoklu-venue kablolama).
- **fix** — `tools/wiki_tools.py:_parse_yaml_lite` blok-skaler (`summary: >-`) desteklemiyordu; `backlinks` yeniden yazarken çok satırlı özet içeriği **sessizce siliniyordu** (CLAUDE.md şablonu tam bu formu öneriyor!). Parser'a `>`/`>-`/`|`/`|-` devam-satırı katlama desteği eklendi; bu batch'te silinen 22 özet tek satır formda yeniden yazıldı. Kural: özetler tek satır tutulabilir, ama `>-` artık güvenli.

## 2026-07-19 — Performans düzeltme turu senkronu

- **sync** — 10-maddelik perf turunun adversarial review'ı sonrası correctness/behavior düzeltmeleri koda uygulandı; wiki kod ile hizalandı. [[vol_targeted_trend]]: boyutlandırma bölümü artımsal EWMA (seed'li, `calc_ewma_vol` ile birebir) + O(1) MA koşan-toplam + 4096-bar drift reset olarak yeniden yazıldı; "Bilinen boşluklar"daki MTM şüphesi kaldırılıp throttled snapshot (`_MTM_SAMPLE=5`) bölümüyle değiştirildi. [[webapp_module_map]]: `parallel_exec` worker RAM guard (psutil→sysconf fallback), `data` footer-stats logical-type guard + `run_vtt` çift-decode giderme, `backtest` plan cache in-flight dedup + `allow_short` açık parse, `wfo_optimizer` FOLDS=3 satırları güncellendi; regresyon bölümüne perf-düzeltme turu + 321 test eklendi.
- **fix (kod, wiki-dışı)** — Turun kaçırdığı gerçek test regresyonu: thread-local `_get_bybit_session()` değişikliği `test_bybit_timestamps`'in `data.requests.get` monkeypatch'ini etkisiz bıraktı (kod artık `Session.get` çağırıyor); test session'ın `get`'ini patch'leyecek şekilde düzeltildi.
- **gap** — 3 golden-parity testi (test_perf_equivalence) + 1 orphan-reaping (Windows-only `ctypes.windll`) hatası perf turundan ÖNCE de kırık; bu senkronun kapsamı dışında, golden hash yenilenmesi ayrı iş olarak bekliyor.

## 2026-07-19 — Backtest panel yerleşimi + missing-day gzip sözleşmesi senkronu

- **sync (UI)** — Backtest ekranı yeniden yerleşti: plan preview (`#plan-preview`) sol formdan **sağ sütunun tepesine** taşındı; strateji üretim ilerleme kutusu (`fragments/describe_progress.html`) artık kökündeki `.bt-gen-float` class'ıyla `position:fixed` — **ekranın sağ-üst köşesinde yüzen overlay** (topbar altı `top:74px/right:24px`, `z-index:40`, `app.css`). `#result`'a swap + `every 1s` self-poll + chain akışı DEĞİŞMEDİ; sadece görsel konum. [[webapp_module_map]] Layout cümlesi güncellendi.
- **sync (data)** — `data.py:_stream_ticker_rows` boş/bozuk gzip'te `pd.errors.EmptyDataError` yakalayıp **boş tipli frame** döndürüyor (missing-day sözleşmesi: bozuk gün tüm yüklemeyi çökertmez); regresyon `tests/test_index_stream_empty.py`. [[webapp_module_map]] `data.py` satırına eklendi; docstring Wiki References zaten [[index_backtest_via_equity_proxy]] (tick→bar) linkli.
- Lint: broken_links 0, orphans 0, stubs 0, missing_summary 0. Katalog 67 sayfa.

## 2026-07-19 — Backtest plan-önerisi tetiği (düğme) + sticky + layout bug fix

- **sync (UI)** — Plan önizleme artık otomatik değil: describe textarea'sındaki `hx-trigger="keyup changed delay:800ms"` (ve `allow_short` checkbox otomatik yenileme) kaldırıldı → **"✨ AI önerisi al" düğmesi**. Kullanıcı denetimli; boş/yarım tarifle gereksiz LLM çağrısı önlenir. [[webapp_module_map]] "Canlı plan önizleme" cümlesi + agent.py notu güncellendi.
- **sync (CSS)** — `#plan-preview:not(:empty)` artık `position:sticky; top:70px` — dolu olunca sağ sütunun tepesinde sabit kalır, boşken sonuçların üstüne binmez (`app.css`).
- **fix (şablon, wiki-dışı bug)** — `backtest.html` sol `.stack` eksik `</div>` yüzünden kapanmıyordu → sağ `.stack` içine nested, `grid-2 equal`'ın 2. hücresi hiç oluşmuyor, sağ yarı boş kalıp plan-preview form altına düşüyordu (HEAD'den beri gizli). Eksik `</div>` eklendi; div dengesi net 0, served-HTML'de iki `.stack` de grid'in doğrudan çocuğu (depth 2) doğrulandı.
- Lint: broken_links 0, orphans 0, stubs 0, missing_summary 0, stale 0. Katalog 67 sayfa.

## 2026-07-19 — Vol-targeted trend → composer vol_target sizing modu

- **refactor** — Bağımsız `VolTargetedTrendStrategy` (strategies.py sınıfı + `STRATEGY_REGISTRY`/`STRATEGY_PARAM_SPEC` kaydı + `/backtest/run_vtt` route + `backtest.html` radio & `vtt-section`) tamamen kaldırıldı. Kullanıcı gerekçesi: "her şeye ayrı ekran istemiyorum". EWMA vol-hedefli boyutlandırma artık composer motorunun bir sizing modu: `TradeSizeMode` literaline `"vol_target"` eklendi; `ComposedStrategy._compute_qty`'de `size = (vol_target/ewma_vol)*capital/price` dalı (üst clamp `0.95*capital/price`, warmup'ta `trade_size` fallback). `calc_ewma_vol(self._closes, span)` yeniden kullanıldı (artımsal state değil — `_compute_qty` yalnız giriş sinyalinde çağrılıyor + eski artımsal yol ~%13 warmup sapması çıkarmıştı). `_buf_cap`'e `vol_span+5` tabanı.
- **spec** — `ComposedStrategySpec`'e `trade_size_vol_target`(0.02)/`trade_size_vol_span`(10)/`trade_size_capital`(10000) alanları + `from_dict`/`validate`. **capital SABİT** (canlı equity değil), Describe formundaki Initial Capital'dan. Round-trip (katalog + subprocess JSON) birim testle pinlendi.
- **UI** — `backtest.html` strateji-mode radyosu ve `vtt-section` silindi; Broker Settings altına "Pozisyon boyutu" dropdown'ı (Sabit/%Equity/ATR-hedef/Vol-hedef) + `btToggleSizeMode` JS. `/backtest/describe` sizing parametrelerini spec'e aktarır.
- **temizlik** — `agent.py` `_fallback_proposal` vol_targeted_trend dalı, `indicators.py`/`backtest.py` yorumları güncellendi. `calc_ewma_vol` kalıcı (artık composer kullanıyor).
- **test** — `TestComputeQty`'ye vol_target case'leri (warmup fallback, formül parite, üst clamp) + `TestVolTargetSpecRoundTrip`. E2E smoke: fixed vs vol_target aynı sinyal setinde (106 trade) farklı PnL → sizing gerçekten volatiliteye tepki veriyor. **NautilusTrader kütüphanesine dokunulmadı.**

## 2026-07-20 — Progress-bar "Reading data · parquet cache" takılması + Bybit cache metadata-only + fee-baking senkronu

- **fix** — Index/Equity backtest dalında `_progress("Reading data · …")` çağrısı parquet full-read'den SONRA geliyordu; blank tarih aralığında cache bounds keşfi için tüm parquet okunurken UI "read" fazında (`Reading data · parquet cache` label'ı) donuk kalıyordu (kullanıcı "Reading data · parquet cache takıldı" bildirdi, ekran görüntüsüyle doğrulandı). Çağrı parquet okuma ÖNCESİNE taşındı; Bybit dalına da metadata okumadan önce bir `_progress` eklendi → faz UI'da anında ilerliyor.
- **perf** — Bybit cache bounds keşfi FULL `pd.read_parquet` yerine pyarrow `ParquetFile` row-group METADATA istatistikleriyle (min/max timestamp) yapılıyor; 3M+ satır taranmıyor. Index kolonu `path_in_schema`'da `"index"` geçen kolon, stats yoksa `columns=[]` hafif fallback read. Blank tarihte varsayılan pencere 7 gün → `max(cache_start, cache_end−365d)` **365 gün cap** (Bar construction 3M+ satır churn etmesin diye).
- **refactor** — Komisyon override artık `_make_bybit_instrument(fee_bps_override=…)` ile instrument'a baked; `run_composed_backtest` ayrı `MakerTakerFeeModel` kurmaz (`_fee = _fee_model_for(instrument)`). `_bars_from_df` satır-satır append yerine vektörize `ts_close` + list-comprehension (davranış birebir).
- **frontend** — `base.html` `htmx.config.allowScriptTags=true`; `describe_progress.html` chain-tetiği `hx-trigger="load"`+hx-post yerine data-attr'lı `#bt-chain-trigger` (`data-bt-chain-url`/`data-bt-chain-vals`, JS ile tetiklenir); `plan_preview.html`'e `✕ Kapat` butonu (`#plan-preview` boşaltıp `#result`'ı `display=''` geri gösterir).
- **wiki** — `synthesis/webapp_module_map.md` (backtest.py fee-baking satırı + web/routes/backtest.py Bybit metadata/365g/progress-sıralaması + Sağlamlaştırma maddesi) ve `entities/parquet_data_catalog.md` (yeni "Parquet Cache Bounds Keşfi: Row-Group Metadata" bölümü + frontmatter tazeleme) güncellendi. `web/routes/backtest.py` docstring'ine `[[parquet_data_catalog]]` referansı + güncel chain-tetiği (data-attr) notu eklendi. Lint temiz (0 broken/orphan/stub). **NautilusTrader kütüphanesine dokunulmadı.**

## 2026-07-20 (6) — "Önce AI ile iyileştir" backtest sonuçlarını kullanıyor

- **feature** — `propose_refined_description` artık `backtest_metrics` alıyor; backtest PnL/Sharpe/DD/işlem/win-rate değerlerine bakarak stratejiyi hedefli iyileştiriyor. `agent.py`.
- **feature** — `backtest_result.html` + `sweep_progress.html`'e `data-bt-pnl-pct/sharpe/max-dd/n-trades/win-rate/spec-name/best-tf` attribute'ları eklendi. `backtest.html` `htmx:afterSettle`'da bunları okuyup `#bt-*` hidden input'lara yazıyor. `POST /backtest/plan` bu değerleri `bt_pnl_pct/bt_sharpe/...` form alanları olarak alıp `propose_refined_description`'a geçiriyor. `web/routes/backtest.py` + `web/templates/backtest.html`.
- **fix** — Plan cache key'e `bt_pnl_pct + bt_n_trades` eklendi — aynı tarif farklı backtest sonuçlarıyla farklı öneri üretir.

## 2026-07-20 (5) — Robustness "Analizi Çalıştır" HTMX process fix

- **bug/fix** — `#robustness-section` başta `display:none` render edilip JS ile `display:""` yapılıyordu. HTMX, DOM yüklenirken gizli olan elementlerin `hx-*` attribute'larını process etmez. `htmx.process(sec)` çağrısı eklendi — panel görünür hale gelince HTMX bindings aktif olur, "▶ Analizi Çalıştır" butonu artık `POST /robustness/run` tetikler. `web/templates/backtest.html`.

## 2026-07-20 (4) — Sweep sonrası robustness paneli açılmıyor bug fix

- **bug/fix** — 4 TF sweep tamamlanınca `#robustness-section` görünmüyordu. Kök neden: `htmx:afterSettle` listener'ı `#result` içinde `[data-rob-spec-id]` arar; `backtest_result.html`'de bu attribute vardı ama `sweep_progress.html`'de yoktu. Ayrıca `_sweep_state_view` / sweep store'da `spec_id` alanı hiç tutulmuyordu. **Düzeltme**: (1) sweep store create'e `spec_id` eklendi, (2) `_sweep_state_view` return dict'e `spec_id` eklendi, (3) `sweep_progress.html` `done` olunca `data-rob-spec-id/symbol/category/interval/start/end` attribute'larını panel div'ine yazar → mevcut `htmx:afterSettle` listener otomatik tetiklenir. `web/routes/backtest.py` + `web/templates/fragments/sweep_progress.html`.
- **wiki** — `synthesis/webapp_module_map.md` sweep state satırına `spec_id` notu eklendi.

## 2026-07-20 (3) — Backtest ④ Robustness Testi adımı

- **ui/feature** — `backtest.html` sağ sütunundaki Robustness paneli başlığına `.bt-step-no` badge'i (daire içinde "4") eklendi; panel adı "Robustness Analysis" → "Robustness Testi" olarak güncellendi. Backtest tamamlanınca `htmx:afterSettle` ile panel `display:""` yapılır — görsel akış artık ① Enstrüman · ② Strateji · ③ Çalıştır · **④ Robustness Testi**. `web/templates/backtest.html`.
- **wiki** — `synthesis/webapp_module_map.md` Layout satırı 4 adımlı akışı yansıtacak şekilde güncellendi.

## 2026-07-20 (2) — Bybit row-group index-kolonu bug + Index NFS-mount takılması senkronu

- **bug/fix** — Bybit cache bounds metadata-only optimizasyonu YANLIŞ kolonu okuyordu: `_rg0.column(0)` = `open` (fiyat), timestamp değil. `_ts0 = 5850.0` → `pd.Timestamp(5850.0)` = **1970-01-01** olarak yorumlanıyor, `cache_start` hatalı hesaplanıyordu (verify oturumunda Playwright + doğrudan pyarrow ile kanıtlandı). Fix: kolon `path_in_schema`'da `"index"` geçen kolonu (`__index_level_0__`, col[5]) ara → doğru aralık `2020-03-25 → 2026-07-20`. `web/routes/backtest.py`.
- **env/fix** — Index/Equity backtest "Reading data · <ticker>/<gran>…" adımında sonsuza takılıyordu. Kök neden: `INDEX_ROOT` (`/Users/…/Z` NFS mount) **erişilemez/asılı**; `load_index_bars` her `missing_day` (QQQ/15m ~3900 gün) için NFS'e stat/gzip stream deniyor, süresiz bloke. Kod bug'ı değil, ortam. Baypas: `NAUTILUS_INDEX_ROOT` var-olmayan yerel yola (`~/.cache/nautilus_web_app/_no_index_source`) → `src.exists()` anında False, backtest cache'teki bar'larla çalışır (247k bar, 180s+ → 0.59s). Kalıcılık: `.claude/settings.local.json` env + `~/.zshrc` export.
- **wiki** — `synthesis/webapp_module_map.md` `data.py` satırına "Index kaynağı NFS-bağımlı + baypas (2026-07-20)" maddesi eklendi (`load_index_bars`/`INDEX_ROOT`/`missing_days` davranışı + takılma kök nedeni + baypas reçetesi). Lint temiz. **NautilusTrader kütüphanesine dokunulmadı.**
- **wiki** — `synthesis/webapp_module_map.md`'ye "Backtest UX yeniden tasarımı: sekmeli sağ sütun + verdict kartı + geçmiş geri-yükleme (2026-07-20)" değişiklik günlüğü maddesi + `backtest.py` satırının **Layout** açıklaması güncellendi (grid-2 iki-`.stack` → sekmeli tek panel: Sonuç/Robustness/Geçmiş, `btTab`, `.verdict-card` otomatik-odak, `bt_results/<run_id>.json` snapshot store + `GET /backtest/result/{run_id}` birebir geri-yükleme, `_recent_runs` run_id + büyük-satır tail penceresi). backlinks (67 sayfa) + index yeniden üretildi; lint temiz (0/0/0/0/0/0). **NautilusTrader kütüphanesine dokunulmadı.**
- **wiki** — `synthesis/webapp_module_map.md`'ye "AI Plan ayrı sekme + robustness ilk-yükleme fix (2026-07-20)" maddesi + `backtest.py` Layout açıklaması 4 sekmeye (Sonuç · ✨ AI Plan · Robustness · Geçmiş) güncellendi. AI iyileştirme kendi sekmesine taşındı; robustness "Analizi Çalıştır" ilk-yükleme regresyonu `btFillPanels()` (afterSettle + DOMContentLoaded) ile düzeltildi (CDP doğrulaması). Lint temiz. **NautilusTrader kütüphanesine dokunulmadı.**

- 2026-07-21 `fragments/robustness_result.html` — Robustness sonuç ekranı görsel yenileme: hero karar kartı (renk kodlu ROBUST/DİKKAT/OVERFITTING banner), tüm KPI kartlarında eşik-renkli değerler, IS/OOS overfitting eşik düzeltmesi (0.7/0.4 → 0.5/0.25), her sekmeye açıklama kutucuğu. [[webapp_module_map]] güncellendi.
- 2026-07-21 **wiki** — `synthesis/webapp_module_map.md` `backtest.py` satırına "Robustness-farkındalık (2026-07-21)" + "Cache düzeltmesi" maddeleri eklendi. Kök neden: robustness sonrası "✨ AI ile iyileştir" önerisi değişmiyordu çünkü robustness verisi `/backtest/plan` endpoint'ine hiçbir kanaldan gitmiyordu. Çözüm `data-bt-*`/`btFillPanels` desenini izler: `robustness_result.html` kök `<div data-rob-summary>`'ye `data-rob-*` özet (overfitting_score/verdict/wfo_efficiency/OOS Sharpe/stability) + inline writeback script → `backtest.html` 5 yeni hidden input → `plan_preview` `rob_*` Form alanları → `propose_refined_description(desc, backtest_metrics, robustness)` overfitting-farkındalıklı öneri (skor<0.4 sadeleştir, ≥0.7 güçlendir). `refined_result` artık hiç cache'lenmez (her basış taze); yeni backtest'te `btFillPanels` eski `rob_*` inputlarını temizler. backlinks (67 sayfa) + index yeniden üretildi; lint temiz (0/0/0/0/0/0). **NautilusTrader kütüphanesine dokunulmadı.**

- 2026-07-21 **wiki** — `synthesis/webapp_module_map.md` iki değişiklik günlüğü maddesi + `agent.py` notu: (1) **Robustness giriş formu yeniden tasarımı** — Robustness sekmesindeki tek-satır ızgara üç `<fieldset>` gruba (Walk-Forward / In-Out-of-Sample / Monte Carlo) ayrıldı; her alanın altında `.field-hint` (ne olduğu + önerilen aralık, `robustness.py run()` clamp'leriyle birebir) + `.rob-form-intro` + `robPreset()` ile 3 preset (⚡Hızlı/⚖Dengeli/🔬Titiz); yeni CSS `app.css`. (2) **"AI ile iyileştir" butonu AI Plan sekmesine taşındı** — "✨ Önce AI ile iyileştir" sol formdan (③ Çalıştır) AI Plan boş-durum kartına taşındı, adı "✨ AI ile iyileştir" oldu; `hx-include="closest form"` → `#gen-form` (buton artık form dışında), `#plan-spinner` de taşındı. (3) **refine prompt** — `_REFINE_SYSTEM_PROMPT` artık "her zaman ≥2 somut öneri" der (eski "netse aynen döndür" kaldırıldı). Backend/endpoint/veri sözleşmesi değişmedi; salt sunum + istemci JS + prompt metni. backlinks (67 sayfa) + index yeniden üretildi; lint temiz (0/0/0/0/0/0). **NautilusTrader kütüphanesine dokunulmadı.**

## [2026-07-21] query | Runtime-performans denetimi sentezlendi
nautilus_web_app'in çok-ajanlı (64 ajan, çekişmeli-doğrulama) runtime-performans denetimi sentez
sayfasına doslandı: [[nau_performans_denetimi]]. 50 ham → 31 doğrulanmış darboğaz. 4 HIGH: LLM
prompt-caching yok (en yüksek ROI, S efor), state.py iterations sınırsız, sandbox subprocess
cold-boot, composer NAU-indikatör per-bar full-recompute. En sert kısıt: NAU_WINDOW=260 sabit-pencere
paritesi incremental indikatörleri bit-parite (<1e-9) testine bağlar. Doğrulama birkaç cazip fix'i
reddetti (elle min/max %15 daha yavaş, equity örnekleme metrik bozar, thread-executor killability
kaybı). [[webapp_module_map]] "Sağlamlaştırma" bölümüne özet + karşılıklı köprü eklendi. Bu tur kod
değiştirmedi — anlık görüntü. NautilusTrader kütüphanesine dokunulmadı.

- 2026-07-21 **code-review** — 5c264ca commit'i (robustness form yeniden tasarımı + "AI ile iyileştir" sekme taşıması) xhigh-effort çok-açılı review'dan geçirildi. 12 bulgu kesinleşti. **Kritik (CONFIRMED):** (1) `plan_preview.html:69` `hx-include="closest form"` kırık — fragment `#gen-form` kapandıktan sonra DOM'a inject ediliyor, "Tamam — Blokları Oluştur" POST'u form verisi olmadan gidiyor; düzeltme `hx-include="#gen-form"`. (2) `backtest.py:1797` `bd` exception → `raise bd` → outer except refined_result'ı sıfırlıyor, bd hata alsa bile başarıyla dönen AI önerisi kayboluyor. (3) `backtest.py:1819` `raise bd` yolu `fut.set_result`'u atlıyor → in-flight future asılı kalıyor, eşzamanlı requestler cevap alamıyor. (4) `backtest.py:1771` in-flight dedup ikinci isteğe birinci isteğin metrics'iyle hesaplanmış refined_result'ı paylaşıyor ("refined hiç cache'lenmez" yorumu yalnız _PLAN_CACHE'i kastediyor). (5) `backtest.html:395` robustness form `hx-indicator` eksik, 30-60s analiz sırasında double-submit mümkün. (6) `backtest.html:562` `btFillPanels` spec_id/bybit_start/bybit_end querySelector null-guard eksik. **PLAUSIBLE:** (7) `plan_preview.html:70` hx-vals js: expression, refined-description DOM'dan giderse sessiz TypeError/POST iptali. (8) `backtest.html:641` querySelector unquoted attribute selector kırılgan. (9) `backtest.py:1736` rob_stability guard listesinden dışarda. **Cleanup:** (10–12) robPreset idx haritası pozisyon bağımlı, double querySelectorAll, Kapat her zaman btTab('result'). Kod değişmedi — bulgular raporlandı.

## [2026-07-21] query | Mimari denetim sentezlendi
nautilus_web_app'in çok-ajanlı (58 ajan, 0 hata, çekişmeli-doğrulama) mimari/yapısal denetimi
sentez sayfasına doslandı: [[nau_mimari_denetimi]]. 45 ham → 41 doğrulanmış yapısal defekt.
**Kritik yok.** Baskın sorun: niyet düzeyinde kalan katmanlama (her sınır sızıyor) + 4 tanrı-modül
(agent_backtest 2923 / composer 2246 / backtest 2286+1978 / data 1996) + fonksiyon-içi import
döngüleri (server↔routes 43 late-import, backtest↔data, sandbox→web.routes). En yüksek 3+1: (H1)
engine sandbox.py web'e yukarı uzanır — _IPC_Q global mutasyonu + private _run_full_robustness
çağrısı, ~250 satır orkestrasyon route dosyasında; (H2) WFO batch timeout 900s dış kill altında
kümülatifi sınırlamaz (n_gen=5 → ~3600s), tamamlanmış suite çöpe; (H7) codegen doğrulaması
agent.py private'larından — `from agent import _has_builtin` (strategy.py:440) ZATEN canlı
ImportError; (H9) global tek-instance AppState, session izolasyonu yok. Diğer HIGH: H3 servis-dikişi
yok (dispatch 5+ yerde sapmış), H8 sıralı/paralel WFO GA sadece parity-yorumuna bağlı, H10 built-in
block 4-site shotgun. Doğrulama data.py load_index_bars hacim-sıfırlamayı KASITLI olarak REFUTED etti.
[[webapp_module_map]] "Sağlamlaştırma" bölümüne özet + karşılıklı köprü eklendi. Bu tur kod DEĞİŞTİRMEDİ
— denetim anlık görüntüsü. NautilusTrader kütüphanesine dokunulmadı.

## 2026-07-21 — "AI ile iyileştir" → çok-turlu sohbet senkronu

Backtest ✨ AI Plan sekmesindeki tek-atışlık "AI ile iyileştir" çok-turlu sohbete dönüştürüldü (single-shot korundu, sohbet yanına eklendi). `webapp_module_map` güncellendi: `agent.py` satırına `chat_refine`/`_CHAT_SYSTEM_PROMPT`/`_format_metrics_block`/claude-cli multi-turn join (len==1 guard), `web/routes/backtest.py` satırına `_CHAT_STORE`+`/backtest/chat*` endpoint, ve değişiklik günlüğüne 2026-07-21 girdisi. Lint: 1 broken_link (cross-vault `single_shot_to_chat_donusum` → düz metne çevrildi) → 0. Canlı LLM doğrulaması + 315 test yeşil (12 baseline-kırık golden-parity/describe git stash ile izole edildi).

## 2026-07-21 — Composer + Backtest → birleşik /studio sayfası (senkron)

A-UI + C-backend birleştirmesi 5 fazda uygulandı, webapp_module_map güncellendi. Yeni web/routes/studio.py + studio.html sekmeli [Compose][Backtest][Analyze] (.lab-tab namespace, backtest iç .bt-tab'e dokunmaz); /strategy+/backtest kök → 307 /studio redirect. Faz 0 render_md→web/shared (3 kopya sil); Faz 1 composer.build_spec() spec-upsert servisi + sizing drift bug fix (vol_target artık round-trip); Faz 2 backtest _CHAT_STORE→ortak ChatStore; Faz 3 _sid→web.shared.session_id + _LAST_RESULT session-scoped; Faz 4 studio template + partial'ler (compose_body/backtest_body/backtest_scripts) + catalog spec-picker (ölü preferred_spec_id devri canlandı). _preview_signals bilinçli birleştirilmedi. 105 ilgili test yeşil, driver+TestClient doğrulama OK, ruff temiz, lint temiz. Modül tablosuna studio.py satırı + strategy/backtest/composer/shared notları eklendi.

## 2026-07-22 — Blokları AI ile düzenleme (çok-turlu sohbet) senkronu

`chat_refine` deseni bloklara taşındı: (A) custom block KODU + (B) strateji draft LİSTESİ, ikisi de çok-turlu sohbetle düzenlenebilir. `webapp_module_map` güncellendi: `agent.py` satırına `chat_edit_block`/`chat_edit_blocks` + `_BLOCK_EDIT_SYSTEM_PROMPT`/`_BLOCKS_EDIT_SYSTEM_PROMPT`/`_coerce_catalog_blocks` ([NET_KOD]/[NET_BLOKLAR] protokolleri, codegate defense-in-depth, halüsinasyon-tip önleme); `web/routes/strategy.py` satırına A/B chat endpoint'leri (`/blocks/{name}/chat/new|chat|chat/save` — 409-bypass + unregister/register; `/drafts/chat/new|chat|chat/apply`), `_BLOCK_CHAT`/`_DRAFTS_CHAT = ChatStore()` ayrı örnekler, `_preview_signals` ortak yardımcı; değişiklik günlüğüne 2026-07-22 girdisi. Doğrulama (HTTP surface, verify skill): tüm sunucu-tarafı yol + guard'lar (404/400/409/expired) PASS; save yolu codegate+smoke+register+HX-Redirect canlı çalıştı. Canlı LLM happy-path gözlenemedi — sağlayıcı ısrarlı 429 PROVIDER_RATE_LIMIT_EXCEEDED (ortam kaynaklı); feature 429'u nazik fallback + çökme-yok ile karşıladı. NautilusTrader kütüphanesine dokunulmadı.

## 2026-07-23 — Studio hata düzeltme turu senkronu (6 bug + Simple wizard geriye dönük)

Kullanıcının önceliklendirilmiş 6-maddelik Studio hata raporu (duplicate-submit yarışı,
result-panel JS SyntaxError, SIMPLE wizard gating yok, tarih validasyonu eksik, uzun-koşu
UI koruması zayıf, Result rozeti kararsız) tek turda kapatıldı; `webapp_module_map` senkron
edildi. (1) `web/routes/backtest.py` satırına: `_ACTIVE_RUNS` session-başına tek-aktif-koşu
guard'ı (409 + HX-Toast, self-clearing + 500-eşik prune, lock-nesting'siz) + `_invalid_date_range`
sunucu validasyonu (worker başlamadan 400). (2) `web/routes/studio.py` satırına **geriye dönük**:
2026-07-22 SIMPLE ↔ PRO ↔ AUTO mod anahtarı + `studio_simple.html` 5-adımlı sihirbaz (singleton
reparenting `swMoveNodes`) — önceki oturumda commit'lenmiş ama wiki'ye hiç senkron edilmemişti —
+ 2026-07-23 adım gating (`btDone/robDone`, backtest zorunlu, reliability açıkça opsiyonel).
(3) Değişiklik günlüğüne 2026-07-23 girdisi: 6 bug'ın kök neden + düzeltme + test özeti
(yeni `tests/test_studio_ui_fixes.py` 11 test; describe'daki 7 kırık git-stash ile baseline'da
da kırık diye izole edildi; code-review'un tek 🟡 bulgusu — registry sınırsız büyüme — prune
ile kapatıldı). Frontmatter last_updated 2026-07-23 + summary tazelendi. NautilusTrader
kütüphanesine dokunulmadı — salt route + şablon/JS/CSS.

## [2026-07-23] update | Bybit TF seti + wizard katalog sembolleri
- Interval seti 1/5/15/60/240/D → 1/5/15/30/60/240/720/D (30m + 12h; 45m Bybit API'de yok).
- Kapalı-set kopyaları senkron: data.py (_BYBIT_MS/Literal/ALL_INTERVALS), backtest.py
  (_make_bybit_bar_type/_BYBIT_TO_DSL), chart.py (saniye map ×2), price_chart.html TF şeridi.
- Simple wizard Market adımı: coin butonları bybit_symbols (katalog) + 8 TF butonu.
- webapp_module_map.md data.py + studio.py satırları güncellendi; test_fixes.py
  unsupported-örneği "30"→"45" taşındı (45 gerçekten desteklenmiyor).

## 2026-07-23 — Ops araçları senkronu (wiki-sync skill kurulum script'i)

Son senkron (1e148b4) sonrası iki chore commit tarandı: (1) `scripts/
install-wiki-sync-skill.ps1` (38e2f5b) — wiki-sync skill dosyalarını gist'ten
`~/.claude/skills/wiki-sync`'e kuran Windows kurulum script'i → `webapp_module_map`
Ops tablosuna satır eklendi (wiki iş akışının kendi kurulum aracı olduğu için
kapsam-içi). (2) `.claude/hooks` ikinci-beyin vault yolu makine-bağımsızlaştırma
(642c955) — dev-ortam konfigürasyonu, wiki kapsamı DIŞI (app modülü değil) →
bilinçli atlandı, yalnız bu notla kayda geçti. App kodu farkı sıfır
(`git diff 1e148b4..HEAD -- '*.py' web/` boş); lint öncesi/sonrası 0/0/0/0/0/0.

## 2026-07-24 — Kalıcı per-model token ledger (kod + wiki senkronu)

Kullanıcı "hangi modelde ne kadar token tüketildi listele" istedi; mevcut izleme
bunu veremiyordu (bkz. ikinci-beyin nau_token_tuketim_izleme: AUTO-only, bellekte,
tek-model, kalıcı değil). Yeni `token_ledger.py` eklendi: `agent._create_message`
merkezî çıkış-noktasına `_ledger_record` hook'u — her başarılı LLM çağrısını
token_usage.jsonl'e yazar, model=resp.model (fallback doğru atfedilir). 4 doğrudan
narrative/summary çağrısı (_create_message'ı baypas eden backtest/lab×2/agent_backtest)
wrapper'a yönlendirildi. Rapor: summary/format_table + CLI + GET /agent/tokens.
webapp_module_map: agent.py satırına ledger-hook notu, Ops tablosuna token_ledger.py
satırı, değişiklik günlüğüne 2026-07-24 girdisi. Testler test_token_ledger.py 7/7 +
regression 68/68, ruff temiz. NautilusTrader'a dokunulmadı.

## 2026-07-25 — Strategy Studio birleştirmesi + Portfolio.equity() tuzağı

- **Yeni sayfa** `wiki/synthesis/strategy_studio.md` — /studio/{id} görsel strateji
  kurucusu: katmanlar (şema → derleyici → to_nautilus → composer spec → runner),
  sessiz-atlama yasağı ve reddedilenler listesi, indikatör köprüsü sözleşmesi
  (`impl(bars, **schema_params)`), iki motor anahtarı ve maliyet gerekçesi,
  metriklerin kaynağı (neden Sharpe işlem bazlı, dsr neden PSR), AI guardrail
  baseline'ının hangi motorda ölçüldüğü.
- **`wiki/entities/portfolio.md`** — yeni bölüm: `Portfolio.equity()` skaler değil
  `dict[Currency, Money]` döndürür (v1.230.0'da doğrulandı). `float(dict)` TypeError'ı
  geniş bir `except` içinde yutulunca kod bakiye taramasına düşüyor; CASH hesapta o
  bakiye harcanmamış nakit olduğundan MTM equity serisi pozisyon açıkken çöküyor.
  Ölçülen etki tabloyla kayda geçti (max_dd -%94.27 → -%2.99).
- **`wiki/synthesis/webapp_module_map.md`** — iki yeni satır (`web/routes/strategy_studio.py`,
  `strategy_studio/` paketi); `composer.py` satırına `_current_equity` düzeltmesi işlendi;
  frontmatter tazelendi.
- **Kod → doküman köprüsü**: `strategy_studio/` altındaki 9 modüle `Wiki References`
  bloğu eklendi (ters yön modül haritasında zaten var).
- Lint öncesi/sonrası temiz (0/0); backlinks 70 sayfada tazelendi; index yeniden üretildi.

**Açık boşluk:** `dsr` gerçek DSR'a deflate edilmiyor (deneme sayısı optimizer
entegrasyonundan gelecek); `walkforward.scheme`/`in_sample_months`/`oos_months`
UI'da ayarlanabiliyor ama fold dilimlemesi yalnız `folds` + `embargo_bars` kullanıyor.

## 2026-07-25 (2) — Studio HTTP semantiği

- **`wiki/synthesis/strategy_studio.md`** — yeni bölüm "HTTP semantiği: 404 kaynak,
  422 girdi". HTMX yüzeyinde durum kodu bir UX kararı: 2xx dışını HTMX swap etmez,
  yanlış kod kullanıcıya "buton hiçbir şey yapmadı" olarak görünür. Kaynak-yok
  artık dört uçta da 404 (`RuleNotFound`, `MutationError` alt sınıfı — mevcut
  `except MutationError` işleyicileri bozulmadan ayırt edilebiliyor); geçersiz
  girdi 422; beklenmedik motor hatası 422 + okunabilir mesaj (`_trial_baseline`
  hatayı bilerek yukarı bırakıyor, route yakalıyor).
- **`wiki/synthesis/webapp_module_map.md`** — `web/routes/strategy_studio.py`
  satırına aynı semantik notu işlendi.
- **Kod → doküman köprüsü**: route modülünün `Wiki References` bloğu artık
  `[[strategy_studio]]` + `[[portfolio]]`'ya işaret ediyor (detay sayfası bir
  önceki senkronda oluşmuştu).
- Lint öncesi/sonrası temiz (0/0); backlinks 70 sayfada tazelendi; index yeniden üretildi.

**Not:** Bu senkronun deltası küçüktü — son senkrondan (025b527) beri tek kod
commit'i vardı (b834d86). Açık boşluklar bir önceki girdideki gibi duruyor.

## 2026-07-25 (3) — IP#3: walk-forward optimizer

- **`wiki/synthesis/strategy_studio.md`** — yeni bölüm "Walk-forward optimizer".
  Stub tam örneklemde grid koşup aynı örneklemin metriğine göre sıralıyordu:
  seçim de değerlendirme de aynı veride, yani sıralamanın kendisi in-sample'dı.
  Yeni akış iki aşama — ankrajlı IS eleme + purged OOS fold'ları — ve ikisi de
  adaptörün `run(compiled, window=Window(...))` kesitlerinde koşar. Sıralama
  `mean − 0.5·std` (host `wfo_optimizer` geleneği); `dsr`/`sharpe` işlem
  sayısıyla sönümlenir, `max_dd` bilerek sönümlenmez.
- Aynı sayfada iki boşluk kapandı, ikisi de yerine daha dar boşluk bıraktı:
  `dsr` optimizer sonuçlarında artık gerçekten deflate ediliyor (benchmark
  `expected_max_sharpe(σ_trial, N)`) ama **tek koşununki hâlâ PSR** — deploy
  kapısı orayı okuyor; `in_sample_months`/`oos_months` artık iş yapıyor ama
  takvim uzunluğu değil **oran** olarak, literal olmaları `lookback_days`'e
  bağlı. Panelin ayın yanına payı yazması (`%33 of sample`) bu yüzden var.
- **Ayrım kayda geçti**: tek koşunun fold tablosu (purged k-fold, kendi
  örneklemi) ile optimizer'ın IS/OOS bölmesi iki ayrı şey — `in_sample_months`
  ilkine ait değil. Önceki senkronda ikisi tek boşluk gibi yazılmıştı.
- **`wiki/synthesis/webapp_module_map.md`** — `strategy_studio/` satırında
  `optimizer.py` artık gerçek; `web/routes/strategy_studio.py` satırında sweep
  maliyeti `min(sweep, 400) × (1 + folds)` üst sınırı olarak düzeltildi (eski
  "~1600 koşu" sabit sayısı pencereli koşuyla geçersiz); `wfo_optimizer.py`
  satırına **studio'nun onu neden yeniden kullanmadığı** işlendi (GA
  `BLOCK_REGISTRY` sınırlarından uzay türetir, studio'nun uzayı kullanıcının
  yazdığı min/step/max — GA ekrandaki aralıkların dışını optimize ederdi).
- Kod → doküman köprüsü: `optimizer.py`/`backtest.py` `Wiki References`
  blokları zaten `[[strategy_studio]]`'ya işaret ediyordu, içerikleri o sayfayla
  hizalandı.
- Lint öncesi/sonrası temiz (0/0); backlinks 70 sayfada tazelendi; index
  yeniden üretildi; `lint/2026-07-25_health.md` yazıldı.

**Açık boşluk:** Deflasyon yayılım ölçmeyi gerektiriyor — tek aday skorlayan
sweep sessizce deflate edilmemiş PSR'a düşüyor. Beşinci INTEGRATION POINT
(`deploy.py` → gerçek TradingNode hand-off) hâlâ stub.

## 2026-07-25 (4) — IP#5: koşulabilir artifact + paper runner

- **`wiki/synthesis/strategy_studio.md`** — yeni bölüm "Deployment: koşulabilir
  artifact + paper runner". İki iş: (1) artifact artık stratejinin KENDİSİNİ
  taşıyor — `to_nautilus`'un indirdiği `ComposedStrategySpec` — eskiden yalnız
  koşul sayımları vardı, yani beşinci entegrasyon noktası bağlanacak runner'dan
  ÖNCE, ortada koşulabilir belge olmadığı için tıkalıydı; (2) `PaperRunner`
  artifact'i sandbox `TradingNode`'una indiriyor (canlı Bybit verisi,
  `SandboxExecutionClient`, kimlik bilgisi yok).
- **`environment='live'` reddi kayda geçti** — sessizce paper koşmak yerine
  gürültülü ret, iki gerekçesiyle: borsa kimlik bilgisi yok ve deploy kapısı
  hâlâ deflate edilmemiş tek-koşu DSR'ını okuyor.
- **Ölçümle bulunan dört tuzak** ayrı bir alt bölüm oldu; hiçbiri gerçek node'a
  bağlanmadan görünmezdi: node kurulduğu loop'a bağlanır (yanlış thread'de
  kurmak data client'ı hiç bağlamıyor, 60 sn sonra timeout);
  `product_types=None` = tüm Bybit ürünleri (option dahil) ve bağlantı hiç
  tamamlanmıyor; 'running' tek seferlik bir iddiadır, sonradan ölen node satırı
  yeşil bırakıyordu; durmuş bir Nautilus bileşeni START'ı reddeder — UI'daki
  Resume düğmesi hiçbir şey yapmıyordu, doğru geçiş RESUME.
- **Durum bölümü**: beş INTEGRATION POINT'in beşi de bağlı. Kalan iş entegrasyon
  değil kapsam (canlı emir yolu bilerek açılmadı). Üç motor anahtarının da
  opt-in olma gerekçesi tek yerde toplandı.
- **`wiki/synthesis/webapp_module_map.md`** — `strategy_studio/` satırına
  `deploy.py` (koşulabilir artifact) ve yeni `runner.py` işlendi.
- Lint öncesi/sonrası temiz (0/0); backlinks 70 sayfada tazelendi; index
  yeniden üretildi; `lint/2026-07-25_health.md` yazıldı.

**Yeni açık boşluklar:** deployment node'ları süreç-içi (sunucu yeniden
başlarsa koşan her deployment `failed`'a düşer — kalıcılık ayrı bir runner
süreci ister); `kill_switch_daily_pct` artifact'te taşınıyor ama node tarafında
uygulanmıyor; paper runner sandbox dolumlarını/PnL'ini panele raporlamıyor.

## 2026-07-25 (5) — eski sweep sonuçlarının render'ı (e427aa8 senkronu)

- **Kapsam boşluğu kapatıldı:** e427aa8 iki wiki senkronunun arasına düşmüştü —
  IP#3 senkronu ondan önceydi, IP#5 senkronu yalnız deploy/runner'ı işledi.
- **`wiki/synthesis/strategy_studio.md`** — "Walk-forward optimizer" bölümüne
  yeni paragraf: walk-forward'dan önce kaydedilmiş optimize koşuları
  `score`/`folds_valid`/`trials` alanlarını sıfırla yüklüyor ve yeni panel
  düzeni onları "DSR 0.000 · 0 folds · 0 trials" olarak basıyordu — eski sonuç
  değil bozuk panel gibi görünüyor. Tek karar noktası `OptResult.walk_forward`
  (`trials > 0`); eski koşular eski düzende, "in-sample" rozetiyle kalır.
  Ders kayda geçti: rehydrate ≠ render — eski JSON'un yüklendiğini test etmek,
  nasıl göründüğünü test etmek değildir.
- Modül haritasına dokunulmadı (detay sayfası zaten `Detay: [[strategy_studio]]`
  ile bağlı; satırı şişirmeye değer yeni bilgi yok).
- Lint öncesi/sonrası temiz (0/0); backlinks 70 sayfada tazelendi; index
  yeniden üretildi.

**Not:** Delta tek commit'ti (e427aa8); açık boşluklar bir önceki girdideki
gibi duruyor (node'lar süreç-içi, kill switch uygulanmıyor, sandbox dolumları
panele raporlanmıyor).

## 2026-07-25 — Güvenlik & dayanıklılık düzeltme geçişi

Beş paralel incelemenin (kod/mimari/performans/test/E2E) kritik ve yüksek
bulguları kapatıldı; yeni sayfa: [[nau_guvenlik_dayaniklilik_duzeltmeleri]].

- **Kritik:** codegate sandbox kaçışı (attribute Store/Del reddi + salt-okunur
  modül proxy'si; 233 diskteki blok yeniden doğrulandı, 0 regresyon).
- **Yüksek:** custom_block_store registry kaybı (I/O hatası artık fırlatılır,
  yalnız parse hatası karantina) + `load_catalog` budamayı durdurur;
  index/ticker path traversal; Studio motorunun web sürecini dondurması
  (artık `run_backtest_guarded(force_subprocess=True)`); deploy geçidinin
  taslak koşusuyla atlatılması (`studio_runs.defn_hash` içerik hash'i).
- **Orta:** robustness eviction KeyError, optimize grid'inin max'ı aşması,
  `kill_switch` 500→422, `store.save()` versiyon yarışı (BEGIN IMMEDIATE),
  SQLite bağlantılarının kapanmaması, hızlı yolun sermaye/komisyon düşürmesi,
  BacktestPool zombie süreçleri, AI döngüsünün kullanıcı düzenlemesini ezmesi.
- **Windows:** index veri hattı `bash|gunzip|awk` yerine saf Python (gzip+csv)
  — bu hat Windows'ta hiç çalışmıyordu.
- **Test:** 518 geçen/12 hatalı → **539 geçen / 0 hatalı** (12 hata giderildi,
  9 yeni regresyon testi). Eskimiş 7 describe testi yeni chain sözleşmesine
  taşındı; 3 golden hash yalnız ekonomik alanlara daraltıldı (pnl/n_trades
  değişmediği doğrulandıktan sonra yeniden üretildi).
- E2E: driver ile 525.600 barlık gerçek backtest PASS; tüm ana sayfalar 200.
- Backlinks 70 sayfada tazelendi; index yeniden üretildi.

**Not:** `defn_hash` migrasyonu geriye dönük NULL'dur — geçit *açık* bir deploy,
o strateji için bir kez yeni backtest ister (hata güvenli yönde).

## 2026-07-26 — İkinci tur inceleme bulguları

Düzeltme turunun ardından yapılan bağımsız incelemenin 4 bulgusu ele alındı;
detay: [[nau_guvenlik_dayaniklilik_duzeltmeleri]] bölüm 9.

- **ORTA — `promote_draft` atomik değildi**: load → save → delete üç ayrı işlemdi;
  araya düşen `save_draft` sondaki delete tarafından siliniyordu (kullanıcının en
  yeni düzenlemesi sessizce kayboluyordu). Tek `BEGIN IMMEDIATE` işlemine alındı,
  silme okunan json'a koşullandırıldı, ortak gövde `_insert_version`'a çıkarıldı.
- **ORTA/DÜŞÜK — sweep'ler bayat adaptörle koşabiliyordu**: `OPTIMIZER` adaptörü
  import anında yakalıyordu; `_optimizer()` artık güncel `TRIAL_ADAPTER`'ı çözüyor.
- **DÜŞÜK — `ruff check .` tüm repoda düşüyordu**: vendor edilmiş `.claude` skill
  script'leri yüzünden; `.claude` ruff extend-exclude'a eklendi (CI artık geçer).
- **ORTAM — EXTERNAL_CATALOGS uyarısı**: bug değil, varsayılan yol başka makineyi
  gösteriyor. Mesaj eyleme dönüştürüldü (hangi env değişkeni, ne zaman yok sayılır).
- **Kapatılmadı (bilinçli)**: Studio route bağımlılıklarının import-anı kurulumu —
  somut kusuru (bayat adaptör) düzeltildi, DI/factory refactor'ü tasarım değişikliği
  olduğu için kullanıcıya bırakıldı. Bkz. bölüm 9.5.

**Yöntem notu:** promote_draft için ilk yazılan thread-yarışı testi eski kodda da
geçiyordu (boşluk mikrosaniye mertebesinde) — ayırt edici olmadığı için atıldı;
yerine "tek bağlantı" invaryantı ölçülüyor, eski kodda 3 != 1 ile düşüyor.

Testler: 539 → **545 geçen / 0 hatalı**. `ruff check .` (tüm repo) temiz.

## 2026-07-26 (ek) — modül haritası ikinci tura yetiştirildi

Bir önceki senkron (03bf9b6) ikinci tur bulgularını sentez sayfasına işlemiş ama
[[webapp_module_map]] tablosunu güncellememişti — kod↔doküman köprüsünün yarısı
eksik kalmıştı. Kapatıldı:

- `strategy_studio/` satırı: `promote_draft`'ın atomikliği (üç ayrı işlem → tek
  `BEGIN IMMEDIATE`, silme okunan json'a koşullu, ortak gövde `_insert_version`).
- `web/routes/strategy_studio.py` satırı: `OPTIMIZER`'ın import anında yakaladığı
  bayat adaptör ve `_optimizer()` çözümü; modül-düzeyi kurulumun bilinçli olarak
  korunduğu §9.5'e bağlandı.
- Değişiklik günlüğüne 2026-07-26 turu (4 bulgu + kapatılmayanın gerekçesi +
  test yöntemi notu).

Lint öncesi/sonrası temiz (0/0); backlinks 71 sayfada tazelendi, index yenilendi,
sağlık raporu `lint/2026-07-26_health.md` olarak dosyalandı.

## 2026-07-27 — wiki-sync: serve.py modül haritasına girdi (kod → wiki)

- **boşluk** — `serve.py` modül haritasında yoktu; dosyanın kendi docstring'i
  `Bkz: [[webapp_module_map]], [[nautilus_kernel]]` diyordu ama harita karşılık
  vermiyordu — iki yönlü köprü tek yönlü kalmıştı. Dosya aynı zamanda **git'te
  izlenmiyordu**: PM2'nin `dump.pm2` kaydı `C:\myAI_Projects\NAU_v18Jul\serve.py`
  yolunu işaret ederken dosya versiyon kontrolünde değildi, yani makine
  sıfırlansa giriş noktası kaybolurdu. Bu senkronla git'e alındı.
- **eklenen** — `webapp_module_map.md`'ye `serve.py` satırı: süreç sarmalayıcısı
  rolü (kurmaz, başlatır), `reload=False`'un neden bilinçli olduğu (worker
  thread'deki bellek durumu), yalnız-loopback bağlanma kararı.
- **operasyonel not** — Tünel ingress'i `localhost` değil `127.0.0.1` yazmalı:
  uvicorn yalnız IPv4 dinliyor, `localhost` önce `::1`'e çözülüp reddediliyor.
  Bu, 2026-07-27'de `nautilus.muratben.com`'un 502 vermesinin iki nedeninden
  biriydi (diğeri: ingress 8111 yerine 3700'ü gösteriyordu).
- **studio** — İndikatör kütüphanesi paneli işi (26 Tem) bu commit'le kod tarafında
  da yerine oturdu; wiki karşılığı `synthesis/strategy_studio.md`'de zaten yazılıydı.

## 2026-07-27 (2) — wiki-sync: Strategy Builder canvas görünümü

Kod tarafında dört faz halinde uygulanan canvas görünümü (`/studio/{id}/canvas`)
wiki'ye yansıtıldı. Tasarım dokümanı repo kökünde `CANVAS_DESIGN.md`.

- **eklenen** — `synthesis/strategy_studio.md` → "Canvas görünümü (2026-07-27)"
  bölümü: kütüphane seçmeme gerekçesi (kısıtlı ağaç ≠ serbest graf), üç GET
  route, "yazan her şey mevcut uca iner" kuralı, iki partial-yeniden-kullanım
  kısıtı, ghost'ların argüman olarak gelmesi.
- **güncellenen** — `synthesis/webapp_module_map.md`: `web/routes/strategy_studio.py`
  satırına canvas paragrafı (yeni `strategy_studio/graph.py` dahil) + sondaki
  değişiklik günlüğüne tam kayıt + `summary` tazelendi.
- **köprü** — `strategy_studio/graph.py` docstring'i `Bkz: [[strategy_studio]]
  "Canvas görünümü", [[webapp_module_map]]`; harita da ters yönden karşılık
  veriyor. İki yön de kapalı.
- **kural** — Genel ikinci beyindeki sayfalara (`kisitli_agac_serbest_graf_degil`,
  `nau_studio_canvas_konsept_c_2026_07`) `[[bare-name]]` ile bağ **verilmiyor**:
  bu wiki'nin linter'ı yalnız kendi ağacını görür, bağ `broken_links`'e düşer.
  Köprü yönü main vault → nau_wiki; ters yön düz metin. (Bu turda ilk yazımda
  iki bağ öyle kondu, lint yakaladı, düz metne çevrildi.)
- **boşluk** — `strategy_studio/*.py` modüllerinin haritada kendi satırı yok;
  canvas ayrıntısı route satırının içine yazıldı. Modül-başına satır açmak
  ileride ayrı bir işe değer.
- **düzeltme (denetim turu)** — İlk yazımda "27 yeni test" yazılmıştı; doğrusu
  **39** (545 → 584). Hata aritmetikten değil sıralamadan geldi: ilk tam-süit
  koşusu `test_graph.py` yazıldıktan SONRA yapılmıştı, yani 557'lik ara ölçüm
  zaten 12 yeni testi içeriyordu. `--collect-only` ile beş dosya sayıldı.

## 2026-07-27 (3) — wiki-sync: Strategy Builder enstrüman seçici

Kullanıcı isteği: "backtest edeceğim sembolü seçebilmeliyim, sadece 3 sembol
görünüyor". Kurucunun enstrüman çipleri seçici hâline getirildi (ekle/sil),
sonuç iki sayfaya işlendi.

- **[[strategy_studio]]** — yeni bölüm "Enstrüman çipleri: stub'tan seçiciye".
  Üç tasarım kararı kaydedildi: datalist-vs-select (katalog kapalı liste değil),
  timeframe listesinin `BYBIT_ALL_INTERVALS`'tan türemesi (adaptörün
  `_interval_code`'uyla tek kaynak), toggle ile ✕'in kardeş öğe olması.
- **[[webapp_module_map]]** — değişiklik günlüğüne "Strategy Builder enstrüman
  seçici (2026-07-27)" maddesi.
- **düzeltme (bayat iddia)** — route satırındaki `APIRouter (29 route)` sayısı
  ölçülüp **34** olarak güncellendi. Sayı zaten canvas turunda (3 GET) bayatlamıştı;
  bu tur 2 route daha ekledi. Ders: haritaya yazılan SAYILAR bayatlar — ölçmeden
  taşıma.
- **kapsam notu** — AI cockpit / Lab / Agent Backtest sayfalarındaki sabit
  BTC/ETH/SOL `<select>`'leri bilerek dokunulmadan bırakıldı (kullanıcı yalnız
  kurucu çiplerini istedi). Backtest sekmesi zaten `_bybit_symbols()` ile
  katalogdan besleniyor — üç sayfa bu desene çekilirse ayrı bir tur olur.
- **boşluk (devam)** — `strategy_studio/*.py` modüllerinin haritada hâlâ kendi
  satırı yok; enstrüman mutasyonları da paket satırının içinde kaldı.

Genel ders (ikinci beyne de yazıldı, [[strategy_studio]] §Enstrüman çipleri):
HTMX'te `hx-*` taşıyan bir öğeyi başka bir `hx-*` öğesinin İÇİNE koymak tek
tıkta iki istek attırır — dinleyici elemanın kendisine bağlıdır, event kabarır.


## [2026-07-28] fix | EXTERNAL_CATALOGS varsayılanı düzeltildi — E: → D:\NAU_ev

Kullanıcı sorusu: "indirdiğim NASDAQ hisseleri /data'da neden görünmüyor?"
Kök neden: `data.py` EXTERNAL_CATALOGS varsayılanı `E:\myAI_Projects\NAU_ev\...`
gösteriyordu; masa sürücü taşımış, katalog `D:\NAU_ev\backend\data\catalog`
altında (3.511 seri / 591 enstrüman). Tek yanlış yol, aynı listeden çözünen dört
yüzeyi birden sessizce boşaltıyordu: /data harici paneli, Lab picker'ları
(`list_external_instruments`), backtest yükleri (`_external_bar_dir`),
enstrüman tanımları (`external_instrument_object`).

Varsayılan `D:` yoluna çevrildi; canlı doğrulama: /data → "591 instruments",
`xq=AAPL` → AAPL.NASDAQ (panel alfabetik ilk 50'yi gösterir, gerisi `xq` araması).
§9.4'teki "ortam sorunu, veri yok" hükmü düzeltildi — hüküm verinin diskte
gerçekten olmadığı doğrulanmadan verilmişti. Sayfalar:
nau_guvenlik_dayaniklilik_duzeltmeleri (§9.4 düzeltme paragrafı),
webapp_module_map (data.py satırı). Modül docstring Wiki References değişmedi
(yeni kavram yok). NOT: seçici + taslak-yarışı turlarının wiki senkronu hâlâ
bekliyor (kullanıcı onayı istendi).

## [2026-07-28] feat | ingest_equities.py — NAU_ev flat-file ingest'inin portu

Kullanıcı isteği "NAU_ev'den buraya kopyala": tools/ingest_flatfiles.py +
tools/build_tf_bars.py ikilisi tek CLI'a portlandı. Akış korunur (yıl-parçalı
minute ingest → RTH TF resample → manifest en sonda); üç bilinçli fark
webapp_module_map'in yeni satırında: kendi kök (equity_catalog — NAU_ev'in
veri klasörüne yazılmaz), universe dışlaması yerine diğer-kök guard'ı,
TF aynı koşumda (manifest-tazeliği makinesi gereksizleşti).

data.py: EQUITY_CATALOG_DIR var olduğunda EXTERNAL_CATALOGS'a SONA eklenir —
/data dedup'ı ilk kökü kazandırdığı için NAU_ev'in adjusted sürümü önceliklidir.

Doğrulama: 3 sentetik-arşiv testi (right-label +60e9, premarket RTH dışı,
guard, taze-yeniden-ingest); gerçek smoke AA 2026 → 109 s / 58.988 minute bar,
/data paneli 591→592, load_external_bars 124 günlük bar + UNADJUSTED uyarısı.
Süit 598 geçti (3 yeni); düşen 2 test bilinen süit-altı flaky çifti, izole 6/6.
README'ye kullanım eklendi. ruff temiz.

## [2026-07-28] feat | External enstrümanlar dört yüzeye açıldı — noktalı id ayrımı

Kullanıcı NASDAQ sembollerini uygulamanın listelerinde göremiyordu; dört yüzey
birden bağlandı (ayrıntı strategy_studio.md yeni bölümünde):
1. /backtest ana sayfası: "US Catalog" üçüncü kaynak radio'su + data-grans'a
   göre daralan granularity — /backtest/external_instruments endpoint'i zaten
   vardı, yalnız UI eksikti. E2E: AA.NASDAQ 1-DAY koşusu sonuç paneli üretti.
2. /data external paneli: kullanılan-önce sıralama (EXTERNAL_CACHE_DIR'de
   pandas cache'i olanlar başa, "used" rozeti) — alfabetik ilk 50 (A, AA,
   AAL…) işlevsiz doluyordu.
3. Strategy Builder: picker'da "external" grubu; add_instrument noktalı
   TICKER.VENUE doğrulaması + external TF kümesi (30m/12h reddi); adaptörde
   recipe/loader dalı; runner.build_node_config deploy kapısı (RunnerError).
4. Lab: sembol select'inde "US Catalog" optgroup; is_external dalı yükleme +
   recipe + bars_info (Index konvansiyonu).

data.py: EXTERNAL_GRAN_BY_BYBIT_CODE tek kaynak (studio+lab paylaşır).
Testler: 12 yeni (tests/studio/test_external_instruments.py) — mutasyon
doğrulaması, recipe ayrımı, loader yönlendirmesi (Bybit yoluna sapmadığı da
kanıtlı), deploy reddi. Süit 611 geçti; tek düşen bilinen flaky
(promote_atomicity, izole 5/5). Modül docstring'lerine yeni link gerekmedi
(yeni kavram yok; mevcut Wiki References geçerli).

OPS NOTU: strategy_studio.md frontmatter'ı PowerShell Get/Set-Content ile
güncellenirken mojibake oldu (PS 5.1 BOM'suz UTF-8'i ANSI okur) — git
checkout ile geri alınıp Edit ile yeniden uygulandı. Wiki dosyalarına toplu
metin işlemi PowerShell'le YAPILMAZ; Edit/python kullanılır.
- 2026-08-02 — Brutal test turu: codegate'in **maliyet ekseni** kapatildi (MAX_POW_EXPONENT=64 + MAX_LITERAL_MAGNITUDE=1M literal katlama). e980c71'in `**` kurali tek basina yetmiyordu: `((10**64)**64)**64` icindeki her `**` literal 64 ussu tasir ve gecerdi (4 seviye = 14,5 s olculdu); ayni bosluk list(range(10**9)) sinifini da kapsiyordu. 268/268 disk blogu hala geciyor. Ayrica: OpenRouter model pini artik Claude kredi fallback'ine yenilmiyor (alan-farkindaligi), studio'daki iki 500 -> 422, preview builtins codegate.safe_builtins()'e gecti. Kirilgan test tespiti: test_timeout_collects_done_and_rebuilds_pool yardimci modulu calisma aninda yazip siliyordu -> spawn worker'in dizin-listesi cache'iyle yarisiyordu; kalici tests/_probe_unit.py + havuz isitmasi ile kararlilastirildi (6/6 yesil).
- 2026-08-02 — AUTO ekrani Mission Control kokpitine gecti (design_handoff_auto_mission_control, yon 1c). Yeni sentez sayfasi: auto_mission_control. Kod<->dokuman koprusu iki yonlu tazelendi — webapp_module_map'e web/mission.py satiri + agent_backtest/studio satirlarina Mission Control notu; karsi yonde web/mission.py, web/routes/studio.py ve (ilk kez) web/routes/agent_backtest.py docstring'lerine Wiki References blogu. Sayfanin tasidigi kalici bilgi, kod okunarak bulunamayacak KARARLAR: (a) alti ajan fazi bes hucreye katlanir — "Ranking" kullanici icin ayri bir asama degil; (b) duraklatmada halka bosalir ama iterasyon sayaci kalir; (c) konsol etiket kaliplari _add_step() cagri yerlerinden turetilmis ve EN-OZEL-ONCE siralidir (robustness satiri da "backtest" gecirir); (d) yukseklik olculur (mcFit -> --mc-offset), hardcode edilmez. Iki bug doğrulamayla yakalandi ve sayfaya yazildi: palet .mc uzerindeydi ama slide-over .mc'nin DISINDA render edilir (token miras alinmiyordu -> :root); `started_at or now` falsy tuzagi (unix ts mesru olarak 0.0 olabilir -> is None). Dogrulama iki katman ve bilerek ayri: tests/test_auto_mission.py 32 test (esleme) + scripts/check_auto_cockpit.py (yerlesim; uygulamayi surec icinde ayaga kaldirir, fake_auto_run ile LLM tuketmeden RUNNING enjekte eder, 1440x900 ve tasarimin "en kotu hal" dedigi 924x540). Suit 700 gecti / 1 atlandi. Lint: 0 -> 0 (broken_links/orphans/missing_summary/stubs hepsi sifir; yeni sayfa stub olarak acilip ayni turda dolduruldu).

OPS NOTU: bu turda calisma agacinda baska ajanlarin degisiklikleri vardi (agent.py, codegate.py, tests/*) — commit'ler yalnizca kendi dosyalarimi sahneleyerek atildi, `git add -A` kullanilmadi.

- 2026-08-02 — Iki ozellik + koprunun iki yonu. (1) MODEL GORUNURLUGU: OpenRouter listesi artik openrouter.ai/api/v1/models'ten canli geliyor (341 metin modeli); cache iki yonlu (basari 1 sa, BASARISIZLIK 60 sn — yoksa kapali uc nokta her render'a zaman asimi ekler), cekim duserse eski statik ucluye duser ASLA uydurma id'ye, NAUTILUS_OPENROUTER_MODELS aga hic cikmaz. Yeni agent.model_label()/model_id(): bos secim ("" = uygulama varsayilani) GERCEK modele cozulur, or: pini "OR · <id>", kredi fallback'i devredeyse o yazilir — cozum SUNUCUDA, cunku cozulmus deger secimden sapabilir. Rozet uc yuzeyde; AUTO ust barinda cakismada CANLI OLAN KAZANIR (picker'i istemci yansitir, kokpit fragment'i OOB ile uzerine biner). Teshis notu: sorun kodda degildi — uvicorn OPENROUTER_API_KEY tanimlanmadan once baslamisti; teshisi veren adim testler degil canli sayfayi curl'lemek oldu. Yeni sayfa: model_secici_ve_gorunurluk. (2) TEAR SHEET OVERLAY: her backtest satiri salt-okunur performans sayfasi aciyor (GET /tearsheet + fragments/tearsheet.html + base.html overlay). Belirleyici bulgu: backtest_log kayitlari zaten tam metrics + iki equity egrisi tasiyor (509 realized / 5051 MTM) -> YENIDEN KOSU YOK; bu, spec'i sandbox'ta bilerek replay eden /reports/detail'den ayri bir uc nokta olmasinin tek sebebi. Dort depo tek render modelinde birlesir (log&ts, session&run_id&i, strategy&run_id, suggestion&id); semalar esit olmadigi icin kural "birlesim al, eksigi gizle" — kaydedilmemis metrik tile olarak BASILMAZ, cizilemeyen bolumun sebebi notes'a yazilir. Kimlik anahtarlari: log_backtest() artik yazdigi ts'i DONDURUR (cagiranlar saklayinca link birebir); manuel kosuda log yazimi snapshot'tan ONE alindi (yoksa kalici gorunum linksiz kalirdi); session iterasyonu ev_i ile SIRALAMADAN ONCE damgalanir (sablon skora gore siraliyor) — bu sayede eski oturum dosyalari da baglanabildi. Id izolasyonu: fragment'in her DOM id'si tsh- onekli ve tutamaklar window.__tsh*; overlay canli sonuc ekraninin USTUNDE aciliyor, id paylasmak alttaki grafigi yok ederdi (testle kilitli). Yeni sayfa: tear_sheet_overlay. KOPRU: webapp_module_map'e iki yeni satir (web/tearsheet.py, web/routes/tearsheet.py) + sekiz mevcut satira (agent.py, state.py, web/mission.py, web/shared.py, web/routes/{studio,backtest,sessions}.py, strategy_studio/) degisiklik notu; karsi yonde web/tearsheet.py, web/routes/tearsheet.py, web/mission.py, web/routes/studio.py ve agent.py docstring'lerindeki Wiki References tazelendi (agent.py ilk kez wiki kapsamina girdi). auto_mission_control'e "Sonraki tur" bolumu. Suit 700 gecti / 1 atlandi; yeni tests/test_tearsheet.py 16 test. Lint: 0 -> 0.

OPS NOTU: bu turda calisma agacinda BASKA bir degisiklik seti daha vardi — AUTO kokpitinin TR->EN cevirisi (web/mission.py etiketleri + tests/test_auto_mission.py + scripts/check_auto_cockpit.py). HEAD kendi icinde tutarli (TR kod + TR test); ceviri commit'lenmemis. mission.py bu ceviriyi VE bu turun degisikliklerini ayni anda tasidigi icin dosya bazinda ayrilamiyor — commit kapsami kullaniciya soruldu.

- 2026-08-02 (2) — OpenRouter picker VARSAYILAN OLARAK ucretsizlerle sinirlandi (337 -> 17). Filtre FIYAT alanindan kuruldu, id deseninden degil: pricing.prompt ve pricing.completion ikisi de 0 ise ucretsiz. ":free" son eki tam degil — openrouter/free (Free Models Router) o eki tasimaz ama ucretsizdir. Uc karar: (a) ucretsizlik bayragi CACHE'E girer (katalog artik (id, ad, free)), filtre render'da uygulanir — NAUTILUS_OPENROUTER_FREE_ONLY cevrildiginde yeni ag turu gerekmez; (b) bilinmeyen/ayristirilamayan fiyat PARALI sayilir — supheden liste kaybeder, fatura degil; (c) free-only modda YEDEK de ucretsiz olmali, cunku eski statik uclu (deepseek/deepseek-chat, gemini-2.5-flash, gpt-4o-mini) tamamen paraliydi — ayri _DEFAULT_OPENROUTER_FREE_MODELS eklendi; aksi halde "ucretsiz" yazan grup sessizce fatura yazardi. NAUTILUS_OPENROUTER_MODELS pini filtreden muaf (acik tercih). Optgroup basligi durumu YAZAR ("… — ucretsiz (17)" / "… — tumu (337)") — sessiz filtre, ayni sayfanin acilisindaki "sessiz gizleme" hatasinin tekrari olurdu. Acik kalan: modalite filtresi text ICEREN cikti kabul ettigi icin google/lyria-3-* (muzik, text+audio) ucretsiz listesinde gorunuyor — 337 icinde gozden kaciyordu, 17 icinde batiyor. Dogrulama: canli /studio render'inda 17 or: secenegi + dogru optgroup basligi; tests/test_agent_run_form.py'ye iki test (free-only filtresi + _is_free kenar durumlari + yedegin paraliya dusmedigi). Wiki: model_secici_ve_gorunurluk'e "Ucretsiz filtresi" bolumu.

- 2026-08-02 (3) — Ucretsiz filtresine UCUNCU mod: "ucretsizler + secili paralilar". Yeni anahtar NAUTILUS_OPENROUTER_EXTRA_MODELS (ilk kullanim: moonshotai/kimi-k3, $3/$15 per Mtok, 1M ctx). Mevcut pin (NAUTILUS_OPENROUTER_MODELS) bunu karsilamiyordu: pin listenin YERINE gecer ve aga cikmaz, yani tek id eklemek icin 17 ucretsizi elle yazmak gerekirdi. Iki anahtar bilerek ayri tutuldu — pin'in "aga hic cikmaz" ozelligi testlerin ag bagimsizligini tasiyor, onu toplamali yapmak o garantiyi bozardi. Parali ek IKI yerde isaretlenir cunku iki farkli yuzey var: (a) etikette "· parali" — agent_backtest.html gibi optgroup'suz duz select'lerde tek ayrim isareti budur; (b) ayri optgroup "OpenRouter · elle eklenen — PARALI (N)" — kokpitte; "ucretsiz (17)" baslikli grubun icinde parali satir dursaydi baslik ve sayim yalan olurdu. Katalogda olmayan ek id sessizce dusurulmez, ham id + "parali" ile listelenir (kullanicinin yazdigi id'yi yok saymak, bu sayfanin acilisindaki sessiz-gizleme hatasinin aynisi olurdu). Tests: test_selectable_models_paid_extra_alongside_free — ek listede/istenmeyen parali sizmiyor, etiket isareti, hayalet id, EXTRA'da tekrarlanan ucretsizin cift listelenmemesi, tum-katalog modunda ayri grubun bosalmasi. 46 odakli test gecti; ruff temiz.

- 2026-08-02 (4) — wiki-sync: kod<->dokuman koprusunun KOD YONU tazelendi. webapp_module_map'te iki satir guncellendi: `agent.py` (ucretsiz filtresi — `_is_free` fiyat alanindan okur, `:free` son ekinden DEGIL cunku openrouter/free o eki tasimaz; bayrak cache'e girer, suzme render'da; NAUTILUS_OPENROUTER_FREE_ONLY=0 tum katalogu acar; free-only yedegi AYRI cunku eski statik uclu tamamen paraliydi; NAUTILUS_OPENROUTER_EXTRA_MODELS ucretsizlere EK olarak parali id ekler ve pin'den farki listenin yerine gecmemesi) ve `web/routes/studio.py` (yeni context anahtarlari _llm_or_free_only + _llm_or_paid_extras — sablon grubu "ucretsiz (N)"/"tumu (N)" diye baslikladirsin ve elle eklenen paraliyi ayri optgroup'a koysun diye). Karsi yon zaten yerindeydi: agent.py docstring'ine "ucretsiz filtresi" eklendi, web/routes/studio.py docstring'i model_secici_ve_gorunurluk'e bakiyordu. Lint 0 -> 0 (broken_links/orphans/missing_summary/missing_frontmatter/stale/stubs hepsi sifir).

OPS NOTU: calisma agacinda bu oturumdan ONCE var olan izlenmeyen dosyalar duruyor (_e2e*.py, _fix_i18n*.py, _scan_tr.py, _stoptime.py, ck*.txt, pyout.txt, r.txt, tmp_m.txt, _studio.html, _auto_shot.png, .claude/settings.local.json.yedek-*). Skill'in onerdigi `git add '*.py'` bunlarin bir kismini SUPURURDU — commit yalnizca bu oturumda degistirilen 7 dosya acikca sahnelenerek atildi.

## 2026-08-02 — LLM maliyet denetimi wiki'ye islendi
- ingest: wiki/synthesis/llm_maliyet_kaldiraclari.md (yeni)
- update: wiki/synthesis/webapp_module_map.md — agent.py + token_ledger.py satirlarina maliyet denetimi ozeti
- update: wiki/synthesis/auto_mission_control.md — BUTCE gostergesi -> kaldirac sayfasina bag
- update: agent.py + token_ledger.py docstring Wiki References -> [[llm_maliyet_kaldiraclari]]
- olcum: defter 2026-07-23>08-02 = 3304 cagri / 21.5M token / ~$346; cache yazimi $234 + output $103
- olcum: model probu — sonnet-5 -61% (44 cagrilik gercek kosu dogruladi), opus-5 +22% PAHALI (2.8x uzun yanit)
- tespit: _ClaudeCLIMessages.create max_tokens'i dusuruyor -> 400/900/4000 tavanlari varsayilan yolda olu
- tespit: _purpose'suz 6 cagri noktasi harcamanin %92'si ($316)
- tespit: cache_rd/wr cagri sinifina gore ayrisiyor — custom_block 2.34, composed 0.52, idea 0.45
- acik: defterde 8 sentetik custom_block satiri, kaynagi bulunamadi

- 2026-08-02 (5) — "AUTO'da sectigim LLM'i kullanmiyor" teshis + duzeltme. Kok neden: `openai` paketi kurulu degildi (pyproject'te ZORUNLU bagimlilik olarak yaziliydi ama surecin yorumlayicisinda yoktu), bu yuzden _build_openrouter_client her cagrida RuntimeError atiyordu. Secim yolu bastan sona dogruydu (form -> brief.model -> set_thread_model -> current_model -> _create_message'in "or:" dali); kirilan tek halka istemcinin kurulmasiydi. Teshisi veren sey kod okumak degil, kosu kaydiydi: ~/.cache/nautilus_web_app/agent_sessions/ae9abbe9.jsonl satir 32 sebebi aynen yaziyordu ("OpenRouter backend requires the `openai` package ... falling back to builtin"). Token defteri (token_usage.jsonl) hicbir "or:" modeli KAYDETMEMISTI — 3378 kaydin tamami claude — yani OR yoluna hic girilmemisti; bu da teshisi tek basina daraltiyordu. Asil kusur eksik paket degil, eksikligin gorunusu: her LLM cagrisi dustu, cagiranların graceful fallback'i "Random ... (Claude unavailable) · fallback (RuntimeError)" uretti ve kokpitte faz "✓ Generating strategy" yazdi — kullanici secitigi modele hic ulasilmadan 5 tur rastgele kompozisyon backtest edildi. Uzun otonom kosuyu ayakta tutmak icin tasarlanan degradation, YAPILANDIRMA hatasinda yanlis ilac (kurtarilacak bir kosu yok). Duzeltme: (1) `pip install openai` (2.52.0); (2) agent.model_unavailable_reason(model) — aga cikmadan yalniz yapilandirmayi yoklar, POST /agent/run kosuyu BASLATMADAN 400 + sebep dondurur, kapsam dar: yalniz "or:" (Claude yolu uygulamanin varsayilani); (3) htmx 1.9 varsayilanda 4xx'te swap ETMEZ — rotanin ZATEN VAR OLAN hata govdeleri (gecersiz tarih, eksik enstruman, 50-kosu limiti 429) AUTO ekranina hic ulasmiyordu, START'a basmak hicbir sey yapmamis gibi gorunuyordu; studio.html'de htmx:beforeSwap 4xx -> shouldSwap=true, YALNIZ /agent/run icin (kendi kendini sonlandiran /agent/progress yoklamasinin 4xx govdesi kokpitin yerine gecmemeli). (3) olmadan (2) sessiz bozulmayi sessiz redde cevirirdi — kullanici acisindan fark yok. Dogrulama: ucretsiz uc (openrouter/free) ile uctan uca gercek cagri, agent._create_message uzerinden, yanit "OK". Tests: test_unavailable_model_refuses_the_run_instead_of_degrading (400 + sebep govdede + worker HIC calismadi; istemci kurulabilince ayni secim normal akar), test_model_unavailable_reason_only_gates_openrouter. Acik kalan: kapida ret yalniz BASLANGIC yapilandirmasini kapatir; kosu ortasindaki OpenRouter hatasi (ucretsiz uclarin oran siniri buna cok musait) hala ayni rastgele-kompozisyon yoluna dusuyor ve faz "✓" ile kapaniyor — fallback sayisini kosu durumunda tutup fazi "degraded" isaretlemek gerekir.

- 2026-08-02 (6) — 429 geri cekilmesi + DeepSeek V4 Flash + restart. Paket duzeltmesinden SONRA cagrilar saglayiciya gercekten ulasti ve bu kez 429 "Rate limit exceeded: free-models-per-min" dondu — 4 dakikada 11 fallback, yine rastgele kompozisyon, yine "✓" fazlar: ilk ariza kapandi, ayni SESSIZ BOZULMA YOLU ikinci bir sebeple acik kaldi. Olcum "kredi yukle" refleksini curuttu: hesapta $10 kredi, harcama $0, is_free_tier=false — gunluk kota (1000/gun) zaten yuksekti, takilan o degildi; ucretsiz uclarin DAKIKA basina siniri krediden bagimsiz. (1) deepseek/deepseek-v4-flash-0731 ($0.09/$0.18 per Mtok, 1M ctx) NAUTILUS_OPENROUTER_EXTRA_MODELS'e eklendi — Kimi K3'ten giriste 33x, cikista 83x ucuz; odenen sey token degil ORAN SINIRI. (2) agent._or_create_with_backoff: plan (5,15,45) sn = 65 sn toplam, cunku sinir DAKIKA basina — bir dakikalik pencereyi kapsamayan plan ise yaramaz (openai SDK'sinin varsayilan 2 denemesi ~0.5/1 sn ile geri cekildigi icin tam bu yuzden yetersizdi); Retry-After varsa tahmini ezer; toplam bekleme NAUTILUS_OPENROUTER_429_MAX_WAIT (varsayilan 75 sn) ile sinirli — bir dakikayi kapatacak kadar uzun, STOP yanitini kilitlemeyecek kadar kisa; YALNIZ 429 yeniden denenir. Tests: test_openrouter_429_backs_off_and_succeeds (plan toplaminin >=60 oldugunu da pinler), _honours_retry_after_and_gives_up_within_budget, _non_429_is_not_retried. Ops: kullanici izniyle kosan 1f8775c7 POST /agent/stop ile durduruldu (oldurmek degil, uygulamanin kendi mekanizmasi), sonra pm2 restart nautilus --update-env. Canli dogrulama: 3 optgroup, 19 or: satiri (17 ucretsiz + 2 parali), "OR · DeepSeek: DeepSeek V4 Flash 0731 · parali" gorunuyor, 4xx gorunurluk yamasi sayfada, POST /agent/run gecersiz tarihle 400 + govde donuyor. Acik: geri cekilme 429'u kapatir ama sessiz bozulmayi kapatmaz — butce tukendiginde ya da 429 disi hatada kosu hala rastgele kompozisyona dusuyor ve faz "✓" ile kapaniyor; kalan is fallback sayaci + "degraded" faz isareti.

  EK (ayni tur) — 429 testleri once `monkeypatch.setattr(agent.time, "sleep", ...)` kullaniyordu; bu PAYLASILAN time modulunu degistirdigi icin surecteki her thread'in uykusunu kaciriyor ve tam suite icinde tests/studio/test_promote_draft_atomicity.py::test_concurrent_draft_write_during_promote_loses_nothing testini uzaktan kiriyordu (izole kosuda geciyordu — klasik "flaky" gorunusu, gercek sebep baska bir testin global yamasi). Care: agent.py'de `_sleep = time.sleep` indirection'i, testler onu yamalar. Kural: bir testin yama yuzeyi, test ettigi modulun sinirini asmamali.

- 2026-08-03 (1) — AUTO brief'i artik ACIK ve kullanicinin fiili ayarlariyla acilyor. Istek ekran goruntusuyle geldi: QQQ.NASDAQ · spot · 1H · Strict · 4 iterasyon · "restart after a winner" acik · guidance "adx ve atr ile maksimum kar ve minimum dd" · DATE RANGE 2003-09-10 -> 2026-07-01. Alan varsayilanlari studio.html'e (form `selected`/`value`), MODEL varsayilani web/routes/studio.py::AUTO_DEFAULT_MODEL = "or:moonshotai/kimi-k3" olarak yazildi. MODEL'i sablonda sabitlemek YANLIS olurdu: picker'in icerigi calisma aninda uretiliyor (OPENROUTER_API_KEY, ucretsiz filtresi, NAUTILUS_OPENROUTER_EXTRA_MODELS) — _mc_default_model() secili degerin listede OLDUGUNU dogrular, yoksa "" (Claude) fallback; aksi halde hicbir secenegi isaretlenmemis bir kutu kalir ve START kullanicinin gormedigi modelle kosardi. DATE RANGE de sabit yazilmadi: acilista MAX dugmesine basiliyor (#mc-max -> /data/range), cunku varsayilan "secili sembol+TF'nin tam kapsami"dir ve sabit tarih cifti katalog buyudukce yalan soylerdi — dogrulandi, /data/range external QQQ.NASDAQ 1-HOUR icin tam ekran goruntusundeki cifti donuyor. Cekmece canli kosuda KAPALI kalir (mcInitBrief, active_run_id): o an gorulmesi gereken sey kokpit. POST /agent/run'in kendi Form varsayilanlari degistirilmedi (BTCUSDT/linear/5/continuous kapali) — onlar form disi cagrilarin sozlesmesi. Dogrulama: /studio render'i alan alan sorgulandi (rota testinde degil, gercek HTML'de), 731 passed / 1 skipped, ruff temiz.

- 2026-08-03 (2) — download_massive.py: US-equity verisi artik yerel arsiv olmadan REST'ten inebiliyor. Kullanici bir Massive (massive.com) API anahtari verdi; olculen sey once anahtarin NE oldugu oldu: api.massive.com ve api.polygon.io ayni yaniti donduruyor (Massive = Polygon'un yeni markasi), yani mevcut ingest_equities.py'nin bekledigi flat-file semasiyla ayni saglayici. Ama flat-file kokU (E:\MarketData\massive-flatfiles) bu makinede YOK ve flat-file erisimi ayri S3 kimlik bilgisi ister — elde yalnizca REST anahtari var. Bu yuzden yeni modul ingest_equities'in yerine gecmiyor, KARDESI oluyor: Faz B (RTH TF resample) ve Faz C (manifest en sonda) ayni fonksiyonlardan cagriliyor, yalniz Faz A REST'e bakiyor. Anahtarin plan penceresi olculdu (varsayim degil, probe): dakika-bar 2024-08-05 OK / 2023-08-01 403 NOT_AUTHORIZED -> ~2 yil gecmis; art arda cagrida 3. istek 429 -> 5 istek/dk. Bu iki sinir tasarimi belirledi: _Pacer (60/rpm bekleme + Retry-After/ustel geri cekilme) ve "plan disi yil koşumu dusurmez" kurali — 403 alan yil loglanip atlanir, manifest yalniz gercekten inen araligi yazar (yarim veri tam gorunmesin; ayni oturumdaki degrade-gorunurlugu dersinin veri tarafi). REST yolunun kazanci: veri ADJUSTED, yani flat-file yolunun bilinen split-sicramasi yok; write_manifest bu yuzden adjusted/source/note parametreleri aldi ve iki kaynak tek manifest yazicisini paylasiyor. Anahtar repoya yazilmadi: yalniz MASSIVE_API_KEY/POLYGON_API_KEY ortam degiskeni + Authorization: Bearer basligi (URL'de gitmez, logda gorunmez). Dogrulama olcumle: HOOD 2026-07 gercek indirme -> 16.337 dakika bar / 22 gunluk bar; turetilen 1-DAY etiketleri flat-file'dan gelen AA ile AYNI konvansiyonda (gun bar'i ertesi gece yarisina etiketlenir — build_tf_bars right-label'inin bilinen davranisi, yeni kod bunu degistirmiyor); gunluk kapanislar Massive'in kendi gunluk bar'lariyla bir gun kaydirmali olarak ~1 kurus icinde ortusuyor. 4 yeni test (sahte _get ile: right-label+RTH+adjusted manifest, next_url sayfalama, plan-disi yil atlama, anahtarsiz calistirma) — 7 passed. Sayfalar: webapp_module_map (download_massive.py satiri), README veri kaynaklari. Acik: flat-file (S3) yolu bu anahtarla acilmiyor; 2 yildan eski gecmis ve ~12.400 ticker'lik toplu ingest icin ya plan yukseltmesi ya da flat-file abonelik kimlik bilgisi gerekiyor.

- 2026-08-03 (2) — wiki-sync: AUTO brief varsayilanlari (studio.html form + web/routes/studio.py::AUTO_DEFAULT_MODEL) webapp_module_map studio.py satirina ve auto_mission_control'e islendi. Ayrica llm_maliyet_kaldiraclari'na iki ekleme: (a) UCUNCU OLCUM TUZAGI — kokpitin maliyet gostergesi OpenRouter kosularinda 3,33x sisik; _llm_cost_usd fiyati current_model() ile cozuyor, o thread-local ve HTTP yoklama thread'inde kosunun pin'i gorunmedigi icin uygulama varsayilanina (fable-5 $10/$50) dusuyor. Kosu 51a9f3ba, 32 cagri/189.832 token: ekranda $5,09, gercek $1,53. Care: modeli kosu durumuna yazip okuyucularin oradan almasi. (b) Kimi K3 <-> Sonnet 5 olcumu: OpenRouter kimi $3/$15 = Sonnet 5 listesiyle BIREBIR ayni (sonnet ayrica 31.08'e kadar $2/$10 tanitim); olculen cagri-basi maliyet kimi $0,0397 (54 cagri) vs sonnet $0,0241 (29 cagri). Farki iki kalem suruyor: cikti uzunlugu (1.796 vs 1.237 tok/cagri) ve onbellek (kimi'nin 54 cagrisinda cache_read=cache_write=0; claude yolunda 29 cagrinin toplam ham girdisi 52 token). OpenRouter kimi icin cache fiyati yayinliyor, entegrasyon kullanmiyor. Kalite tarafinda kimi'yi tercih ettirecek olculmus gerekce yok; cikis-blogu ad-kod uyusmazligi Sonnet 5 oturumlarinda da var (4/4, 2/4, 2/4) -> uretici degil ISTEK kusuru.

- 2026-08-03 (3) — download_flatfiles.py: S3 flat-file aynasi + olculen kapsam ayrimi. Kullanici Flat Files kimlik bilgilerini verdi ("kalanlari buradan indir" — 9 sembolun 2024 oncesi gecmisi ve kalan ~12.400 ticker). Sonuc kullanicinin bekledigi gibi CIKMADI ve bunu ancak olcum gosterdi: kimlik bilgileri GECERLI (LIST 200, imza kabul), ama GET her us_stocks_sip datasetinde (day/minute/trades/quotes, 2003'ten 2026'ya denenen her tarihte) 403 NOT_AUTHORIZED donuyor; crypto/forex/futures/options da 403; yalniz us_indices/ 200 veriyor. Iki bagimsiz istemciyle dogrulandi (boto3 + curl --aws-sigv4), gövde {"status":"NOT_AUTHORIZED","message":"forbidden"} — yani REST tarafindaki plan hatasiyla ayni sinif. Ders: LISTELEME ile INDIRME ayri yetkiler; "arsivi goruyorum" erisimi kanitlamiyor (5.759 dosya / 84,9 GB us_stocks_sip listelenebiliyor ama tek bayti inmiyor). Kullaniciya secenek sunuldu, us_indices/minute_aggs_v1 (904 dosya / 119,4 GB, 2023-02-14 -> 2026-07-31) secildi. Modul: yerel yol = S3 key yolu; .part + os.replace ile yarim dosya asil adiyla kalmaz; yeniden kosumda boyutu tutan atlanir, tutmayan yeniden iner (119 GB'in kesintiden sonra bastan inmemesi buna bagli); 403 yeniden DENENMEZ (FlatFileError) cunku ag hatasi degil kapsam. Hiz olcumunun kendi dersi: tek dosya 0,9 MB/s, 12 paralel ranged GET 12,4 MB/s olculdu ve plan buna gore yapildi — gercek ayna 48,5 MB/s kostu, cunku boto3 download_file dosya BASINA multipart esszamanlilik kuruyor; tek-akis probu toplam kapasiteyi olcmuyor, tahmini 2,7 saatten 41 dakikaya dustu. 6 test (sahte S3: yol aynasi, tam dosya atlanir, yarim dosya yeniden iner, 403 yapilandirma hatasi + artik birakmaz, yil suzgeci, kimlik bilgisi yoklugu). boto3 yeni bagimlilik. Sayfalar: webapp_module_map (yeni satir), README (veri kaynaklari). Not: us_indices verisi INDEX_ROOT'un bekledigi values_v1 semasiyla ayni (ticker,value,timestamp) — minute_aggs ise ticker,open,close,high,low,window_start (hacim YOK, 13.326 index ticker'i); ingest tarafi henuz yazilmadi.

## 2026-08-04 — Bes eksenli review + uc duzeltme

Mimari/kod/performans/algoritma/E2E taramasi yapildi; bulgular olculdu ve ucu duzeltildi.

YENI SAYFA [[auto_arama_ekonomisi]]. AUTO'nun "hicbir aday gecemiyor" sonucu stratejilerden
degil BOYUTLANDIRMADAN geliyordu: LLM kripto aliskanligiyla trade_size=0.01 uretiyor, hisse
senedinde 0 adede yuvarlaniyor, `_clamp_spec_trade_size` 1.0'a sabitliyordu. QQQ ortalama $220
+ IBKR Fixed (max(adet*0.005, 1)) => gidis-donus $2 => islem basina %0,91 vergi. Ustelik
komisyon 200 hisseye kadar SABIT, yani 1 hisse oran olarak mumkun olan en kotu boyut. Olculdu:
ayni sinyal/ayni islemler, yalniz pozisyon carpani -> kaybeden 10 adayin 8'i kara geciyor.
Duzeltme sabit adet DEGIL `percent_equity` (AGENT_EQUITY_PCT, vars. %95); sabit adet QQQ'nun
30x fiyat araliginda (2003 ~$25, bugun ~$725) calismaz. Komisyon orani %0,91 -> ~%0,02.
ACIK UC: pozisyon buyuyunce drawdown da dolar olarak buyur; kazanc net P&L'in ISARET
DEGISTIRMESINDEN geliyor, risk-getiri oraninin iyilesmesinden degil (Calmar ~korunur).

KARAR — `_MIN_TRADES` 20'de BIRAKILDI. `_score` az-islem cezasini zaten surekli bir carpanla
(n/(n+20)) uyguluyor, ustune n<20 -> -inf sert kapisi var ve bu ikinci ceza bilgiyi yok ederek
uyguluyor (bir kosuda net pozitif uc adayin ikisi, 17'ser islem, hic degerlendirilmeden elendi).
20->10 denendi, `test_threshold_is_nau_aligned` kirildi: deger NAU_ev optimizer'inin
JUNK_MIN_TRADES=20 esigiyle BILINCLI parite, yani kusur degil yontem karari. Ayrica gerekce de
curudu — esigi kurcalama sebebi sabit komisyonun dusuk frekansa verdigi yapay avantajdi,
boyutlandirma duzeltilince o baski kalkti. Geri alindi; secenek AGENT_MIN_TRADES=10 ile acik.

PERFORMANS ([[nau_performans_denetimi]] ikinci tur bolumu). (P1) `robustness_result` olay basina
3,5 MB yaziyordu (wfo_windows 2,43 MB + mc.curves_sample 0,44 + 8.605 noktalik ham OOS egrisi);
76 oturum = 11,8 GB, en buyugu 4,7 GB. Kok neden YUZEY BASINA uygulanmis duzeltme: equity
indirgeme `backtest_result` yolunda vardi, `robustness_result` yolunda yoktu. `_thin_curves`
eklendi — yalniz tamami sayi olan ve 40'tan uzun dizileri indirger, dict listeleri ve metrikler
korunur, girdi degismez. (P2) `_session_summary` her satiri json.loads ediyordu (3,5 MB'lik
satirlari yalniz ADINI saymak icin); artik olay adi + ts satirin ilk 400 baytindan regex ile
okunuyor, tam parse yalniz dort olay icin. /sessions soguk 114 s -> 19 s; fonksiyon A/B ile
(regex hic eslesmeyecek hale getirilip eski yol zorlanarak) BIREBIR esdeger dogrulandi.
KALAN BORC: 19 s'nin tamami I/O — P1 yeni buyumeyi durdurur, mevcut 11,8 GB'i kucultmez; eski
loglarin arsivlenmesi ~1 sn'ye indirir. `_SUMMARY_CACHE` surec ici oldugu icin her restart
soguk maliyeti geri getiriyor.

ACIK, DUZELTILMEMIS: (a) `calmar = pnl_pct / max(|max_dd|, 0.01)` tabani dusuk riskli
stratejiyi cezalandiriyor (DD %0,3 olan aday Calmar 1,4 yerine 0,42 aliyor). (b) /studio yaniti
1,73 MB. (c) agent_backtest.py 3.279 satir — route+worker+skorlama+robustness+SQLite tek modulde.
(d) Escalisan AUTO kosusu sinirlamasi yok (ProgressStore(50) bellegi korur, CPU/token'i degil).
(e) Fix 1'in gercek etkisi OLCULMEDI — yeni bir AUTO kosusu gerekiyor.

E2E: pozitif 15/16, negatif 16/17 (iki "fail" de test beklentisi hatasiydi). Path traversal,
XSS, devasa/negatif girdi savunulmus; n_iterations max(2, min(15, n)) ile kelepceleniyor.
Suit 745 passed / 1 skipped.

## [2026-08-04] ingest | AUTO denetimi — kapı ve geri bildirim düzeltmeleri
- wiki/synthesis/auto_kapi_ve_geri_bildirim.md (yeni): WFO kapısı naive seriye
  çevrildi (_wfo_test), modele giden geçmişe TF/max_dd/commission eklendi,
  timeframe= üretim anında geçiyor, profit_factor işlem bazlı, oturum logu %95 küçüldü.
- wiki/synthesis/auto_arama_ekonomisi.md: 1376c812 ölçümü (komisyon brütün %16.500'ü).

## [2026-08-04] ingest | Doğrulama koşusu 3cad3325 — dört kusur daha
- Blok adına tur eklendi (sertifikalanan strateji üzerine yazılıyordu),
  IS/OOS etiketi üçe ayrıldı, IS_SHARPE_MIN tabanı, HOLDOUT_MIN_TRADES,
  backtest_result equity çiftinin hizalı seyreltilmesi (_thin_pair).
- wiki/synthesis/auto_kapi_ve_geri_bildirim.md §4 eklendi.

## [2026-08-05] ingest | AUTO 360° canlı inceleme — başlangıç kaydı
- Yeni sayfa: [[auto_360_canli_review_iyilestirmeleri]]. `6432552f` ve `bf598e4f`
  koşularının rol sözleşmesi, WFO kapısı, multi-symbol fail-fast, sealed holdout,
  HTTP 402 devre kesici, token/maliyet ve JSONL büyümesi kanıtları kaydedildi.
- Durum: düzeltme öncesi taban. Uygulanan kod ve test sonuçları aynı sayfaya ikinci
  güncellemede eklenecek.

## [2026-08-05] ingest | AUTO 360° iyileştirmeleri — uygulandı ve doğrulandı
- Rol kontratı fail-closed; degraded finalist winner olamaz; terminal 401/402/403
  devreyi açar; WFO `%50 + penalized Sharpe`; multi-symbol kesin ret fail-fast.
- Sealed holdout katalogdan önce: `n>=20`, pozitif PnL/Sharpe/excess ve timeframe
  başına tek kullanım. Buy-and-hold benchmark ve AUTO'ya deterministik 1-tick
  slippage eklendi; bilinen unadjusted hisse verisi reddediliyor.
- Sürekli mod varsayılanı 4 saat/250k token; model-kimliği maliyet tutarlılığı,
  run-id RNG seed'i, nested JSONL curve thinning ve tur sayacı düzeltildi.
- Kusurlu iki koşunun 14/14 winner spec'i tam yedekle karantinaya taşındı
  (aktif katalog 850 → 836).
- Doğrulama: 763 passed, 1 skipped; PM2 restart, `/studio` HTTP 200.
- Ayrıntı: [[auto_360_canli_review_iyilestirmeleri]].

## [2026-08-05] maintain | AUTO 360° Wiki son senkronizasyonu
- Aktif katalog yeniden doğrulandı: 836 kayıt; karantinadaki 14 kayıt aktif
  kataloğa geri dönmemiştir.
- PM2 `nautilus` online, unstable restart 0; `/studio` HTTP 200.
- Canlı izleme otomasyonunun artık mevcut olmadığı doğrulandı. Sonraki inceleme
  adjusted veriyle yapılacak yeni AUTO koşusuna bağlandı.

## [2026-08-07] ingest | Test araçları kararı — Semgrep (harici, built-in değil)
- Claude Code built-in skill'leri (code-review/security-review) yerine bağımsız
  CLI tercih edildi; birincil: Semgrep. Tamamlayıcı önerileri: SonarQube,
  Locust, py-spy/scalene.
- Ayrıntı: [[test_araclari_karari]].

## [2026-08-07] ingest | Semgrep ilk tarama tamamlandı
- 456 kural / 122 dosya (studio_app, strategy_studio, web, scripts, tests);
  10 bulgu, triyaj sonrası 1 gerçek (base.html CDN script'lerinde SRI eksik),
  9'u yanlış pozitif/bilinçli tasarım.
- Windows'ta semgrep JSON çıktı cp1254 codec hatası veriyor,
  PYTHONUTF8=1 ile çözülüyor.
- Ayrıntı: [[test_araclari_karari]].

## [2026-08-08] maintain | DeepR toplu sertleştirme — 35/35 görev tamamlandı
- Kişisel `mDeep`/DeepR skill'i (Workflow yerine `Agent` tool ile — Workflow'un
  additional-working-directory kapsam sızıntısı bu koşuda da doğrulandı) 13
  boyutta NAU_v18Jul'u taradı; rapor `deepr_report_2026-08-08_0038.md`.
- ~80 ham bulgu 35 konsolide göreve indirgendi; her biri koddan doğrulanıp
  sonra düzeltildi. En kritik 3: auth yoktu + internete açıktı + chat XSS,
  iki backtest motoru arası ters-işaretli max-drawdown sözleşmesi, `calc_rsi`
  negatif `period`'da sessizce yanlış değer.
- Test tabanı 849 → 931 geçen (0 regresyon, 2 önceden var olan ilgisiz hata
  değişmedi); yeni `pytest.mark.e2e` gerçek Bybit ağ çağrısı yapan testleri
  varsayılan koşumdan ayırıyor.
- Ayrıntı: [[nau_deepr_toplu_sertlestirme_2026_08]], kod↔wiki köprüsü
  `server.py` satırı [[webapp_module_map]]'te güncellendi.

## [2026-08-09] maintain | agent.py + composer.py kademeli çıkarım — Faz 1 tamam, Faz 2 devam ediyor
- Faz 1 ("safe-first slice", 10 commit): `agent.py` Adım 0-4 → `web_research.py`
  + `llm_client.py` (model/effort/OpenRouter-katalog seçimi, LLM kontrol
  düzlemi, Claude Code CLI backend); `composer.py` Adım 0a-3 → iki kalıcı
  güvenlik-ağı testi (gerçek deploy-path çözümlemesi + genel-yüzey golden-set)
  + `block_meta.py`/`block_library_classic.py`/`block_library_nau.py`.
- Faz 2 (riskli katman, devam ediyor): Adım 5 `_run_openrouter_killable`'ın
  GERÇEK multiprocessing spawn/pipe/timeout/kill mekaniğini süren 3 test
  (öncesi: tek test bunu sahteyle değiştiriyordu, gerçek mekanik SIFIR
  kapsamlıydı) — mutasyon-doğrulandı (`_stop_provider_process` bozulunca
  sentinel dosya yazılıyor, test yakalıyor). Adım 6: OpenRouter backend'in
  tamamı → `openrouter_backend.py`. Bu adımda iki gerçek ama bu taşımadan
  kaynaklanmayan hata bulundu/düzeltildi: (1) `test_llm_client_control.py`
  thread-local `_LLM_CONTROL`'ü teardown'da sıfırlamıyordu — dosya tek başına
  ve alfabetik tam suite'te sorunsuzdu, ama farklı sırada birlikte
  koşturulunca `test_agent_run_form.py`'nin bir 429-backoff testi milyonlarca
  döngüyle pratikte asıldı (bkz. ikinci-beyin
  [[thread_local_testte_sonraki_testi_kirletir]]); (2) `_retry_after_seconds`
  re-export'u, dış tüketiciyi arayan grep deseni yalnız `agent.X` erişimini
  yakalayıp `from agent import (X, ...)` biçimini kaçırdığı için ilk seferde
  atlandı — tam suite collection hatasıyla yakalandı.
- `_run_openrouter_killable`'ın Windows konsol-donması korumasının
  `sandbox.py`'nin `pythonw.exe`/`set_executable()`'ına KAZA eseri bağımlı
  olduğu bulundu (`multiprocessing.Process()`'in `creationflags` kolu yok);
  kaynak-seviyesi AST tripwire testi eklendi
  (`TestNoConsoleWindow::test_loop_runner_still_imports_sandbox_at_module_level`),
  bağımlılığı düzeltmek (sandbox.py'den bağımsız, açıkça çağrılan hale
  getirmek) davranış değiştiren ayrı bir iş olarak ertelendi.
- Kalan: composer.py Adım 4-5 (spec modeli çıkarımı + katalog I/O'nun 4
  spesifik boşluğu için karakterizasyon testleri), agent.py Adım 7
  (opsiyonel `_build_client`/`_get_client` testleri). Registry çekirdeği +
  custom-block entegrasyonu (composer.py) ve genel dispatch çekirdeği +
  Domain C (~2200 satır, agent.py) kasıtlı olarak ayrı bir oturuma
  bırakıldı. `ComposedStrategy`/`ComposedStrategyConfig` kalıcı olarak
  taşınmıyor.
- Ayrıntı: kod↔wiki köprüsü [[webapp_module_map]]'te güncellendi (agent.py/
  composer.py satırlarına kademeli-çıkarım önsözü + 6 yeni modül satırı).

## [2026-08-09] maintain | Faz 2 bu oturum için kapandı: composer_spec.py + katalog testleri + agent.py Adım 7

Önceki girdinin "Kalan" listesi tamamlandı, 3 commit daha (`764bd9b`,
`3792f75`, `6ab1702`): composer.py'nin spec modeli + spec-upsert servisi
(`SignalBlock`/`ComposedStrategySpec`/`build_spec`/`new_spec_id`) →
`composer_spec.py` (composer.py'nin kendi decomposition'ında ilk kez
in-file tüketicisi olmayan bir isim re-export edildi, ilk `__all__`
listesi bunun için eklendi); katalog I/O'nun 4 boşluğu için 9 yeni test
(kod taşınmadı); agent.py'nin `_build_client`/`_get_client` backend-seçim
dallanması için 11 yeni test. Yol boyunca ikinci gerçek hata bulunup
düzeltildi: iki test `BLOCK_REGISTRY`'yi monkeypatch'leyip `BLOCK_CATALOG`'u
unutuyordu — `_rebuild_catalog()`'un kimlik-koruması yüzünden GERÇEK,
süreç-geneli `BLOCK_CATALOG` 370→1'e kalıcı bozuluyordu (yeni bir katalog
test dosyası tek başına 9/9 geçerken bu dosyayla birlikte 5/9 düşerek
ortaya çıktı).

Adım 6 (katalog I/O'nun `strategy_catalog.py`'ye taşınması) planın kendi
tasarımı gereği taahhüt edilmemiş bir karar noktasıydı — kullanıcıya
soruldu, **"burada dur" seçildi**. composer.py'nin registry çekirdeği +
custom-block entegrasyonuyla (ComposedStrategy örnek-durumuna çalışma-anında
erişen özel risk sınıfı) birlikte, tamamen ayrı bir oturuma bırakıldı.
Faz 2'nin planı (`~/.claude/plans/seninle-deepr-skillini-g-zden-abundant-hellman.md`)
bu kararla güncellendi.

## 2026-08-11 — Onarım yolunda UTC/ET gün sınırı kayması [KRİTİK]

DeepR bulgusu: `repair_massive_intraday._replace_day` silinecek günü naif
`[D 00:00 UTC, D+1 00:00 UTC)` penceresiyle seçiyordu. Right-label sözleşmesi
yüzünden bir seansın 1-DAY barı `D+1 00:00 New York` (DST'ye göre 04:00/05:00
UTC) ile damgalanır, yani D'nin UTC gününün DIŞINDADIR: pencere D'nin barını
ıskalayıp bir ÖNCEKİ seansınkini siliyor, üstüne yenisini ekleyerek onarılan
günü İKİZLİYORDU. Gün içi TF'ler tesadüfen doğruydu (09:31–16:00 NY her iki
DST rejiminde de aynı UTC gününe düşer), bu yüzden kusur yalnız 1-DAY'de
görünüyordu.

Düzeltme `ingest_equities.session_label_bounds_ns` olarak eklendi (pencere =
kovanın kendisi: solda açık `D 00:00 NY`, sağda kapalı `D+1 00:00 NY`) ve
onarım aracı onu import ediyor — damgalama sözleşmesinin ikinci bir kopyası
tutulmadı; bugün düzeltilen 4-HOUR/`resample_tf` hatası da tam bu sınıftandı.

`us_equity_katalog_veri_butunlugu` sayfasına 6. kusur olarak işlendi. Gerçek
katalog taraması (16 ticker, salt-okunur): ikizlenmiş seans yok, tek günlük
delik yok, `.bak` yok — hata sahada hiç tetiklenmemiş.


## 2026-08-11 — DeepR dördüncü tur: üç test boşluğu + tekrar-üretilebilirlik

Dört YÜKSEK bulgu kapatıldı; üçü "kod doğru ama hiçbir test onu tutmuyor",
biri "ortam hiçbir yerde sabitlenmemiş".

**1. Klasik blok evaluator'ları (`block_library_classic.py`).** Sekiz
evaluator tüm test ağacında yalnız golden-name frozenset'inde geçiyordu:
davranışları değil, erişilebilir OLMALARI test ediliyordu. 74 test eklendi
(`tests/test_block_library_classic_evaluators.py`). Üç sözleşme pinlendi:
kenar tetikleme (`prev = _prev_state.get(idx, diff)` → ilk barda asla
ateşlenmez), warmup (`None`, durum bile yazılmaz) ve stateful `_eval_atr_stop`
— trailing tepe/dip taşınması, flat olunca SIFIRLANMA (yeni pozisyon eski
tepeyi miras almaz), yön (`hi - atr*mult`; yükselen fiyat uzun pozisyonu
stop'lamaz) ve blok-indeksi başına ayrı anahtar. Ek olarak
`BLOCK_REGISTRY[...]["eval"] is <fonksiyon>` assert'i, testlerin ölü bir
kopyayı değil üretimde koşan kodu koruduğunu garantiliyor.

**2. `composer_spec.py` boyutlandırma dalları + `build_spec` clamp'leri.**
Gerçek para büyüklüğünü belirleyen kod; test edilen tek kısım isim/entry-blok/
atr_period idi. 95 test eklendi (`tests/test_composer_spec_sizing_and_clamps.py`):
`percent_equity` (sonluluk, alt sınır, %100 dahil üst sınır, castlanamayan
girdi), `atr_target`, `fixed_usdt`, `vol_target`'ın üç kapısı, bracket SL/TP
dalları — her biri hem RED hem de SINIR DEĞERDE KABUL yönüyle. Ayrıca dalların
MODA BAĞLI olduğu (formda kalmış eski bir yüzde, ilgisiz bir fixed_usdt
stratejisini kaydedilemez yapmamalı) ve `vol_target`'ın trade_size_mode
whitelist'inde KALDIĞI çivilendi — `build_spec`'in varlık sebebi tam olarak
eski strategy.py clamp'inin vol_target'ı sessizce "fixed"e düşürmesiydi.
`_as_bool` (işaretlenmemiş checkbox → `use_bracket=True` riski) da kapsandı.

**3. `wfo_optimizer` GA çekirdeği.** `ga_plan` / `ga_initial_population` /
`_tournament_idx` / `ga_next_population` docstring'leri bir DETERMİNİZM
sözleşmesi yazıyordu ama hiçbir test onu tutmuyordu. 50 test eklendi
(`tests/test_wfo_ga_core.py`): aynı tohum → birebir aynı popülasyon/nesil
(ve FARKLI tohum → farklı sonuç, yoksa "determinizm" sabit çıktıyla da
sağlanır), eşitlikte düşük indeks kuralı (sıralı-paralel parite bunun üstünde
duruyor), `ga_plan`'ın yarım-yukarı yuvarlaması (11→1, 12→2, 20/8→3), dejenere
uzaylar (boş uzay → `(1,1)` ve boş bireyler; `lo == hi`; `pop_size <= 1`
sonsuz döngüye girmez), mutasyon clamp'i ve elit skorun nesiller boyunca
düşmemesi.

**4. Bağımlılık kilidi.** `[build-system]` yoktu (kurulum setuptools'un eski
yoluna düşüyordu), `uv.lock` yoktu ve `nautilus_trader` dışında her sürüm
tabanı serbestti; CI `uv sync` ile her koşumda yeniden çözüyordu — workflow'un
kendi yorumu sorunu zaten itiraf ediyordu. Eklendi: PEP 517 backend
(`setuptools`, `py-modules = []` — depo kütüphane değil uygulama), 98 paketlik
`uv.lock` ve CI'da `uv sync --locked`. Kilit KURULU ortamdan üretildi: ilk
`uv lock` her şeyi en yeni sürüme çözünce (pandas 3.0.5, pyarrow 25.0.1,
starlette 1.6.0 …) geçici bir `constraint-dependencies` iskelesiyle çözüm
çalışan ortama sabitlendi, sonra iskele kaldırıldı ve `uv lock --check` ile
sürümlerin sabit kaldığı doğrulandı — kilitteki 98 paketin tamamı geliştirme
makinesindeki sürümlerle birebir. Böylece CI ile yerel makine aynı
pandas/numpy'ı koşuyor; `regression_baseline.json`'ın dayandığı sayısal
tekrar-üretilebilirlik ilk kez gerçekten sabitlenmiş oldu.
`tests/test_dependency_lock.py` (8 test) kilidin beyan edilen her bağımlılığı
kapsadığını ve CI'nın `--locked` ile kurduğunu bekçiliyor.

Bu turda eklenen: 235 test (74 + 95 + 50 + 8 + 8). Süit tamamen yeşil;
`ruff check .` ve `ruff format --check .` temiz.

## 2026-08-11 — DeepR: iki YÜKSEK performans bulgusu kapatıldı

**1. H4 — NAU-parite blokları bar başına 260-pencereyi sıfırdan hesaplıyordu.**
İki turdur "parite kısıtı yüzünden ertelendi" duruyordu; artımlı state
NAU_WINDOW=260'ı kırar. Bu kez pencere aynen bırakıldı ve aynı iş daha az
yorumlayıcı adımıyla yapıldı: `indicators.calc_adx` tek geçişe indi (dört ara
liste + bar başına 246 tek-kullanımlık sözlük gitti), `calc_stoch_rsi`'nin
14'lük dilim min/max taraması tembel-yeniden-taramaya döndü,
`calc_wave_trend`'in yedi geçişi beşe indi, `_tail3` aynı boyda kopya
üretmiyor. `block_library_nau._nau_cached` bar başına tek hesap garantisi
veriyor (entry+exit aynı bloğu paylaşırsa ikinci tarama yok). Ölçüm:
calc_adx 117→80 µs, calc_stoch_rsi 131→83 µs, calc_wave_trend 70→59 µs;
40.000 barlık uçtan uca adx 4,88→3,39 s, entry+exit ikilisinde 9,68→3,29 s.
Parite `tests/test_indicators_hotpath_parity.py` ile kanıtlandı: dosya
optimizasyon öncesi sürümün birebir kopyasını taşıyor ve 641 kayan pencerenin
her birinde `==` (tolerans YOK) eşitlik arıyor. Python 3.12'nin `sum()`
Neumaier toplaması yüzünden Wilder tohumları elle biriktirilmedi — bu tek
detay parite ile hız arasındaki sınırı çiziyor.

**2. `_validate_external_data` event loop'u kilitliyordu.** `async def run(...)`
gövdesinden doğrudan çağrılıyor, her interval için tam parquet serisini
okuyordu; POST /agent/run boyunca açık her sekmedeki HTMX poll'u donuyordu.
`asyncio.to_thread` ile sarıldı ve tarih daraltması `load_external_bars`'ın
içine itildi (`< X` → `<= X − 1 ns` birebir eşdeğer dönüşümüyle).

Bu turda eklenen: 52 test (34 + 11 + 7). Ayrıntı: [[nau_performans_denetimi]].


## 2026-08-11 — DeepR: dört YÜKSEK "sistem biliyor ama söylemiyor" bulgusu kapatıldı

Ortak desen: bilgi ÜRETİLİYOR ama hiçbir kanala verilmiyor. Dördü de aynı
duruşla kapatıldı — bilgiyi log'a, ekrana ve kalıcı kayda taşı; "0 sonuç" ile
"hata" birbirinden ayrılsın.

1. **Manuel robustness suite'inin `full_error` alanı hiç okunmuyordu**
   (`web/routes/robustness.py`, `sandbox.py`). `_manual_suite_child` bu anahtarı
   sözleşmesinin parçası olarak döndürüyor, projede okuyan tek satır yoktu:
   Monte Carlo için koşulan tam backtest çöktüğünde kullanıcı dolu WFO/IS-OOS
   tabloları görüp koşuyu başarılı sanıyor, hata hem ekrandan hem log'dan
   kayboluyordu. Artık sandbox "0 işlem" (`failed=False`) ile "backtest çöktü"
   (`failed=True`) için AYRI sonuçlar üretiyor; route hatayı loglayıp adım
   listesine ve sonuca taşıyor; `robustness_result.html` kırmızı bir "kısmen
   bozulmuş koşu" bandı + işaretli Monte Carlo sekmesi gösteriyor;
   `web/shared.log_robustness` `full_error`/`monte_carlo.failed` alanlarını
   kalıcı kayda yazıyor (rapor ekranla aynı hikâyeyi anlatsın).

2. **`load_catalog` bozuk kayıtları sessizce düşürüyordu** (`composer.py`).
   `n_broken` sayılıyor ama loglanmıyor, çağırana bildirilmiyor, UI'a
   taşınmıyordu — hemen üstteki custom-block registry hatası düzgün uyarı
   basarken. Artık kayıtların kendisi toplanıyor (indeks, id, ad, gerçek hata),
   `logging.warning` ile basılıyor ve
   `<katalog dizini>/quarantine/strategy_catalog.broken-records.<ts>.json`
   dosyasına HAM JSON'uyla yazılıyor (kayıt silinmez — `append_to_catalog`
   load→append→save olduğu için bir sonraki kaydetme onu diskten düşürebilir;
   karantina geri dönüşü mümkün kılar). Özet `composer.last_catalog_load_issues()`
   ile açılıyor ve `server.py`'deki `catalog_issues` Jinja global'i üzerinden
   `catalog_list.html`'in üç include noktasının hepsinde sarı bir bant oluyor.
   (mtime,size) anahtarı ~18 sıcak çağrı yolunda tekrar-yazmayı engelliyor.

3. **Cent altı semboller 0.00'a yuvarlanıyordu** (`backtest.py` + `sandbox.py` +
   `parallel_exec.py` + `data.py`). Ayrıntı ve genel kural:
   [[index_backtest_via_equity_proxy]] "Kardeş Trap: `price_precision`".

4. **Kritik E2E testi hiçbir yerde koşmuyordu**
   (`tests/test_backtest_run_progress_result_e2e.py`, `.github/workflows/ci.yml`).
   `spec_id` fixture'ı repo DIŞINDAKİ `~/.cache` kataloğuna bağlıydı ve boşsa
   skip ediyordu; temiz bir runner'da test 0 iş yapıp yeşil dönüyor, CI'ın
   3-denemeli retry'ı bunu başarı sayıyordu. Katalog bağımlılığı kaldırıldı
   (fixture kendi `tmp_path` kataloğunu tohumluyor) ve aynı zincirin AĞSIZ
   ikizi eklendi: `TestBacktestChainOffline` gerçek route/worker/sandbox/Nautilus
   motorunu sentetik barlarla sürüyor, varsayılan koşumda ve CI'da çalışıyor
   (~18 s). Canlı Bybit testi silinmedi/zayıflatılmadı. Aynı desendeki ikinci
   sessiz skip — `tests/test_sandbox.py::TestExternalRecipe` — de ortamdan
   kurtarıldı: test artık kendi sahte QQQ.NASDAQ dış kataloğunu kuruyor.
   Skip'ler artık sessiz değil: `pyproject.toml` `addopts`'a `-ra` eklendi,
   CI `--junitxml` + iş özetine skip tablosu yazıyor ve e2e adımı "exit 0 ama
   hiç test geçmedi" durumunu hata sayıyor.

Eklenen test: **39** — 8 robustness (`test_robustness_full_error_surfaced.py`),
9 katalog (`test_catalog_broken_records_visible.py`), 20 hassasiyet
(`test_subcent_price_precision.py`), 1 ağsız zincir
(`test_backtest_run_progress_result_e2e.py::TestBacktestChainOffline`) ve 1
katalog-yazıcı ipucu (`test_fixes.py`); ayrıca `TestExternalRecipe` artık her
makinede koşuyor. Süit: **1874 passed, 1 skipped** (skip = kasıtlı canlı-LLM
smoke testi), `ruff check .` + `ruff format --check .` temiz.

## 2026-08-11 — DeepR: üç YÜKSEK entegrasyon bulgusu kapatıldı

Aynı günün DeepR koşusundan `integration` boyutunun üç YÜKSEK bulgusu.

**1. LLM ucunun varsayılanı çalışmayan bir yerel proxy'ydi.**
`llm_dispatch._build_client()` `ANTHROPIC_BASE_URL` yoksa `localhost:6655`'e
gidiyordu; temiz bir kurulumda her çağrı ölü bir porta düşüp graceful
fallback'i tetikliyor, koşu "Random … (Claude unavailable)" ile normal
görünerek sürüyordu. Varsayılan resmi uç oldu, proxy açık tercih; ulaşılamayan
proxy artık adıyla anılıyor (`LLMEndpointUnreachable`), ve `strategy_studio/
ai.py` aynı değişkeni okuyor (aynı anahtar iki farklı uca gitmiyor). README
hizalandı. Ayrıntı: [[model_secici_ve_gorunurluk]].

**2. Bybit cache kilidi okuma yolunu da kilitliyordu.** `load_bybit_bars`
koşulsuz exclusive kilit alıyor, kilit ~10 dk tutulurken bekleme tavanı 120 sn
idi: bir indirme sürerken aynı seriyi okumak isteyen herkes bloke olup timeout
yiyordu. Yazma zaten atomik olduğu için okuma yolu kilitten çıkarıldı; yazma
yolu kilidi alıp kilit altında tazeden okuyor; bekleme 900 sn'ye çekildi (stale
eşiğinin altında). Ayrıntı: [[parquet_data_catalog]].

**3. Restart sonrası "running" kalan studio satırları uzlaştırılmıyordu.**
`studio_runs`/`optimize_runs`/`ai_loops` sonsuza dek `running` kalıyor, footer
ve optimizer paneli bitmeyecek bir koşuyu poll'luyor, AI loop kalıcı 422 ile
kilitleniyordu. Deployment tarafının crash-only mantığı üç kardeş tabloya da
uygulandı; durum `interrupted` (kesinti ≠ başarısızlık), neden satıra yazılıyor.
Ayrıntı: [[strategy_studio]].

Bu turda eklenen: 46 test (15 + 15 + 16). Tam suite yeşil.

## 2026-08-11 — DeepR dördüncü tur senkronu
- yeni: `wiki/synthesis/nau_deepr_dorduncu_tur_2026_08_11.md` (557 ajan; kapı
  kalibrasyonu, katalog onarımı, motor hızlandırması, kritik güvenlik)
- `index_backtest_via_equity_proxy` → `us_equity_katalog_veri_butunlugu` bağı
  eklendi (öksüzdü)

- 2026-08-13 — DeepR kalan bulgular: `web/templating.py` (server↔routes çift yönlü bağımlılığı, 54 fonksiyon-içi import), `nau_config.py` (77 ortam değişkeninin kataloğu + sürüklenme testi), `auto/log_thinning.py`, `web.shared.SessionRunGuard` / `MAX_LLM_TEXT_LEN` / `invalid_date_range`. Bkz. [[webapp_module_map]].

## 2026-08-14 — Devralma turu senkronu
- yeni: `wiki/synthesis/nau_devralma_turu_2026_08_14.md` (kopan oturumun
  transcript'ten devralınması; iki yakalı sınır, 4 onarım kümesi, pm2)
- yeni: `wiki/concepts/import_aninda_yakalanan_referans.md` — kodun (nau_data,
  templating, testler) atıf yaptığı kavram sayfası yoktu; üç depodaki vakayla
  yazıldı (_static_version takma adı, data bölme şartı, ölü patch seam)
- `webapp_module_map`: `nau_data/` satırı eklendi, `data.py` satırına bölme
  notu düştü; `web/templating.py`'ye Wiki References bloğu eklendi (köprü iki
  yönlü oldu)
- lint: tümü 0 (öksüz kalan devralma sayfasına kavram sayfasından bağ verildi);
  sağlık raporu `lint/2026-08-14_health.md`

## 2026-08-14 — AUTO turu senkronu (bütçe, depolama kökü, kokpit, postmortem)
- `webapp_module_map`: `scripts/auto_postmortem.py` satırı eklendi (deterministik
  koşu postmortem'i, LLM'siz); `web/shared.py` satırına tek depolama kökü
  (NAU_DATA_DIR yönlendirmesinin beş sabite ulaşmaması) notu; `agent_backtest.py`
  satırına bütçe muhasebesi + snapshot + kokpit köprüsü düzeltmeleri.
- `auto_mission_control`: yeni bölüm — continuous modda kokpitin kalıcı donması
  ("aynı state'in iki sunumu" tasarımının iki sunumun aynı SÖZLEŞMEYİ okumaması
  hâlinde ödediği bedel) + snapshot'ın düşürdüğü iki alan.
- Ölçüm notu (postmortem `--calibrate`, 118 oturum): fikir örtüşmesi medyanı
  %54, kazanan/koşu medyanı 0, koşu maliyeti medyanı $1,30. İki alarm eşiği bu
  yüzden düzeltildi — sezgiyle konan eşik medyana denk gelirse alarm ölür.
- AÇIK BOŞLUK: 118 oturumun 62'sinde `session_end` yok. `session_end` 12 çağrı
  yerinde ve hiçbiri dış `finally`'de değil; crash-only uzlaştırma
  (`interrupt_job` + açılışta `_reconcile_studio_jobs`) studio TABLOLARINA
  uygulanmış ama oturum LOGLARINA yayılmamış. Düzeltme kullanıcı onayı bekliyor
  (geçmişe dokunulmaması ve /sessions ilk isteğinde uzlaştırma önerildi).

## 2026-08-14 — Kesinti uzlaştırması KAPANDI (yukarıdaki açık boşluğun cevabı)
Kullanıcı onayıyla uygulandı (c3d9acc): `_reconcile_session_logs_once()` ilk
`/sessions` isteğinde koşuyor; sahipsiz log `outcome="interrupted"` ile kapanıyor.
İki sert kural mutasyonla doğrulandı — geçmişe dokunulmaz (su işareti öncesi
loglar tarihsel kayıt) ve canlı koşu ölü ilan edilmez (`live_run_ids()`).

Aynı turda iki kusur daha çıktı ve kapandı (f43aa5d):
- `/sessions` 500 veriyordu: `_read_events`/`_session_summary` metin dosyalarını
  `encoding` vermeden açıyordu (Windows cp1254 ↔ UTF-8 loglar). ESKİ bir kusurdu,
  uzlaştırmanın getirdiği değil; liste ilk sayfasındaki 25 dosyanın 10'u
  okunamıyordu. Okuyucular + aynı ailedeki yazıcılar UTF-8'e alındı; koruma
  davranış testi değil AST kaynak taraması (kusur POSIX CI'da üremez).
- Su işaretinde saat-alanı yarışı: duvar saati ↔ dosya mtime karşılaştırması
  sınırda rastgele taraf seçtiriyordu. Eşik artık su işaretinin kendi mtime'ı;
  sınırda geri alınabilir taraf (dokunmama) seçiliyor ve bu teste bağlandı.

Süit: 2226 passed. Kalan: uzlaştırma etkisi ancak bir sonraki kesintide görünür
(mevcut `.reconcile_watermark` 19:02'de kuruldu, 62 tarihsel log muaf).

## 2026-08-14 — AUTO koşusu 360° review edilebilir hale geldi

Kullanıcı isteği: "auto loglamasını 360 derece her şeyi tıpkı deepr gibi oluştur
ki sonra review edebileyim". Kayıt zaten genişti (20 olay tipi); eksik olan üç
şeydi ve üçü de review'ın İLK sorularıydı.

1. **Provenance (`nau_provenance.py` → `run_env` olayı).** Metrikler, promptlar
   ve kararlar ADSIZ bir build'e karşı kaydediliyordu; iki koşu arasındaki fark
   bir commit'e bağlanamıyordu. Artık git SHA/branch/kirli-ağaç, 6 paketin
   sürümü, `NAU_*`/`NAUTILUS_*` ezmeleri, python/platform/host/pid yazılıyor.
   İki kural teste bağlandı: koşuyu asla düşürmez (git yoksa `available: False`;
   `git status` başarısızsa `dirty: None` — bilinmeyen "temiz" değildir) ve sır
   yazmaz (KEY/TOKEN/SECRET/PASSWORD içeren ad `<set>`'e iner).

2. **LLM transkripti (`llm_dispatch._transcript_fields`).** `llm_usage` yalnız
   SAYAÇ tutuyordu. Postmortem'in en büyük bulgusu "fikir tekrarı bu sistemde
   norm" idi ve o bulgunun SEBEBİ prompt'ta yaşıyor, sayaçta değil. Metin artık
   yakalanıyor, `<run_id>_artifacts/` altına gz olarak iniyor, JSONL satırında
   yalnız kimlik (path+sha256+karakter sayısı+`clipped`) kalıyor. Kırpma baş+son:
   prompt'un başı sistem yönergesi, sonu o çağrıya özgü istek.

3. **`auto_review.py` — 13 bölümlü markdown, `session_end`'de otomatik.**
   Postmortem terminale basıyor ve kayboluyordu. İki dürüstlük kuralı testle
   sabit: **yokluk sıfır olarak çizilmez** ("robustluk HİÇ başlamadı" ≠ "0 aday
   geçti") ve yargı uydurulmaz. `analyze`/`TH` tek yere indi — iki ayrı kopya,
   eşiklerin sessizce ayrışması demekti.

İlk gerçek çıktı (d4878a43, geçmiş koşu): 16 backtest koştu, robustluk kapısı
HİÇ başlamadı, süre %96 LLM'de geçti, cache ıskası %100, 6 fallback. Bu tablo
tek bir ajan çağrılmadan çıktı.

AÇIK: `/sessions` arayüzü review dosyasına link vermiyor — şimdilik yalnız disk
ve CLI (`python -m auto_review <run_id>`).

## 2026-08-14 — AUTO çalışırken: sabit aralıklı nabız

Review belgesi bitmiş koşuyu anlatıyordu; koşu SÜRERKEN olay üretilmeyen
aralıklar hâlâ ölçülemezdi. `run_heartbeat` (vars. 30 sn) o boşluğu doldurur:
açık işler ve yaşları (`stalled_s`), faz, son adım, bütçe-eşdeğeri kullanım,
RSS, iş parçacığı sayısı. Süreç ölse bile diskteki son nabız "nerede duruyordu"
sorusunu en fazla bir aralık hatasıyla cevaplar — bitiş kaydı olmayan koşularda
review artık "bilinen son hâl" satırını buradan yazıyor.

Yan ürün olarak `run_config` (koşuyu belirleyen tüm env-ezilebilir sabitler) ve
yığın izleri (`degraded` + hata `session_end`) eklendi.

Yazarken kendi kuralımı çiğnedim ve test yakaladı: `float(sp["t0"] or now)`
yazmıştım; `0.0` yanlış-değer olduğu için eski bir span "az önce başladı" diye
raporlanıyordu — takılmayı tam arandığı yerde gizleyen bir kısayol. `_age()`
ile açık `None` denetimine çevrildi ve teste bağlandı.

## [2026-08-15] sync | Yerel LLM + hibrit: dört yol ölçüldü, anlatı düşüşü sessiz değil

Yeni kaynak: `sources/07_yerel_llm_hibrit_olcumu_2026_08_15.md` (llama.cpp CUDA
13.3 + Qwen3.8-27B kurulumu, dört LLM yolunun ölçümü, AUTO koşusu 14ff96e7).

Güncellenen sayfalar: `model_secici_ve_gorunurluk` (üretimde 19/19 doğru
yönlendirme), `kesilme_ve_degrade_gorunurlugu` (anlatı düşüşü + STOP yutulması
düzeltmesi; tavan tırmanışının duvar saatini zorlaması), `llm_maliyet_kaldiraclari`
(AÇIK: hibritte maliyet atfı tek modele yazılıyor), `webapp_module_map`
(llm_client `model_for_purpose`/`hybrid_note`, llm_dispatch iki kullanım yeri).

Açık kalan boşluk: maliyet satırı hibridi bilmiyor — `_llm_cost_usd` tek model
alıyor, koşu maliyeti pinlenmiş modele atfediliyor (ölçülen vaka: 1,02 USD
Claude'un custom_block çağrılarının bedeliyken `or:qwen3.8-27b`'ye yazıldı).

## [2026-08-16] sync | Hibrit koşu ölçümleri: iki tavan, risk-ayarlı kapı, stall watchdog

Yeni kaynak: `sources/08_hibrit_kosu_olcumleri_2026_08_16.md` — altı AUTO
koşusunun ölçümü (üçü asıldı, üçü sonuç verdi), pencere/benchmark tablosu,
geçen üç adayın profili.

Güncellenen sayfalar: `auto_arama_ekonomisi` (iki tavan + körlük şartı),
`auto_kapi_ve_geri_bildirim` (risk-ayarlı benchmark kapısı + seçtiği strateji
sınıfı), `auto_mission_control` (stall watchdog ve neden yama değil araç),
`auto_360_canli_review_iyilestirmeleri` (postmortem None çökmesi),
`model_secici_ve_gorunurluk` (zamanaşımı ayarının sessizce ölü olması).

Açık kalan: asılmaların kök nedeni bilinmiyor (watchdog henüz tetiklenmedi);
`backup/macos-portability` dalının eksik özellikleri main'e taşınmadı.

## [2026-08-16] sync | Çok-sembol kapısı: peer havuzu, venue çözümlemesi, risk-ayarlı ölçüt

Yeni sayfa: `wiki/synthesis/multi_symbol_generalization.md` — peer seçiminin üç
süzgeci (dikiş-farkındalıklı dışlama → venue çözümlemesi → veri filtresi +
`PEER_SAMPLE_SIZE` kırpması), örneklem boyutunun eşik çözünürlüğü olması, ve
üstünlük ölçütünün ana kapıyla ortaklaşması. Sayfa `auto/robustness.py`'nin uzun
süredir ÇÖZÜMLENMEYEN `[[multi_symbol_generalization]]` bağının hedefiydi.

Güncellenen: `auto_kapi_ve_geri_bildirim` (5 peer'lı koşunun sonucu + kapının
risk-ayarlıya çekilmesi), `webapp_module_map` (`backtest_robustness.py` satırı
`peer_is_superior` ve ortak anahtarı yazıyor), `backtest_robustness.py` docstring
(Wiki References artık iki sayfaya bağlı).

Kaynak `09_baglam_ve_butce_olcumu_2026_08_16.md` bu senkronda genişletilmedi;
392287b2 sonrası ölçümler (38bdfeff, 5 peer) ilgili sentez sayfalarında duruyor.

### Açık boşluk: lint kod→wiki köprüsünü GÖRMÜYOR

`wiki_tools.py lint` yalnız `wiki/` içindeki sayfaları tarıyor; Python
modüllerinin `Wiki References` bloklarındaki wikilinkleri kontrol etmiyor. Bu
yüzden `broken_links (0)` raporlanırken `auto/robustness.py` var olmayan bir
sayfaya bağ veriyordu.

Ölçüm: `Wiki References` bloklarında 40 bağ var, **5'i kırık** (blokla sınırlı
tarama yanlış pozitif üretmiyor; tüm dosyayı taramak Python liste literallerini
`[[close[0]]`, `[["QQQ"]]` wikilink sanıyor). Bu senkronda YALNIZ biri kapatıldı
(`multi_symbol_generalization`). Kalanlar:

- `[[nau_deepr_mimari_katman_ayrimi]]` — `auto/__init__.py`, `auto/robustness.py`
- `[[nau_token_tuketim_izleme]]` — `compact_sessions.py`
- `[[nau_token_tuketim_izleme_2026_07]]` — `web/routes/tokens.py`
- `[[ticker_kimlik_degil_o_gunun_etiketi]]` — dört ingest betiği
- `[[max_tokens_tavani_modelin_uslubuna_baglidir]]` — `tests/test_model_by_purpose.py`

İki iş öneriliyor: (1) lint'e kod dosyalarını tarayan bir kol eklemek (yanlış
pozitifleri ayıklamak için yalnız `Wiki References` bloğuna bakmalı), (2) yukarıdaki
sayfaları yazmak. İkisi de bu senkronun kapsamı dışında bırakıldı.

## [2026-08-16] kod incelemesi — kapının tek kopyası, muafiyetin kapsamı

`99a2560..6991f44` incelendi (7 commit, 12 dosya). Beş bulgu, hepsi düzeltildi.

- **Kapı iki kez kopyalanmış, iki kez ıraksamıştı.** Önce ölçüt (2026-08-15 →
  2026-08-16 arası çok-sembol kapısı terk edilen mutlak kuralda kaldı), sonra
  ölçüt hizalandığında GERİ DÜŞME basamağı: ana kapı `annualized_alpha`'ya,
  çok-sembol kapısı `excess_return_fraction`'a düşüyordu. Kural
  `app_constants.benchmark_rejection`'a taşındı — tek kopya. Bekçi:
  `test_both_gates_return_the_same_verdict_for_the_same_metrics`.
- **Muafiyet tek SDK'ya bağlıydı.** `openai.APIConnectionError` ≠
  `anthropic.APIConnectionError`; süreç-içi OpenRouter dalında aynı sıfır-bayt
  çağrı faturalanıyordu. Ayrım hâlâ somut ada bağlı (timeout muafiyeti açılmadı).
- Negatif bütçe değeri 400 yerine sessizce SERT TAVANA açıyordu; studio dış
  katalog taraması sayfa başına iki kez koşuyordu; `resolve_peer_ids` aynı
  ticker iki venue'daysa son-yazan kazanıyordu (artık: sepetteki tam id > katalog
  sırasında ilk).

Süit: 2350 passed / 2 skipped. Ruff temiz.

**Açık boşluk (değişmedi):** Wiki References bloklarında 40 bağdan 4'ü hâlâ
kırık; lint bu bloklara bakmıyor.

## [2026-08-17] Loop sayfası emekliye ayrıldı + iki denetim raporunun doğrulaması

**Kaldırma (25ad8eb → 50d7729).** Kullanıcı Loop sayfasını kullanmadığını
söyledi. İki commit'te yapıldı çünkü `loop_runner.py` sahipsiz bir koruma
taşıyordu: OpenRouter'ın `multiprocessing.Process()` çocuklarını pm2 altında
konsol donmasından koruyan `mp.set_executable(pythonw.exe)`, yalnız
`server → routes/loop → loop_runner → sandbox` import zinciri sayesinde
çalışıyordu. Önce koruma `server.py`'ye açıkça taşındı ve tripwire hedef
değiştirdi; sonra silme yapıldı. Silinen: `loop_runner.py`, `web/routes/loop.py`,
`web/routes/fragments.py`, `legacy/streamlit_app.py`, iki şablon parçası, iki
test dosyası. `state.py`'den yalnız `AppState` gitti — `IterationResult` her
backtest'in dönüş tipi.

**Kapsam dersi:** ilk tüketici taramam `| head` ile kesilmişti ve iki tüketici
(`web/routes/strategy.py`, `web/templating.py`'nin `loop_running` Jinja
global'i) listede görünmedi; silme `ImportError` ile patladı. Silme/taşıma
kararını besleyen taramayı kırpma.

**İki denetim raporu doğrulandı (kod okuyarak + çalıştırarak).** Kapatılanlar bu
oturumda: comprehension bütçe deliği (b3bb253), backtest çocuğunun bellek tavanı
(80f20c8). Raporların ikisi de bu commit'lerden ÖNCE üretilmiş — "En Kritik 3"
listesindeki bellek tavanı maddesi çoktan kapalıydı.

### AÇIK BOŞLUKLAR (doğrulandı, henüz düzeltilmedi)

- **Kill switch dekoratif.** `kill_switch_daily_pct` config'de var
  (`strategy_studio/deploy.py:73`), artefakta yazılıyor (`:152`), docstring
  "realized daily PnL breaches" diye söz veriyor — ama `runner.py`'de kelime hiç
  geçmiyor. Günlük PnL izleyen bir şey yok, `pause()` yalnız elle. Var olmayan
  bir korumadan kötü: kullanıcı buna güvenerek pozisyon büyütür.
- **Auth fail-open.** `_is_authenticated` token boşken `True` dönüyor. Kod bunu
  biliyor ve PM2 altında UYARIYOR (`_warn_if_unauthenticated_and_deployed`);
  doğru düzeltme yeni mekanizma değil, uyarıyı REDDE çevirmek.
- **Path traversal — kanıtlandı.** `web/shared.load_result_snapshot` `run_id`'yi
  doğrulamıyor ve `GET /backtest/result/{run_id}` ham parametreyi geçiriyor.
  Ölçüldü: `..%5C..%5Cevil` → HTTP 200 ve yol `C:\Users\MYDESK\evil.json`'a
  çözülüyor (ters bölü Starlette'in segment eşleşmesine takılmıyor, eğik çizgi
  takılıyor). Kardeş yüzeyin (`data._bybit_cache_path`) testi var, bu atlanmış.
- **MC hep IID.** `run_monte_carlo` varsayılanı `iid_bootstrap`; `block_bootstrap`
  yazılı (`backtest_robustness.py:859`) ve HİÇ seçilmiyor. IID karıştırma
  oto-korelasyonu yok eder → `max_dd_p50/p95` iyimser, ve o değerler kabul
  kapısını besliyor.
- **Eşik tutarsızlığı.** `WFO_MIN_TRADES = 3` vs peer geçerlilik eşiği 5.
- **Non-finite fold'lar paydadan düşüyor** (`_mean`, backtest_robustness.py:692)
  → fold düzeyinde survivorship.
- **Peer sepeti survivorship taşıyor** — yedi isim de hayatta kalan mega-cap.
- **Para tavanı girişte zorlanmıyor** — `_admit_llm_budget` yalnız token bakıyor.
- **Depo hijyeni:** kökte 5 ezik isimli diff dökümü + 6 `deepr_report_*.md`,
  hepsi git'te izleniyor.

### Rapor iddialarının DÜZELTMELERİ (yanlış yönlendirmesin diye)

- Stub adapter'ın provenance'ı VAR: `engine_is_stub` + "SİMÜLE" rozeti
  (2026-08-08). Kalan risk daha ince — bayrak koşu kaydına değil o anki sürecin
  adaptörüne bakıyor.
- Trend filtresi arızası SESSİZ değil: `backtest.py:1789` ilerleme akışına
  yazıyor. Gerçek kusur, bozulmanın artefakta taşınmaması.
- MC bellek tahmini ~4 kat büyük: gerçek çağrı `n_sims=300`, `n_trades` ölçülen
  en yüksek 1.184 (50.000 değil). Üstelik artık 3072 MB tavanın altında.
- "Varsayılan para tavanı $5": kod öyle, DAĞITIM öyle değil —
  `ecosystem.config.js` `AGENT_DEFAULT_MAX_COST_USD: "20"` pinliyor.

## 2026-08-17 — bulgu kapatma turu + kendi işine review

İki denetimin doğrulanmış 14 bulgusu kapatıldı (f0bd66d..60cc0d8, 11 commit),
ardından kendi işine yapılan review'ün 10 maddesi. Üç ölçüm düzeltmenin TÜRÜNÜ
değiştirdi; iki iddia geri çekildi (biri "kapı canlıda açık" — kanıtların ikisi
de araç varsayılanıydı, biri "dış hisse verisi yok" — yanlış katalogda arandım).

Peer sepeti ölçüldü: nominal 7 sembol, ETKİN ~2 bağımsız bahis (SPY↔QQQ 0,95;
PC1 varyansın %62-70'i). GOOGL'ın 498 barı var, diğerlerinin 5.762; QQQ
düzeltilmemiş olabilir. Kapı kararı kullanıcıya bırakıldı.

- wiki/synthesis/nau_bulgu_kapatma_turu_2026_08_17.md (yeni)
- wiki/synthesis/multi_symbol_generalization.md (etkin bağımsızlık ölçümü)
- wiki/synthesis/webapp_module_map.md (5 satır: runner, serve, backtest_robustness, app_constants, web/shared)

## 2026-08-17 (ikinci senkron) — Blocks paneli + canlı koşunun iki ölçümü

- wiki/synthesis/tear_sheet_overlay.md — Blocks paneli: Canvas node dili, akış
  sırası (kaydın dizilişi değil), üç deponun farklı kapsamı
- wiki/synthesis/webapp_module_map.md — `web/tearsheet.py` satırı
- wiki/synthesis/llm_maliyet_kaldiraclari.md — `max_tokens` advisory: 22/39 çağrı
  tavanı aşıyor, medyan ×1,41, en kötü ×1,95; aşım TEK YÖNLÜ olduğu için tavana
  dayanan maliyet tahmini iyimser yanılıyor. Yalnız `custom_block` amacında.
- wiki/synthesis/nau_bulgu_kapatma_turu_2026_08_17.md — canlı doğrulama: iki
  turda 0/15, tıkanma Calmar'a kaydı (8→10), robustness zinciri hiç çalışmadı,
  yani bu turun üç yeni göstergesi HÂLÂ canlıda sınanmadı.

BOŞLUK: peer satırları / ρ₁ rozeti / WFO paydası ilk kapıyı geçen adayı bekliyor.

## 2026-08-17 — AUTO 755b7880: zincir uçtan uca koştu, holdout aritmetiği kırık

Yeni: `wiki/synthesis/nau_auto_kosusu_755b7880_2026_08_17.md`.

Kapanan boşluk: `multi_symbol_generalization`'daki "karar verilmedi" notu kapandı —
`effective_symbols` teşhis olarak eklendi (ağırlıklandırma seçilmedi).

AÇIK KALAN İKİ ÇELİŞKİ (ikisi de ölçülmüş, ikisi de düzeltilmedi):

1. `holdout_days=60` × `holdout_min_trades=20` çifti, boru hattının SEÇTİĞİ
   frekansta imkânsız. Kazanan 2,3 işlem/yıl yapıyor; 60 günde beklenen 0,38.
   Önceki kapılar düşük frekansı ödüllendirirken son kapı yüksek frekans istiyor.
2. WFO ölü: 89 pencerenin 0'ı ≥3 işleme ulaşıyor. Pencere boyutlandırması
   (6ay/2ay) stratejilerin frekansıyla uyumsuz.

Ayrıca kayda geçti: koşu sürerken `backtest_robustness.py` değişti ve spawn
çocuğu diskten yeniden import ettiği için yeni kod aynı koşuda çalıştı —
`run_env.sha` artık çalışan kodu tarif etmiyor.

## 2026-08-17 — söz verip yapmayan yollar turu senkronlandı

`2b3c392..9d6602f` on iki düzeltme wiki'ye girdi:
[[nau_soz_verip_yapmayan_yollar_2026_08_17]]. Önceki kapatma turundan
([[nau_bulgu_kapatma_turu_2026_08_17]]) ayrımı: orada çoğu bulgu "koruma yok"tu,
burada hepsi "koruma VAR ama uygulanmıyor" — ayar okunmuyor, eşik ulaşılamıyor,
filtre sessizce düşüyor, rozet kapıya ulaşmıyor.

Kapanmayan boşluk, açıkça kaydedildi: holdout aritmetiği (`OOS_HOLDOUT_DAYS=60`
ile `HOLDOUT_MIN_TRADES=20` birlikte sağlanamıyor) ve ölçümsüz sekiz performans
maddesi. Lint: 0/0/0/0/0/0.

## 2026-08-17 — holdout aritmetiği kapandı, üç sayfa güncellendi

`2db813b` senkronlandı. [[nau_auto_kosusu_755b7880_2026_08_17]]'nin "aritmetik
olarak ulaşılamaz" bölümüne kapanış eklendi (ölçümle: 1-DAY 41→862 bar, gereken
sıklık %49→%2, eğitim verisinin %85'i korundu).
[[nau_soz_verip_yapmayan_yollar_2026_08_17]]'nin açık listesinden düşürüldü.
[[auto_kapi_ve_geri_bildirim]]'e kapıların çelişebileceği İKİNCİ eksen yazıldı:
frekans. Maliyet-ayarlı ölçü düşük frekansı ödüllendirirken sayım cinsinden bir
eşik yüksek frekans istiyordu; iki kapı aynı adayı farklı BİRİMLERDE ölçüyordu.

Kaydedilen ayrım: eşik oynatılmadı, birim düzeltildi. O koşunun kazananı yeni
pencerede de geçemiyor — değişen, sistemin verdiği cevap.

## 2026-08-18 — wiki-sync: köprünün kod yakası denetlenmiyor (373 bağın 32'si kırık)

Kod tarafı senkron: son wiki senkronundan (7933a00) sonra kod commit'i YOK, tree
temiz. Lint altı kategoride de sıfır. Ama linter'ın görmediği yakada ölçüm:
157 modülde 373 `Wiki References` bağı, 45'i çözülmüyor — 13'ü yanlış pozitif
(10 docstring örneği + 3 dosya-adı kullanımı), **32'si gerçek** ve tamamı
çapraz-vault sızıntısı (kişisel Obsidian vault'unda var olan sayfa adlarının
proje modülüne kopyalanması; en sık `deepr_skill` ×11).

- wiki/concepts/kod_dokuman_koprusu_denetlenmiyor.md (yeni)
- wiki/synthesis/webapp_module_map.md (köprünün denetlenmeyen yakası bölümü)
- Bu turda açılmış 3 test dosyasının kırık bağı düzeltildi; kalan 28 başka
  oturumların dosyalarında — toplu düzenleme wiki-sync kapsamı dışında,
  kullanıcıya raporlandı.

## 2026-08-18 — köprünün kod yakası artık lint'in içinde

`_code_bridge_links()` + `code_broken_links` kategorisi eklendi; çıkış kodu da
sayıyor (rapora yazıp yeşil yanmak, kapatılmak istenen desenin ta kendisiydi).
Docstring `ast` ile okunuyor, gövde sayfa tarafıyla aynı `_bare_targets`
süzgecinden geçiyor — bunun yan kazancı, elle taramada ayıklanan 10 "docstring
örneği" yanlış pozitifinin kendiliğinden düşmesi. Aracın kendi ölçümü: 30 kırık.
Lint bugün 2 ile çıkıyor; sayı zaten oradaydı, artık görünüyor.

- wiki/concepts/kod_dokuman_koprusu_denetlenmiyor.md (kapatma bölümü)
- tools/wiki_tools.py · tests/test_wiki_lint_scans_the_code_side.py (8 test)

## 2026-08-18 — 2db813b doğrulandı: aritmetik doğru, düzeltme eksik, bir regresyon

44 ajanlı denetim (4 mercek + bulgu başına çürütme ajanı): 32 onaylandı, 8 çürütüldü.
Onaylananların dört tanesi yapısal: (1) span sadeleşiyor, kapı gizlice "ömür boyu
>=134 işlem" oluyor ve daha derin katalog onu ASLA açmıyor; (2) holdout_feasibility
yalnız <60 barlık pencerede konuşuyor, yeni pencere ~857 bar — üstelik bir test
n=862 için sessizliği assert ediyor; (3) sıralama (eğitim span'ı) ile mühür
(mühürlü span) 5,7 kat ıraksıyor, 20-113 bandındaki adaylar doğmadan ölüyor;
(4) REGRESYON — 1254 günlük mühür 730 günlük peer penceresinin %100'ünü yuttu.
Ayrıca ekranda ve run_config'te hâlâ 60 gün yazıyor (karar anında 20,7x yanlış).

- wiki/synthesis/nau_holdout_dogrulama_turu_2026_08_18.md (yeni)
- wiki/synthesis/nau_auto_kosusu_755b7880_2026_08_17.md (düzeltme eksik notu)
- wiki/synthesis/auto_kapi_ve_geri_bildirim.md (frekans ekseni kapanmadı, taşındı)
- wiki/synthesis/multi_symbol_generalization.md (regresyon)
