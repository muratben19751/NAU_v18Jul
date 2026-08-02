---
title: nautilus_web_app Güvenlik & Dayanıklılık Düzeltmeleri (2026-07-25)
type: synthesis
summary: Beş paralel incelemenin (kod/mimari/performans/test/E2E) kritik ve yüksek bulgularının kapatılması — sandbox kaçışı, registry/katalog veri kaybı, index path traversal, Studio'nun web sürecini dondurması, deploy geçidi taslak istismarı — ve bunların yerleşen yeni sözleşmeleri.
key_concepts:
  - crash_only_design
  - single_threaded_core
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/nau_mimari_denetimi.md
  - wiki/synthesis/nau_performans_denetimi.md
  - wiki/synthesis/strategy_studio.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-02
---

# Güvenlik & Dayanıklılık Düzeltmeleri — 2026-07-25

[[nau_mimari_denetimi]] ve [[nau_performans_denetimi]] *neyin bozuk olduğunu* anlatır;
bu sayfa o denetimlerin **kritik/yüksek bulgularının nasıl kapatıldığını** ve geriye
kalan **yeni sözleşmeleri** anlatır. Test tabanı 518 geçen/12 hatalı durumdan
**545 geçen / 0 hatalı**'ya çıktı (12 hata giderildi, 15 yeni regresyon testi eklendi;
son 6'sı aşağıdaki ikinci tur incelemesinden).

## 1. Sandbox kaçışı — AI kodu paylaşılan modülleri değiştiremez

**Neydi:** `codegate` AST kontrolü attribute'un *adına* bakıyor, *bağlamına* bakmıyordu.
Namespace'e canlı `math`/`statistics`/`ind` (gerçek `indicators` modülü) enjekte edildiği
için üretilen kod `ind.calc_rsi = ...` yazabiliyordu. Bu kod önizleme ve smoke yolunda
**web sunucu sürecinin içinde** exec edilir; bir kez çalıştıktan sonra süreç ömrü boyunca
tüm stratejiler zehirlenmiş göstergeyle koşardı. `sandbox.py`'nin süreç sınırı bu yolu
korumuyordu (önizleme yolu o sınırı hiç geçmez).

**Yeni sözleşme — iki katman:**
1. `codegate`: `ast.Attribute` düğümünde `ctx` `Load` değilse reddedilir (atama/silme yasak).
2. `codegate.safe_module_proxy`: enjeksiyon noktalarının hepsi (`agent.py`,
   `composer._load_module_from_path`, `web/routes/strategy.py::_preview_signals`) artık
   canlı modül yerine **salt-okunur sığ proxy** alır; proxy yalnız `_ALLOWED_ATTRS`
   içindeki adları iletir, yazma denemesi `AttributeError` verir.

Statik kontrol ile çalışma-zamanı görünümü aynı beyaz listeden beslendiği için ayrışamaz.
Diskteki 233 blok yeniden doğrulandı: sıfır regresyon.

## 2. Özel blok registry'si ve katalog — tek okuma hatası artık veri silmiyor

**Neydi:** `custom_block_store._read_registry` **her** istisnada `registry.json`'ı
`.bak`'a taşıyordu. Windows'ta eşzamanlı bir `save_custom` sırasında okuma
`PermissionError` alır (sharing violation) → tüm blok kayıtları silinir. Ardından
`composer.load_catalog` bu durumu `custom_names = set()` diye yorumlar, özel blok
kullanan tüm stratejileri geçersiz sayar ve **budanmış kataloğu diske yazardı**:
anlık bir dosya kilidi kalıcı strateji kaybına dönüşürdü.

**Yeni sözleşme:**
- I/O hatası ≠ bozulma. I/O hatası kısa aralıklarla yeniden denenir, hâlâ başarısızsa
  `RegistryUnavailable` **fırlatılır** ("boş registry" diye raporlanmaz). Yalnızca
  gerçek *parse* hatası `.bak` karantinasına yol açar.
- `list_custom`/`get_custom` artık yazarlarla aynı `_STORE_LOCK` altında okur.
- `load_catalog`: registry okunamıyorsa `custom_names = None` ("bilinmiyor") → **hiçbir
  budama yapılmaz, katalog diske yeniden yazılmaz**.
- Registry yazımı UTF-8'e sabitlendi (Türkçe prompt/meta Windows locale'inde patlıyordu).

Regresyon testi: `tests/test_block_registry_resilience.py`.

## 3. Index/ticker path traversal

`_ticker_to_filename` yalnız `:` ve `/` değiştiriyordu; `\` ve `..` olduğu gibi kalıyordu.
`ticker` doğrudan `/backtest/run` ve `/backtest/sweep` form gövdesinden geldiği için
`..\..\Users\...\evil` cache dizini dışına yazabiliyordu. Artık `_bybit_cache_path` ile
**aynı sözleşme**: ASCII alnum + `_-.` dışındaki her karakter `_` olur, `..` dizileri
daraltılır, geriye kullanılabilir bir ad kalmazsa dosyaya dokunulmadan `ValueError`.
`I:SPX → I_SPX` ve `BRK.B → BRK.B` adlandırması korunur (mevcut cache geçersizleşmez).

## 4. Index veri hattı saf Python'a taşındı (Windows'ta çalışmıyordu)

`discover_index_tickers` ve `_stream_ticker_rows` `bash -c "gunzip | awk"` çağırıyordu;
uygulamanın birincil platformu olan Windows'ta `bash` bozuk WSL launcher'ına çözülüp
`execvpe(/bin/bash) failed` veriyordu — index hattı **hiç çalışmıyordu**. Artık
`gzip` + `csv` ile satır-satır akıtılıyor (2 GB dosyalarda bellek düz kalır).
Ayrıca kaynak dosya yoksa `RuntimeError` değil `FileNotFoundError` fırlatılır; route
bunu zaten 404'e eşliyordu (500 yerine doğru kod).

## 5. Strategy Studio motoru artık web sürecini dondurmuyor

**Neydi:** `STUDIO_BACKTEST=nautilus` iken `_execute_run`/`_execute_opt`,
`run_composed_backtest`'i **doğrudan** çağırıyordu. Nautilus backtest'i tüm koşu boyunca
GIL'i tutar; uygulamanın geri kalanı (agent, `/backtest`, robustness, loop) bu yüzden
`force_subprocess=True` kullanır — Studio bu düzeltmeyi hiç almamıştı. Bir "Optimize"
tıklaması (200'e kadar motor koşusu) tüm HTMX pollingini dakikalarca dondurabilirdi.

**Yeni sözleşme:** `NautilusBacktestAdapter._run_one` artık
`sandbox.run_backtest_guarded(..., force_subprocess=True)` üzerinden geçer. Child'ın
enstrümanı yeniden kurabilmesi için adapter bir `recipe` üretir
(`_recipe(symbol, timeframe)` → `{symbol, interval, category, initial_capital}`).
Ölçüm: koşu başına ~6-9 s (spawn + nautilus import dahil), 1 headline + 3 fold ≈ 6 s.

Ek olarak `NautilusBacktestAdapter.load_bars` eklendi: `(symbol, timeframe)` başına
`BARS_TTL_S = 300` saniyelik memoization. Bir sweep aynı barları aday×fold sayısı kadar
yeniden decode ediyordu (ve varsayılan loader'da Bybit'e gidip cache'i yeniden yazıyordu).
Adapter modül düzeyinde singleton olduğu için TTL zorunlu — sweep boyunca tutarlı,
seans boyunca bayat değil.

## 6. Deploy geçidi: ölçülen tanım = konuşlandırılan tanım

**Neydi:** `_baseline_metrics` `latest_run`'ı, yani *en yeni* satırı döndürüyordu; deploy
ise **kaydedilmiş** sürümü derliyor. Kaydedilmiş v3 (DSR 0.40) → taslağı 0.92'ye ayarla →
Backtest (kaydetmeden) → Deploy: geçit taslağın sayısıyla geçer, artifact başka bir
tanımdan üretilirdi.

**Yeni sözleşme:**
- `studio_runs` tablosuna additive `defn_hash` sütunu eklendi; `create_run` her koşuya
  ölçtüğü tanımın **içerik hash'ini** yazar (`store.definition_hash`).
- Hash `version`/`parent_version`/`created_at`/`origin` alanlarını **dışlar**: taslağı
  kaydetmek yalnız sürüm defterini değiştirir, kuralları değil — böylece normal
  *düzenle → backtest → kaydet → deploy* akışı tek tıkla çalışmaya devam eder.
- Geçit ve deploy modalı artık `_gate_baseline(defn)` → `store.latest_run_for_hash` okur;
  modal, geçidin yargılayacağı sayının aynısını gösterir.

**Migrasyon notu:** bu değişiklikten önce kaydedilmiş koşuların `defn_hash`'i `NULL`'dur.
Geçit *açık* olan bir deploy, o strateji için yeni bir backtest koşusu isteyecektir —
hata güvenli yönde (sıkı) ve tek bir tıkla giderilir.

Regresyon testi: `tests/studio/test_deploy_gate_draft_bypass.py` (istismar bloklanır,
normal akış geçer).

## 7. Orta öncelikli düzeltmeler

| Bulgu | Düzeltme |
|---|---|
| `robustness` eviction sonrası `_PROGRESS[run_id]` yazımı `KeyError` → daemon thread ölür, child öksüz kalır | `_set_progress(run_id, **fields)` — girdi yoksa sessizce atlar (izleyen kimse yok demektir) |
| Optimizasyon grid'i kullanıcının `max`'ını aşıyor (`min=10, step=6, max=20 → 22`) | `n_values` artık `floor(span/step + 1e-9) + 1`; epsilon ikili kayan nokta hatasını kapsar |
| Deploy `kill_switch` sayısal değilse 500 (HTMX gövdeyi göstermez → buton "çalışmıyor") | `off/none/disabled/""` → `None`, virgüllü ondalık kabul, geçersiz değer → 422 mesajı |
| `store.save()` read-then-write versiyon yarışı → PRIMARY KEY ihlali → 500 | `BEGIN IMMEDIATE` ile okuma+yazma tek işlem; 12 eşzamanlı kayıt: 0 hata, benzersiz sürümler |
| SQLite bağlantıları hiç kapanmıyor, 5 sn busy_timeout | `_Conn.__exit__` commit + close; `busy_timeout = 30 s` |
| `run_backtest_guarded` hızlı yolu `initial_capital`/`commission_bps_override` düşürüyor | Hızlı yol da child ile aynı iki `recipe` anahtarını geçirir (yollar artık gerçekten eşdeğer) |
| `BacktestPool` timeout'ta süreçleri terminate ediyor ama join etmiyor → zombie/handle sızıntısı | `terminate → join(5) → kill → join(5)` |
| AI döngüsü eşzamanlı kullanıcı düzenlemesini eziyor | İterasyon başında `base_hash` alınır; yazmadan önce çalışma kopyası değiştiyse öneri reddedilir ve döngü açıklamayla durur |

## 8. Test tabanı

- **7× `test_describe_backtest`** eskimişti: chain tetiği `hx-post="/backtest/run"`
  attribute'undan JS tabanlı `data-bt-chain-url` / `data-bt-chain-vals` div'ine taşınmış,
  testler eski işaretçiyi arıyordu (yani bu davranışı hiç korumuyorlardı). Yeni sözleşmeye
  güncellendi (`_chain_url` / `_chain_vals` yardımcıları).
- **3× `test_perf_equivalence` golden**: `pnl` ve `n_trades` birebir eşleşiyordu, yalnız
  trade-listesi SHA'sı kaymıştı. Hash artık **yalnız ekonomik alanları** kapsar
  (side/zaman/fiyat/pnl/dur_min/exit_kind); `entry_detail`/`exit_detail`/`*_reason`
  insan-okur açıklamalardır ve UI her değiştiğinde golden'ı boş yere kırardı — sürekli
  "yenilenen" bir golden gerçek regresyonu kaçırır. Ekonomik değerler değişmediği
  doğrulandıktan sonra hash'ler yeniden üretildi.
- **1× `test_data_page`**: `discover_index_tickers` artık `FileNotFoundError` fırlatıyor → 404.
- **1× `test_index_stream_empty`**: saf-Python index hattı ile Windows'ta geçiyor.

## 9. İkinci tur inceleme (2026-07-26)

Düzeltme turunun ardından yapılan bağımsız incelemenin bulguları:

### 9.1 `promote_draft` atomik değildi — ORTA, düzeltildi

Draft'ı kaydetmek üç ayrı işlemdi: `load_draft` → `save` → `delete_draft`. Aradaki
boşluğa düşen bir `save_draft` (UI otomatik kaydı ya da AI döngüsünün kabul edilen
öneriyi geri yazması) **sondaki delete tarafından siliniyordu** — kullanıcının en yeni
düzenlemesi hiçbir yerde hata üretmeden kayboluyordu.

Artık tamamı tek `BEGIN IMMEDIATE` işlemi: yazarlar tüm dizi boyunca dışarıda tutulur,
eşzamanlı bir draft yazımı commit'ten **sonra** iner ve sıradaki draft olarak yaşar.
Silme ayrıca okunan json'a koşullandırıldı (`WHERE strategy_id=? AND json=?`) — işlem
içinde gereksiz, ama kuralı kodun içinde söylüyor: *okumadığın draft'ı silme*.
`save()` ile paylaşılan gövde `_insert_version(con, defn)` yardımcısına çıkarıldı.

**Test notu (yöntem):** ilk yazılan thread-yarışı testi eski (hatalı) kodda da
geçiyordu — yani ayırt edici değildi; boşluk mikrosaniyeler mertebesinde olduğu için
zamanlamaya dayalı bir test bu iki uygulamayı güvenilir biçimde ayıramıyor. Test
bunun yerine düzeltmenin özünü doğrudan ölçüyor: `promote_draft` **tek bağlantı**
açmalı. Eski kodda 3 açıyor ve test `3 != 1` ile düşüyor (doğrulandı).
Bkz. `tests/studio/test_promote_draft_atomicity.py`.

### 9.2 Sweep'ler bayat adaptörle koşabiliyordu — ORTA/DÜŞÜK, düzeltildi

`OPTIMIZER = WalkForwardOptimizer(adapter=TRIAL_ADAPTER)` adaptörü **import anında**
yakalıyordu. `TRIAL_ADAPTER` sonradan değiştirilirse (test, ya da ileride bir config
yeniden yüklemesi) optimizer eski nesneyi tutmaya devam ediyordu: tek bir anahtar için
iki doğruluk kaynağı, üstelik **koşan** olan bayat olanı. `_optimizer()` artık güncel
adaptörü çözüyor (dokunulmamış yolda aynı nesne, ayrışma varsa yeniden kuruluyor).

### 9.3 `ruff check .` tüm repoda düşüyordu — DÜŞÜK, düzeltildi

Ürün kodu temizdi; `.claude/skills/**` altındaki **vendor edilmiş** skill helper
script'leri 22 hata veriyordu. CI aynı komutu koşarsa kimsenin yazmadığı kod yüzünden
build kırılırdı. `.claude` ruff `extend-exclude` listesine eklendi (skill güncellemesi
bu dosyaları zaten üzerine yazıyor — onları düzeltmek kalıcı olmaz).

### 9.4 `EXTERNAL_CATALOGS` uyarısı — ORTAM, bug değil

Varsayılan yol başka bir makinenin diskini gösteriyor (NAU_ev masası, `E:` sürücüsü),
bu kutuda yok. Uyarı L31'de **bilerek** eklenmişti (sessizce boş panel yerine sesli
uyarı). Kod mantığı değişmedi, yalnız mesaj eyleme dönüştürüldü: hangi env
değişkeninin (`NAUTILUS_EXTERNAL_CATALOGS`) ayarlanacağını ve bunu görmezden gelmenin
ne zaman doğru olduğunu söylüyor. Bybit ve index yolları etkilenmez; harici katalog
entegrasyonu bu makinede gerçek veriyle **doğrulanmadı** (veri yok).

**Düzeltme (2026-07-28): teşhisin "veri yok" yarısı yanlış çıktı.** Veri bu kutuda
vardı — NAU_ev masası sürücü değiştirmiş, katalog `D:\NAU_ev\backend\data\catalog`
altında duruyordu (3.511 bar serisi, 591 enstrüman; QQQ/SPX/NDX dahil). Yanlış
varsayılan yalnız `/data` panelini değil, aynı `EXTERNAL_CATALOGS` listesinden
çözünen her şeyi sessizce boşaltıyordu: Lab picker'ları (`list_external_instruments`),
backtest yükleri (`_external_bar_dir`) ve enstrüman tanımları
(`external_instrument_object`) — kullanıcı "indirdiğim NASDAQ hisseleri /data'da
neden yok?" diye sorana kadar. `data.py` varsayılanı `D:` yoluna çevrildi; canlı
doğrulama: `/data` → "591 instruments", `xq=AAPL` → AAPL.NASDAQ. L31 uyarısı başka
makineler için yerinde duruyor. Ders: "ortam sorunu, bug değil" hükmü ancak verinin
GERÇEKTEN yok olduğu doğrulanınca verilebilir — diskte nerede durduğuna bakarak,
kodun nereyi okuduğuna bakarak değil.

### 9.5 Kapatılmayan: Studio route bağımlılıklarının import-anı kurulumu

`store`, `ADAPTER`/`TRIAL_ADAPTER`, `OPTIMIZER`, `LLM`, `RUNNER` modül içe aktarılırken
kuruluyor. Somut kusuru (9.2) kapatıldı; kalan **mimari** öneri (DI/factory) bilinçli
olarak yapılmadı — `store.` çağrısı ~40 yerde geçiyor ve bu, davranış-koruyan bir
refactor'dan çok bir tasarım değişikliği. Bugünkü etkileri: (a) `studio.db` import
anında açılır/oluşturulur, (b) motor anahtarları (`STUDIO_BACKTEST`, `STUDIO_RUNNER`)
import'tan sonra değiştirilemez, (c) testler modül global'lerini monkeypatch'ler.
(c) bugün çalışıyor; (b) tek-worker kısıtıyla birlikte [[nau_mimari_denetimi]]'ndeki
H9 (global tek-instance `AppState`) ile aynı ailedendir. Yapılacaksa doğru sıra:
`deps.py` + FastAPI `Depends` override'ları.

## 10. Maliyet ekseni: beyaz listenin üçüncü sorusu (2026-08-02)

§1 beyaz listenin **ad** ve **bağlam** eksenlerini kapattı. Üçüncü eksen —
*bu isimle ne kadar pahalıya iş yapılabilir* — bugüne kadar açıktı: düzeltmesi
silinen macOS dalında (`e980c71`) kalmış, `main`'e hiç girmemişti. Ölçümle
doğrulandı ve kapatıldı.

**Neden aşağıdaki hiçbir mevcut koruma tutmuyor:**

- **Döngü bütçesi** yalnız `While`/`For`/comprehension düğümlerine guard enjekte
  eder. Bu ifadelerde döngü düğümü yok → enjekte edilecek yer yok.
- **Smoke'taki `t.join(timeout=2.0)`** GIL tutan bir thread'i kesemez. Tek büyük
  tamsayı işlemi bytecode sınırı sunmaz; ana thread guard'ı çalıştırmak için
  GIL'i geri alamaz bile. Ölçüldü: 2 s vaat eden yol **8.8 s** sürdü, hata yok.
- **Sabit ifade daha da erken kaçar:** tümüyle literal olan bir ifade `compile()`
  sırasında katlanır — thread daha kurulmadan, **çağıran thread'de** (10.0 s).
  Bu, diskteki blokları açılışta derleyen `composer._load_module_from_path`
  yolunu da vurur; orada thread guard'ı hiç yok, yani **sunucu açılışı** asılır.

**İki kurallı düzeltme (`codegate.py`):**

1. `MAX_POW_EXPONENT = 64` — `**` üssü küçük bir sayısal **literal** olmak
   zorunda. Hesaplanmış üs de reddedilir (doğrulama anında sınırsız, değeri
   saldırganın seçimi). `e980c71`'den alındı.
2. `MAX_LITERAL_MAGNITUDE = 1_000_000` — literal-aritmetiği doğrulama anında
   katlanır, herhangi bir **ara değer** tavanı aşarsa reddedilir.

İkinci kural neden gerekli: birincisi tek başına yetmiyor. `((10**64)**64)**64`
ifadesinde **her** `**` literal 64 üssü taşır ve §1'den geçer, ama değer
10\*\*262144'tür — her ek seviye 64× daha pahalı (ölçüldü: dört seviye **14,5 s**,
55 Mbit). Aynı boşluk `list(range(10**9))` / `[0] * 10**9` gibi döngü düğümü
olmayan konteyner bombalarını da kapsıyordu.

Aşağıdan-yukarı reddetmenin ikinci faydası: **doğrulamanın kendisi ucuz kalır.**
Hiçbir operand tavanı, hiçbir üs 64'ü aşamadığı için hesaplanan en büyük çarpım
1e6\*\*64 — birkaç yüz basamak. Kontrol, durdurmak için var olduğu bombaya
dönüşemez.

**Kalibrasyon ve regresyon:** tavan tahminle değil veriyle seçildi — diskteki 268
bloğun en büyük sayısal literali **9999**, tek `**` kullanımları `** 2` ve
`** 0.5`. 268/268 blok hâlâ geçiyor (0,14 s). Gerçek boru hattında dört bomba da
**~0,1 ms**'de reddediliyor, meşru blok uçtan uca 38 ms'de geçiyor.
Testler: `tests/test_guards_and_indicators.py::TestCostGate`.

`e980c71`'in yanındaki iki 500 hatası da aynı turda `main`'e alındı: doğrulanmamış
`block` alanının `model_copy` ile diske yazılıp sayfayı okunamaz hâle getirmesi
(dismiss düğmesi dahil kurtarma yolu yoktu) ve alansız `PATCH`'in bilinmeyen blokta
500 vermesi → ikisi de 422.

## İlgili sayfalar

[[nau_mimari_denetimi]] · [[nau_performans_denetimi]] · [[strategy_studio]] ·
[[webapp_module_map]] · [[crash_only_design]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
