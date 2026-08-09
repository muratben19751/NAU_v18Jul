---
title: nautilus_web_app DeepR Üçüncü Tur (2026-08-09, NAU app'e odaklı)
type: synthesis
summary: Üçüncü DeepR koşusu (164 ajan, 37 bulgu) — ilk deneme nautilus_wiki/ alt-dizinine kapsam-sızdı, envanterden isim çıkarılınca düzeldi. 37/37 bulgu karara bağlandı (kullanıcı "hepsini yap" dedi); 35'i düzeltme/test aldı, 2'si aksiyon gerektirmedi (bkz. ilerleme listesi).
key_concepts:
  - crash_only_design
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/nau_deepr_ikinci_tur_2026_08_08.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-09
---

# DeepR Üçüncü Tur — 2026-08-09 (NAU app'e odaklı)

[[nau_deepr_ikinci_tur_2026_08_08]] 33 bulguyu (#45–77) kapatmıştı. Kullanıcı
`/mDeep`'i tekrar (argümansız, tam proje) tetikledi; **ilk koşum (203 ajan)
kapsam dışına kaydı**: Faz-0 envanterinde `nautilus_wiki/` dizini "hariç tut"
diye isimlendirilince ajanlar paradoksal biçimde tam tersine ona kilitlendi —
28/28 bulgu gerçek uygulama yerine `nautilus_wiki/tools/wiki_tools.py`
hakkında çıktı. Bu, additional-working-directory sızıntısından (bkz. ikinci
beyin: `workflow_ek_dizin_sizintisi`) FARKLI yeni bir Workflow kapsam-sızıntısı
varyantı: hedef İÇİNDEKİ bir alt-dizini dışlamak için envanter metninde adını
anmak, dikkati oraya çekebiliyor. "sonra tekrar sor" ile ertelendi; kullanıcı
"NAU app'e odaklayarak tekrar tetikle" deyince envanterden `nautilus_wiki/`
adı TAMAMEN çıkarılarak yeniden koşuldu (164 ajan) — bu kez 37 bulgunun
tamamı gerçek uygulama hakkında, 0 nautilus_wiki sızıntısı. Rapor:
`deepr_report_2026-08-09_0040.md` (repo kökü; ilk sızıntılı koşumun raporu
`deepr_report_2026-08-08_2320.md` olarak arşivde kaldı, kullanılmadı).

## Kullanıcının seçtiği en kritik 3 — tamamlandı, kalanlar üzerinde devam ediliyor

1. **`composer._load_custom_blocks()` import-zamanı çökme riski [YÜKSEK].**
   Bu fonksiyon `import composer` sırasında modül-seviyesinde çağrılıyor;
   `custom_block_store.list_custom()` registry.json okunamadığında
   `RegistryUnavailable` fırlatır (bilinçli tasarım — bkz.
   `custom_block_store.py` docstring'i, boş registry ile karıştırılmasın
   diye), ama bu istisna `_load_custom_blocks()`'tan yakalanmadan çıkıp
   `import composer`'ı düşürüyordu — ona bağımlı her route modülünü,
   dolayısıyla tüm sunucuyu. Düzeltme: `cbs.list_custom()` artık ayrı bir
   `try/except`'te, hata durumunda uyarı loglayıp bu koşum için sıfır
   custom-block evaluator'ıyla devam ediyor. 3 yeni test
   (`tests/test_block_registry_resilience.py`), mutasyon testiyle doğrulandı.
2. **AUTO/`_agent_worker` hiçbir testte gerçek uçtan uca sürülmemişti.**
   [[nau_deepr_ikinci_tur_2026_08_08]]'deki #47 bölmesi (A0-A11b) her
   çıkarılan yardımcıyı ayrı ayrı test etmişti; `TestContinuousCircuitBreaker`
   gerçek `_agent_worker`'ı çağırıyordu ama yalnız Faz-0 erken-hata yolunda.
   Sealed-holdout promosyon kapısının (AUTO'nun finansal bütünlük kontrol
   noktası) Faz 0→1→2→3→4→5 boyunca gerçek bir winner/promotion'a kadar
   sürüldüğü hiçbir test yoktu. `tests/test_agent_worker_e2e.py`: yalnız 4 dış
   sınır (`load_bybit_bars`, `propose_composed_strategy`,
   `run_backtest_guarded`, `run_robustness_guarded`) mock'lanıp gerisi gerçek
   koşuyor — biri promosyonun geçtiği, biri mühürlü holdout'un
   yetersiz-işlem sayısı yüzünden reddettiği iki senaryo. İkinci test
   mutasyon testiyle doğrulandı: `_holdout_promotion_verdict` geçici olarak
   her zaman `True` dönecek şekilde bozulunca red testi gerçekten kırıldı,
   düzeltme geri alınınca yeşile döndü — testin gerçekten kapıyı sınadığı,
   sahte-yeşil olmadığı kanıtlandı.
3. **`repair_massive_intraday.py` — ingest'in her rutin çalışması TF
   düzeltmesini sessizce geri alıyordu.** Kullanıcının bu projeyle
   ilgisiz kendi ayrı script'i (untracked, `git status: ??` — [[nau_deepr_ikinci_tur_2026_08_08]]'deki #60/#61 ile aynı gerekçeyle
   commit dışı); kullanıcı onayıyla diskte düzeltildi + test eklendi
   (`ingest_equities.build_tf_bars()` her TF'yi 1-MINUTE kataloğundan sıfırdan
   yeniden üretiyor — yalnız TF dosyasını düzeltmek bir sonraki rutin
   ingest'te kayboluyordu), ama proje git geçmişine hiç girmedi.

## Kalan bulgular üzerinde devam (2026-08-09, aynı gün)

Kullanıcı kalan 34 bulgunun tam listesini istedi, sonra "devam" dedi — kalan
3 YÜKSEK bulgudan başlayarak sırayla ele alınıyor (en düşük riskliden en
yükseğe): birim test boşluğu → CI yapılandırması → cross-module mimari
sorunu. Rapor (`deepr_report_2026-08-09_0040.md`) tam listeyi tutuyor.

1. **`delete_custom_batch` hiçbir testte yoktu [YÜKSEK, tamamlandı].**
   Kardeşi `save_custom_batch` rollback dahil test edilmişti (bkz.
   `tests/test_auto_360_fixes.py::test_custom_batch_validator_failure_rolls_back_registry`),
   `delete_custom_batch` yalnız `cleanup_agent_run` üzerinden DOLAYLI ve
   tek-gerçek-silmeli bir senaryoda kapsanıyordu. 5 yeni doğrudan test
   eklendi (aynı dosyaya, kardeşinin hemen yanına): çoklu-isim atomik silme,
   registry'de olmayan isimlerin zararsız yok sayılması, boş girdi, geçersiz
   isim reddi (hiçbir I/O olmadan), ve kısmi-hata rollback'i. Rollback testi
   mutasyon testiyle doğrulandı: `_registry_transaction`'ın except bloğu
   geçici olarak devre dışı bırakılınca HEM bu yeni test HEM
   `save_custom_batch`'in var olan rollback testi aynı anda kırıldı (ikisi
   aynı ortak transaction mekanizmasını paylaşıyor) — düzeltme geri
   alınınca ikisi de yeşile döndü.
2. **Tek gerçek E2E testi CI'da `continue-on-error: true` [YÜKSEK, tamamlandı].**
   Bu ayar 2026-08-08 DeepR'da bilinçli eklenmişti (canlı Bybit ağ hatası
   pipeline'ı bloklamasın diye) ama aynı taşı iki kez kırıyordu: gerçek bir
   regresyon da yalnız kırmızı GÖRÜNÜYOR, PR'ı bloklamıyor. Kullanıcıya 3
   seçenek sunuldu (retry+kaldır / yalnız kaldır / olduğu gibi bırak);
   **"retry (3x) + continue-on-error kaldır"** seçildi. `.github/workflows/ci.yml`:
   adım artık PowerShell döngüsüyle en fazla 3 kez deniyor, biri geçerse
   yeşil (geçici ağ sorunu emilir), üçü de düşerse (gerçekten tekrarlanan
   hata = muhtemelen gerçek regresyon) adım GERÇEKTEN başarısız olup PR'ı
   bloklar. Yeni bağımlılık eklenmedi (üçüncü parti retry action yerine düz
   `pwsh` döngüsü). Mantık PowerShell tool'uyla ayrı ayrı simüle edilip
   doğrulandı (hep-başarısız → 3 deneme + red; 3.'de başarı → erken çıkış +
   yeşil) — gerçek bir CI koşumu tetiklenmeden.

3. **Route modülleri birbirinin private (`_`) state'ine erişiyordu [YÜKSEK, tamamlandı].**
   En riskli/en geniş kapsamlı olduğu için bilinçli olarak sona bırakıldı.
   `studio.py` tek başına `backtest.py`'den 4, `strategy.py`'den 2 fonksiyonu
   `_`-önekli isimleriyle içe aktarıyordu (public eşdeğerleri yoktu — hepsi
   alt çizgisiz yapıldı: `session_drafts`, bare `drafts` DEĞİL, çünkü birkaç
   çağrı noktası zaten `drafts` adında yerel değişken kullanıyor, aynı isimli
   fonksiyonu gölgeleyip `UnboundLocalError` verirdi). Daha riskli olan:
   `studio.py` `agent_backtest.py`'nin ham `_AGENT_LOCK`/`_AGENT_PROGRESS`'ini
   import edip elle tarıyordu — bu kilidin 2026-07-14 tarihli belgelenmiş bir
   deadlock geçmişi var (`tests/test_lock_nesting.py`). Çözüm: yeni
   `newest_active_run_id()` — hem `studio.py`'nin taramasını HEM
   `agent_backtest.py`'nin `page()`'indeki AYNI taramanın birebir kopyasını
   tek fonksiyona indirdi; `studio.py` artık kilide hiç dokunmuyor. Mutasyon
   testiyle doğrulandı. `SESSION_LOG_DIR` (3. alt-bulgu, `_`-öneksiz ama aynı
   "yanlış sahiplik" kokusu) `web/shared.py`'ye taşındı — `sessions.py` ve
   `tearsheet.py` artık `agent_backtest.py`'ye değil oraya bakıyor.

   **Süreç notu:** önce `GET /studio` için hiç olmayan bir characterization
   testi yazıldı (`tests/test_studio_page.py`) — refactor'dan ÖNCE, güvenlik
   ağı olarak. Tam suite iki kez koşuldu: ilk koşum yeni testlerin KENDİ
   bir kusurunu yakaladı (`_AGENT_PROGRESS` process-global paylaşılan durum;
   iki test boş başladığını varsaymıştı, ki suite'in geri kalanı çalıştıktan
   sonra bu doğru değil) — snapshot/clear/restore fixture'ıyla düzeltildi,
   simüle edilmiş kirlenmeye karşı doğrulandı. İkinci koşum: 1170 geçti, 0
   kaldı.

Kullanıcının bu geçişte seçtiği 6/6 YÜKSEK bulgu tamamlandı. Kullanıcı sonra
kalan 34'ün tam listesini, ardından ORTA listesini istedi, sonra **"hepsini
yap"** dedi — kalan 31 ORTA/DÜŞÜK/BİLGİ de sırayla ele alındı. Rapor
(`deepr_report_2026-08-09_0040.md`) tam listeyi tutuyor; her madde ayrı
commit, aşağıda yalnız gerçek bir sürprizi/kararı olanlar detaylandırılıyor,
geri kalanı tek satır.

## Kalan 31 (ORTA/DÜŞÜK/BİLGİ) — "hepsini yap"

- ✅ `NAU_ACCESS_TOKEN` fail-open — pm2 (PM2_HOME) altında token boşsa artık uyarıyor.
- ✅ `atr_period`/`commission_pct` sunucu-taraflı doğrulama/clamp (composer.validate() + backtest.py).
- ✅ `codegate.py` LShift DoS — `_check_pow`'un eşdeğeri, `_fold_literal`'ın "bitwise büyümez" varsayımı LShift için yanlıştı.
- ✅ `_terminal_message` run_id doğrulaması — path-traversal dosya-varlık oracle'ı kapandı.
- ✅ `_index_rows()` artık `_bybit_rows()` ile aynı TTL cache deseninde.
- ✅ `_env_float`/`_env_int` `app_constants.py`'ye konsolide edildi (3 kopya → 1).
- ✅ **`backtest.py`'de ~467 satır ölü kod silindi** (`run_backtest_node`/`run_composed_backtest_node`,
  BacktestNode yolu) — **beklenmedik bulgu**: `webapp_module_map.md`'nin "Uçtan uca akış" bölümü bu
  kodu hâlâ AKTİF mimari diye belgeliyordu ("katalogda veri varsa → BacktestNode"), ama
  `sandbox.run_backtest_guarded` repo genelinde bu iki fonksiyonu SIFIR yerde çağırıyordu — wiki, kod
  `sandbox.py`'nin tek-yol subprocess mimarisine geçtikten sonra hiç güncellenmemiş. Akış bölümü
  düzeltildi; [[backtest_node]] (Nautilus'un kendi BacktestNode API'si, hâlâ geçerli genel doküman)
  "webapp'te ölçüldü" notlarına "bu entegrasyon kaldırıldı" açıklaması eklendi.
- ✅ `indicators.py` vs `chart_indicators.py` SMA/EMA/RSI — **bulgu yeniden doğrulanınca kısmen
  geçersiz çıktı**: chart_indicators.py'nin üçü de zaten O(n) (running sum / incremental EMA / Wilder
  recurrence), iddia edilen performans farkı yok. Kalan kopya kasıtlı: chart tarafı zaman eksenine
  hizalı None-dolgulu aynı-uzunlukta dizi ister, indicators.py backtest sinyali için yalnız hesaplanan
  değerleri. Kod değişmedi, açıklayıcı yorum eklendi.
- ✅ `repair_massive_intraday.py` (untracked, commit dışı — kullanıcının kendi scripti) 3 madde birden:
  `_fixed()` artık NaN/inf/overflow'u satır numarasıyla reddediyor; `.bak` her onarımda tazeleniyor
  (eskiden yalnız ilkinde alınıyordu — "son onarımı geri al" aslında hepsini geri alıyordu, mutasyon
  testiyle doğrulandı); yarım borsa günleri artık `--expected-minutes` ile açıkça destekleniyor (sabit
  390 varsayımı gerçek kısmi-veri hatalarını yakalama işlevini korumak için gevşetilmedi, yalnız
  operatör bilinçli override edebiliyor).
- ✅ `download_grouped_daily.py` (untracked — kullanıcının kendi scripti): eksik "T"/"t" alanı artık
  kardeşi (geçersiz TİP) ile aynı şekilde reddediliyor, sessizce epoch-0/"" yazılmıyor.
- ✅ `loop_runner.py` `_try_log` + `strategy_studio.py` başlangıç reconciliation'ı — ikisi de kardeş kod
  yollarının (backtest.py `run()`, aynı dosyanın kendi log.debug'ı) zaten sahip olduğu "sessizce yutma
  yerine logla" düzeltmesini almamıştı; ikisi de artık `log.warning(..., exc_info=True)`.
- ✅ `backtest.py` Index tarih çözümlemesi — `except (ValueError, Exception)` fiilen `except Exception`
  idi; bozuk cache okuması "tarih formatı yanlış" diye yanlış etiketlenip hiç loglanmıyordu.
  `_resolve_index_date_range()` olarak çıkarıldı (doğrudan test edilebilir olsun diye), yalnız
  ValueError'ı yakalıyor; dış handler artık logluyor da.
- ✅ `data.py` dosya kilidi/atomic-write yardımcıları (`_cache_lock`, `_atomic_to_parquet`,
  `_atomic_write_json`) sıfır doğrudan testten 14 teste çıktı — M121 stale-lock regresyonu
  (eşik eskiden 60s bekleme-zaman aşımıyla AYNIYDI, uzun backfill kilidini kırıyordu) artık
  doğrudan kilitleniyor: `os.utime` ile 65s-eski kilit simüle edilip KIRILMADIĞI, eşik geçici
  olarak 1800s→60s'ye düşürülünce aynı tarihsel hatanın gerçekten tekrarladığı kanıtlandı.
- ✅ `_bybit_gap_frames` (önbellek ortasındaki delikleri hedefli dolduran fonksiyon) 7 yeni
  testle kapsandı — mevcut testler yalnız baştan/sondan genişletmeyi kapsıyordu. Test yazımı
  sırasında kendi hatamı yakaladım: boş `pd.DatetimeIndex([])` tz bilgisini kaybediyor, tz-aware
  `Timestamp` ile karşılaştırılınca `TypeError`; test helper'ına `tz=` eklenerek düzeltildi
  (üretim kodu değil, yalnız test fixture'ı).

**18/19 ORTA'ya ayrı görev numarası verildi — 19.su (`repair_massive_intraday.py` sıfır test
kapsamı, Birim Testleri bölümü) ayrı bir iş GEREKTİRMEDİ:** aynı script'in YÜKSEK #3 (TF-only
fix) çalışması sırasında zaten oluşturulan `tests/test_repair_massive_intraday.py`, sonraki
`.bak`/half-day/`_fixed()`-doğrulama/`StopIteration` düzeltmeleriyle 18 teste çıkmış durumda —
bulgu bu turun SONUNDA fiilen zaten karşılanmış hâlde bulundu, doğrulandı, ayrı commit
gerekmedi.

## 10 DÜŞÜK + 1 BİLGİ (turun kapanışı)

- ✅ Ticker registry JSON çift `json.loads()` — `discover_index_tickers()` ve
  `_index_rows_uncached()` artık paylaşılan `_read_ticker_registry()` üzerinden okuyor; yan
  etki: bozuk registry artık `_index_rows_uncached()` için de non-fatal (eskiden
  `JSONDecodeError` sızardı). `/backtest/tickers`'ın limitsiz `<option>` listesi BİLİNÇLİ
  dokunulmadı — native HTML5 `<datalist>` client-side substring-autocomplete için tüm listeye
  ihtiyaç duyuyor.
- ✅ `composer.py` kalan 3 `print()` → `logging.getLogger(__name__).warning(...)` (kardeş dal
  zaten logluyordu, tutarsızlık giderildi).
- ✅ `/login` rate-limit artık Cloudflare'in `CF-Connecting-IP` başlığını tercih ediyor —
  tünel arkasında `request.client.host` HER ZAMAN tünelin kendi local peer'ıydı, "IP-bazlı"
  limit fiilen TEK global limitti (gerçek saldırgan IP'sinden bağımsız). Mutasyon testiyle
  doğrulandı.
- ✅ `agent_backtest.py` boyutu — **yeniden doğrulanınca büyük ölçüde zaten ele alınmış çıktı**:
  [[nau_deepr_ikinci_tur_2026_08_08]]'in #47 planı (A0-A9) bu turdan ÖNCE tamamlanmış (~40
  modül-seviyesi yardımcı çıkarılmış, her biri test edilmiş — 53 test hâlâ yeşil). Dosya
  BİLİNÇLİ olarak büyük kalıyor (`test_lock_nesting.py`'nin AST taraması bu dosyayı hedefliyor;
  alt-pakete bölmek o regresyon testini kör bırakırdı). Kalan riskli adımlar (Faz 2
  lookahead-generation, Faz 0 veri/holdout yükleyici) kasıtlı olarak ayrı dikkatli oturuma
  ertelenmiş durumda kalıyor. Kod değişmedi, docstring güncellendi.
- ✅ Composer sayfası dil tutarsızlığı — `catalog_list.html`'in 3 buton tooltip'i (▶/✎/⎘)
  Türkçe'ydi, panelin geri kalanı (tablo başlıkları, diğer butonlar) İngilizce; İngilizce'ye
  çevrildi. **BİLİNÇLİ dokunulmadı**: "💬 AI ile düzenle" butonları — bunlar Türkçe bir LLM-chat
  düzenleme özelliğinin giriş noktası (empty-state mesajı, ilk chat balonu, modele giden prompt
  hepsi Türkçe, `web/routes/strategy.py`); yalnız butonu çevirmek özelliği DAHA tutarsız
  yapardı, daha tutarlı değil.
- ✅ Katalogda parquet yoksa `StopIteration` opak çökme (`repair_massive_intraday.py`, untracked)
  — yeni `_find_parquet()` artık "bu ticker/timeframe hiç ingest edildi mi?" diyen açık bir
  `FileNotFoundError` fırlatıyor.
- ✅ `custom_block_store.save_custom` artık kardeşi `save_custom_batch`'in `_registry_transaction()`
  rollback'ini kullanıyor — dosya yazılıp registry yazımı arada başarısız olursa (disk dolu vb.)
  artık sahipsiz dosya/kod-metadata uyumsuzluğu kalmıyor, tam rollback.
- ✅ `delete_custom()`'ın bellek-unregister hatası artık kardeşi `delete_custom_batch` gibi
  `log.exception` ile loglanıyor (eskiden sessizce yutuluyordu).
- ✅ `chart_indicators.py` sıfır testten 34 teste çıktı — tüm saf fonksiyonlar +
  `indicators_for_spec`'in her blok dalı. İki ince davranış testle kilitlendi:
  `ema_cross`/`macd_cross` aynı fingerprint namespace'ini paylaşıyor (aynı periyottaki ikinci
  blok yeni çizgi çizmiyor), aynı periyottaki entry+exit `rsi_threshold` çifti tek pane
  paylaşıyor.
- ✅ `_base_ccy` sıfır testten (tek kullanım yeri `monkeypatch` ile tamamen stub'lanmıştı) 8
  teste çıktı; `_instrument_meta`'nın bybit dalındaki aynı süfiks-çözme döngüsü artık
  `_base_ccy`'yi çağırıyor (kopya kod gitti).
- ✅ `chart_indicators._series`'in `zip()`'i artık `strict=True` — times/vals uzunluk uyuşmazlığı
  (hep aynı DataFrame'den geldiği için asla olmaz) artık sessizce kırpmak yerine `ValueError`
  fırlatıyor; `chart.py` çağrıyı zaten geniş bir except-and-degrade ile sarmalıyor.
- ⏭️ Kalan tek BİLGİ ("ruff (E,F,I,W,UP) tüm proje genelinde temiz") aksiyon gerektirmiyordu —
  saf olumlu doğrulama, düzeltilecek bir şey yok.

**Sonuç: 37/37 bulgu karara bağlandı** (6 YÜKSEK + 19 ORTA + 10 DÜŞÜK + 2 BİLGİ — "En Kritik 3"
özet bölümündeki 3 tekrar hariç ham rapor sayısı). `repair_massive_intraday.py` +
`download_grouped_daily.py` (ve ikisinin testleri) kullanıcının kendi scriptleri — bilinçli
olarak commit dışı kaldı, yalnız diskte düzeltildi + test edildi.

Branch `fix/auto-gate-and-artifact-identity` main'e fast-forward merge edildi (58 commit,
1312 test yeşil), branch silindi (local + origin).

## Ek: DeepR #47'nin son adımı da kapandı (aynı gün, ayrı istek)

37/37 kapandıktan sonra "başka bir düzeltme var mı" sorusuna verilen taramada iki şey bulundu:
(1) bu sayfanın kendi docstring notu [[nau_deepr_ikinci_tur_2026_08_08]]'deki #47 planının Faz 0
yükleyicisinin (A11a/A11b, planın en riskli adımı) hâlâ ertelendiğini söylüyordu — halbuki kod
zaten tamamlamıştı (`_load_timeframe_bars`+`_TfLoader` mevcut, `TestContinuousCircuitBreaker`
yeşil); yalnız docstring güncellendi. (2) #47'nin GERÇEKTEN tek açık kalemi ortaya çıktı: Adım 10
(Faz 2'nin lookahead-generation bloğu). Kullanıcı onayıyla tamamlandı:
`_propose_next_strategy` olarak çıkarıldı (davranış korunarak, literal taşıma). Test yazımı iki
ince pre-existing davranışı ortaya çıkardı (ikisi de DEĞİŞTİRİLMEDİ, yalnız testle sabitlendi):
`degraded_terminal` burada Faz-1'in aksine propagate olmuyor (bir önceki spec'e düşülebildiği
için `TerminalLLMError` genel except'e düşüp yutuluyor); ve o özel alt-durumda `spec` raise'den
ÖNCE zaten yeni teklife atanmış oluyor, yani "önceki stratejiyle devam" log mesajı tam doğru
değil. 11 yeni test, ikisi mutasyon testiyle doğrulandı (cancel/bütçe re-raise'i geçici olarak
kaldırıp testlerin gerçekten kırıldığını, geri alınca yeşile döndüğünü kanıtladı). Tam suite:
1323 yeşil. Ayrıntı: [[nau_deepr_ikinci_tur_2026_08_08]]. A12 (alt-paket bölmesi) hâlâ bilinçli
olarak plan dışı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[backtest_node]]
- [[nau_deepr_ikinci_tur_2026_08_08]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
