---
title: nautilus_web_app DeepR Üçüncü Tur (2026-08-09, NAU app'e odaklı)
type: synthesis
summary: Üçüncü DeepR koşusu (164 ajan, 37 bulgu) — ilk deneme nautilus_wiki/ alt-dizinine kapsam-sızdı, envanterden isim çıkarılınca düzeldi; kullanıcının seçtiği en kritik 3 bulgu tamamlandı, kalan 34'ü henüz ele alınmadı.
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

## Kullanıcının seçtiği en kritik 3 — tamamlandı, kalan 34 bulgu henüz ele alınmadı

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

## Kapsam dışı (henüz)

Kalan 34 bulgu (37 − kullanıcının seçtiği 3) bu geçişte ele alınmadı — rapor
(`deepr_report_2026-08-09_0040.md`) tam listeyi tutuyor, sıradaki oturuma
kalıyor.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_deepr_ikinci_tur_2026_08_08]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
