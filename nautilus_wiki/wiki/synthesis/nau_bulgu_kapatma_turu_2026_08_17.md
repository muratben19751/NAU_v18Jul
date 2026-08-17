---
title: NAU — bulgu kapatma turu (2026-08-17)
type: synthesis
summary: İki denetim raporundan doğrulanan 14 bulgunun sırayla kapatılması, ardından kendi işine yapılan review'ün 10 bulgusu. Üç ölçüm düzeltmenin TÜRÜNÜ değiştirdi; iki iddia geri çekildi. Kalıcı ders - eşik değil, ölçüm önce.
sources:
  - https://github.com/muratben19751/NAU_v18Jul
key_concepts:
  - auto_kapi_ve_geri_bildirim
  - multi_symbol_generalization
related:
  - wiki/synthesis/nau_deepr_toplu_sertlestirme_2026_08.md
  - wiki/synthesis/multi_symbol_generalization.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/strategy_studio.md
last_updated: 2026-08-17
---

# NAU — bulgu kapatma turu (2026-08-17)

`f0bd66d..60cc0d8`, on bir commit. Girdi: iki denetim raporundan **doğrulanmış**
14 bulgu (raporların dört iddiası yanlış çıkıp elenmişti), çıktı: hepsi kapalı
+ kendi işine yapılan bir review'ün 10 maddesi.

## Kapatılanlar

| # | bulgu | ne yapıldı |
|---|---|---|
| 1 | Kill switch dekoratif | `PaperRunner` monitor thread'i; `check_kill_switches` ölçer, `pause()` + satıra gerekçe |
| 2 | Auth fail-open | pm2+token'sız her istek 503; kaçış kapısı `NAU_ALLOW_NO_AUTH` |
| 3 | `load_result_snapshot` path traversal | `_snapshot_path` tek kural, `[0-9a-f]{8}`; okuma+yazma ondan geçer |
| 4 | Monte Carlo hep IID | `block_bootstrap` varsayılan + `pnl_autocorr_lag1` rozeti |
| 5 | İşlem eşiği 3 vs 5 | `app_constants.MIN_DECISION_TRADES = 5`, üçü tek kaynaktan |
| 6 | Para tavanı girişte yok | `_admit_llm_budget` parayı da sayar |
| 7-8 | Depo hijyeni, requirements sapması | beş bozuk döküm silindi; liste pyproject'ten türetildi + parite testi |
| 9 | `_LAST_RESULT` sınırsız | LRU + 6 sa TTL + ölçümden 24 tavan |
| 10 | CSRF izin listesi istekten türüyor | yapılandırma varsa TEK kaynak o |
| 11 | Sayısal çıpa uykuda | deterministik çıpa testi; `capture_baseline.py` neden uygulanamadığını yazar |
| 12 | CI kapsam/tek OS | `--cov-fail-under=75` + ubuntu ayağı |
| 13 | Peer sepeti survivorship | ölçülemedi → düzeltme değil BEYAN (ama bkz. aşağıdaki ölçüm) |
| 14 | Non-finite fold'lar | payda görünür (`windows_scored`, `scored_label`); kapı oynatılmadı |

## Ölçüm, düzeltmenin türünü değiştirdi

Üç bulguda "raporun önerdiği düzeltme" ile "doğru düzeltme" farklı çıktı, ve
farkı ölçüm gösterdi:

* **Blok bootstrap.** AR(1) rho=0,6'da IID p95 −1,50 iken blok −2,17 (%45 sert).
  Ama deponun GERÇEK işlem dizisinde (451 işlem) lag-1 oto-korelasyon sıfıra
  yakın ve iki yöntem yüzde-puanın yüzde biri içinde aynı. Mekanizma gerçek,
  bugün bağlayıcı değil → varsayılan yine değişti (bedeli sıfır) ama yanına
  ρ₁ rozeti kondu: hangi rejimde olunduğunu okuyan kişi tablodan değil kendi
  koşusundan görsün.
* **İşlem eşiği.** 178 gerçek WFO penceresinde 3-4 bandında TEK pencere yok —
  eşiği yükseltmenin bedeli sıfır. Aynı ölçüm aranmayan bir bulgu verdi:
  hiçbir pencere 3 işleme bile ulaşmamış, yani **WFO fiilen hiç konuşmuyor**
  (pencere boyutlandırmasının sorunu).
* **Fold paydası.** 356 pencere-metrik çiftinin ancak 216'sı ortalamaya girmiş
  (%60,7). Düzeltme aritmetikte değildi — NaN'ı ortalamaya katmak zaten yanlış;
  yanlış olan onu SESSİZCE atmaktı. Payda görünür kılındı, kapı oynatılmadı.

Kavramın kendisi ikinci beyinde ayrı bir sayfa: `wiki/concepts/olcum_duzeltmenin_seklini_degistirir.md`
(bu wiki'nin dışında, o yüzden wikilink değil — depolar arası bağ linter'da kırık görünür).

## Geri çekilen iki iddia

**"Kapı canlıda AÇIK"** — `pm2 env` çıktısında `PM2_HOME` yok diye erişim
kapısının kapalı olduğunu, uygulamanın tünelden kimlik doğrulamasız servis
ettiğini raporladım. Yanlıştı. `pm2 env <id>` süreçten değil YAPILANDIRMADAN
okur; doğrudan ölçüm (pm2 altında üç satırlık geçici bir app) `PM2_HOME`'un
çocuk süreçte VAR olduğunu gösterdi. İkinci "kanıt" da araç varsayılanıydı:
`urllib.urlopen` yönlendirmeyi takip edip `303 → /login → 200` zincirini tek
bir `200` gibi gösterdi. Kod (tek kurallı `_is_deployed`) doğru olduğu için
kaldı, gerekçesi düzeltildi — yanlış gerekçe sonraki okuyucuyu olmayan bir
arızayı aramaya gönderirdi. Ders: **iki kanıt aynı türdense tek kanıttır**;
bağımsızlık kaynak sayısında değil gözlem türünde aranır.

**"Dış hisse verisi bu kutuda yok"** — yalnız Nautilus kataloğuna bakıp
söylemiştim; `~/.cache/nautilus_web_app/equity_catalog` altında 16 enstrüman
duruyor. Bu, #13'ü ölçülebilir hâle getirdi (aşağıda).

## Peer sepeti: nominal 7, etkin ~2

Beyanla bırakılan #13, veri bulununca ölçüldü — ve survivorship'ten daha keskin
bir şey çıktı: sepet **7 sembol içeriyor ama ~2 bağımsız bahis taşıyor**.

| sepet | gün | ort ρ | PC1 | etkin/nominal |
|---|---|---|---|---|
| 7'li (tam) | 497 | 0,54 | %62 | 2,37 / 7 |
| GOOGL'suz 6 | 4.173 | 0,63 | %70 | 1,95 / 6 |
| QQQ'suz 5 | 5.761 | 0,58 | %67 | 2,07 / 5 |
| yalnız üç ETF | 4.173 | 0,82 | %88 | 1,27 / 3 |
| yalnız üç mega-cap | 5.761 | 0,46 | %64 | 2,11 / 3 |

22 yıllık pencerede sonuç değişmiyor; SPY↔QQQ **0,95** (pratikte aynı seri).
Kapı `pass_rate >= 0.7`'yi "5 bağımsız testin 4'ü geçti" diye okuyor. İki veri
kusuru da çıktı: **GOOGL 498 bar** (2024-08'den; diğerleri 5.762), ve **QQQ**
4.174 bar + `split suspicion` (düzeltilmemiş seri olabilir) uyarısına rağmen
sepette. Karar kullanıcıya bırakıldı — en ucuz seçenek `effective_symbols`'ü
artefakta ve ekrana yazmak. Ayrıntı: [[multi_symbol_generalization]].

## Kendi işine review

On bulgu, ikisi bloklayıcı ve ikisi de bu oturumun KENDİ temasını ihlal ediyordu:

* `scored_label`, naive bacağı olmayan bir spec'te (belgelenmiş geri düşme)
  dördü de sayı üretmiş sağlıklı bir aggregate'te "0/4 · ÇOĞU SAYI ÜRETMEDİ"
  diyordu — **alarm tarafına yalan**. Payda artık karar metriğinden sayılıyor
  ve geri düşme kuralı `wfo_test`'ten çağrılıyor (dördüncü kopya olmasın).
* Üç test `Path("server.py")` ile cwd'ye bağlıydı; `cd tests && pytest`
  düşürüyordu. Ara çözüm `conftest.REPO_ROOT` TAM SÜİTTE `ImportError` verdi
  (`tests/browser/conftest.py` aynı adı taşıyor) — kök artık dosyaya göre.
* Kapsam eşiği pyproject'teydi ve HER `--cov` koşumunu bağlıyordu (tek dosya →
  %10 ile kırmızı). %75 tam süitin ifadesi; eşik CI adımına taşındı ve
  `test_ci_workflow_contract.py` orayı denetliyor.

Kalanlar: kill switch'in kasıtlı durdurmada satırı `failed` yapması (dar yarış),
CSRF öğüdünün her istekte tekrarlaması (log seli), sayısal çıpanın ölçüldüğü
platforma bağlanması, ve üç küçük. `_run_cost`'un sıcak yolda olması **ölçüldü**:
3,2 µs/çağrı, yanındaki sağlayıcı çağrısı yüzlerce ms — sorun değil, sayı
docstring'de duruyor ki aynı soru üçüncü kez sorulmasın.

Süit 2.452 test, kapsam %79,65.

## Canlı doğrulama: göstergelerin üçü HÂLÂ sınanmadı

Aynı gün bir AUTO koşusu (`8af5d495`, QQQC.NASDAQ, 83 dk, $4,93) iki tur döndü ve
**iki turda da 0/15** aday alfa kapısını geçti. Eleme sebeplerinin kırılımı iki
tur arasında anlamlı biçimde kaydı:

| eleme sebebi | tur 1 | tur 2 |
|---|---|---|
| <20 işlem | 4 | 4 |
| PnL ≤ 0 | 3 | **1** |
| **Calmar < buy&hold** | 8 | **10** |

Arama para kazanmayı öğreniyor (zarar edenler 3→1) ama tıkanma tamamen Calmar'a
kaydı: 15 adayın 10'u KÂR ETTİĞİ hâlde buy&hold'un risk-ayarlı getirisini
geçemedi. QQQC'nin 2003-2026 penceresinde buy&hold güçlü ve `risk_adjusted` mod
bilinçli olarak "mutlak getiride kaybetsen de Calmar'da geç" diyor — 30 adayın
hiçbiri geçemedi.

Bunun bu sayfa için doğrudan sonucu şu: alfa kapısını geçen aday olmadığı için
**robustness zinciri hiç çalışmadı**, dolayısıyla bu turda eklenen üç göstergenin
üçü de (peer eleme gerekçeleri, `pnl_autocorr_lag1` rozeti, WFO paydası) canlıda
HÂLÂ sınanmamış durumda. Kodda test edilmiş olmaları onları ekranda doğrulanmış
yapmıyor; ilk kapıyı geçen aday bu üçünün ilk gerçek sınavı olacak.

Aynı koşudan çıkan ikinci ölçüm — `max_tokens`'ın advisory olması ve maliyet
tahminini tek yönlü bozması — [[llm_maliyet_kaldiraclari]] sayfasına yazıldı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[multi_symbol_generalization]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
