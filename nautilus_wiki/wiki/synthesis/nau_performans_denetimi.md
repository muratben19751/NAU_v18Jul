---
title: nautilus_web_app Performans Denetimi (2026-07)
type: synthesis
summary: nautilus_web_app'in runtime-performans denetimi — 50 ham bulgu, 31 doğrulanmış darboğaz (2026-07); en yüksek ROI LLM prompt-caching, en sert kısıt NAU_WINDOW=260 paritesi. 2026-08-04 ikinci turu sayfa/disk ölçtü: /sessions 114 s → 19 s, robustness_result 3,5 MB/olay indirgendi (11,8 GB birikmişti).
key_concepts:
  - single_threaded_core
  - crash_only_design
  - backtesting_guide
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/auto_arama_ekonomisi.md
  - wiki/synthesis/backtesting_guide.md
last_updated: 2026-08-04
---

# nautilus_web_app Performans Denetimi — 2026-07-21

`nautilus_web_app` repo kodunun (~31k satır) **runtime-performans** denetimi — [[webapp_module_map]]'in
performans-odaklı tamamlayıcısıdır (o *ne nereye bağlanır*'ı, bu *ne yavaş ve neden*'i verir).
12 modül paralel performans-lensiyle tarandı, ardından her bulgu **çekişmeli doğrulayıcı**
("kod karşısında bunu çürüt") ile denetlendi: **50 ham bulgu → 31 doğrulandı** (64 ajan).
Yalnız repo kodu incelendi; NautilusTrader kütüphanesine dokunulmadı ([[single_threaded_core]]
GIL kısıtı sandbox izolasyonunun *neden* var olduğunu açıklar — H3 bunu doğrular).

## İki baskın tema

1. **Sıcak-yol CPU.** Robustness taraması (WFO × GA × k-fold, `backtest_robustness.py` +
   `parallel_exec.py`) yüzlerce tam backtest çalıştırır. Her backtest'te NAU recursive
   indikatörleri (`composer.py` `adx_threshold`/`stoch_rsi_cross`/`wave_trend_cross`
   blokları) **her bar 260-pencereyi saf-Python'la sıfırdan** hesaplar; her guarded çağrıda
   **~1 sn'lik subprocess cold-boot** (nautilus/pandas/numpy re-import) ödenir.
2. **Cache eksikliği + sınırsız birikim.** `agent.py` LLM çağrıları ~16.6K token'lık sabit
   sistem prefix'ini **prompt-caching olmadan her seferinde** yeniden gönderir; `state.py`
   `AppState.iterations` listesi gece-boyu loop'ta **sınırsız büyür** (RAM + her 2 sn poll'da
   O(n) render).

## En yüksek ROI — 4 HIGH bulgu

| # | Bulgu | Modül | Efor |
|---|-------|-------|------|
| **H1** | LLM prompt-caching (`cache_control`) hiç yok (grep:0); 16.6K token her çağrıda yeniden işleniyor | `agent.py` `_create_message`, `web/routes/agent_backtest.py` | **S** ⭐ |
| **H2** | `iterations` listesi sınırsız (RAM + O(n) poll her 2 sn) | `state.py` | M |
| **H3** | Her guarded çağrıda subprocess spawn + ağır import (~1 sn) | `sandbox.py` | L |
| **H4** | NAU indikatörleri her bar 260-pencereyi saf-Python sıfırdan (~83–160 µs/çağrı) | `composer.py` → `block_library_nau.py` + `indicators.py` | L — **kısmen kapandı 2026-08-11**, aşağıya bak |

**En pratik ilk iş — H1 (prompt-caching):** `agent.py`'de `system`'i content-block listesine
çevirip son büyük sabit bloğu `{"type":"text","text":…,"cache_control":{"type":"ephemeral"}}`
ile işaretle; `usage.cache_read_input_tokens > 0` ile doğrula. Backtest/spec/JSON semantiği
değişmez; onlarca-çağrılı continuous-loop koşularda belirgin maliyet/latency düşüşü.

## En sert kısıt — NAU_WINDOW=260 parite

En büyük CPU kazancı incremental indikatör state'inde (H4 + orta/düşük bulgular: ADX/StochRSI/
WaveTrend'i her bar full-recompute yerine running-state ile güncellemek). **Ama engel mimaridir:**
[[webapp_module_map]]'te belgelenen `composer.py` **NAU_WINDOW=260 sabit pencere** semantiği
korunmalı — naif incremental state farklı değer üretir, strateji sinyalleri kayar, NAU paritesi
bozulur. Kural:

> Bu göstergelerde herhangi bir incremental/vektörize optimizasyon **kayan-260 pencereyi koruyan**
> bir sürüm olmalı ve `indicators.calc_*` referansıyla **bit-parite testi** (tolerans <1e-9)
> geçmeli. `tests/test_regression_anchors*.py` yeşil kalmalı. Geçmezse uygulanmaz.

Bu, "Sağlamlaştırma & regresyon" bölümündeki NAU-uyum denetiminin ([[webapp_module_map]])
performans-tarafı devamıdır: **hız için strateji doğruluğunu feda etme.**

## Doğrulamanın kattığı değer — reddedilen "iyileştirmeler"

- **Elle tek-geçiş min/max** (`indicators.calc_stoch_rsi`): CPython'da `min()`/`max()`
  C-implemented; elle döngü **~%15 DAHA YAVAŞ**. Yalnız monotonik-deque gerçek kazanç.
- **Equity N-bar örnekleme** (`composer.py` `on_bar`): `_mtm_equity` telemetri değil **birincil
  metrik kaynağı**; örneklemek max_dd ve Sharpe'ı bozar.
- **"O(n²) kilitlenir"** (`indicators.calc_nadaraya_watson`): n her yolda ≤~1040 sınırlı —
  "kilitlenir" premisi yanlış. Yine de DeepR'ın 2026-08-08 turu aynı fonksiyonu
  algoritmik gereksizlik olarak tekrar işaretledi (kilitlenme değil, boşa iş);
  Gauss çekirdeği 6-bandwidth ötesinde ~0 olduğundan iç döngü artık her satırda
  tam `n` yerine `±6·bandwidth` penceresiyle sınırlı — çıktı bit-bit aynı
  (kesilen terimler 1e-8 altı), maliyet O(n²) → O(n·bandwidth).
- **Thread-executor'a geçiş** (`sandbox.py`): killability'i kaybettirir ([[single_threaded_core]]
  GIL — thread güvenle öldürülemez); bilinçli bir takas.

## Güvenli hızlı kazanımlar (S efor, parite korur)

- `indicators.sma()` running-sum (bit-identical, tol 1e-9) + monotonik-deque min/max.
  **Running-sum kısmı uygulandı (2026-08-08, DeepR ikinci tur)** — O(n) artık;
  kayan min/max **2026-08-11'de kapandı** ama deque ile DEĞİL (aşağıdaki H4
  bölümüne bak: deque ölçüldü, tembel-yeniden-tarama daha hızlı çıktı).
- `composer.py` `on_bar` FFI çağrılarını (`is_net_long`/`is_net_short`) sinyal-guard altına al.
- `backtest.py` `_extract_trades` `itertuples`; `_metrics` tek `np.asarray`.
- `wfo_optimizer.py` fold dilimlerini kandidat başına değil bir kez ön-hesapla.
- `backtest_robustness.py` Monte-Carlo win-rate vektörizasyonu (`(shuffled>0).sum(axis=1)`).
- `custom_block_store._read_registry()` **uygulandı (2026-08-08, DeepR ikinci tur)** —
  `composer._read_catalog_raw`'daki (mtime, size) önbellek deseninin aynısı,
  ayrıca `path`'i de anahtara katarak (composer'ın CATALOG_FILE önbelleğinde
  olmayan bir güvenlik payı) test fixture'larının `REGISTRY_FILE`'ı farklı
  `tmp_path`'lere yönlendirmesi eski içeriği sızdırmıyor.
  **Düzeltme (2026-08-11, DeepR dördüncü tur [YÜKSEK]):** önbellek nesnesi
  çağıranlara AYNEN veriliyordu; `_registry_transaction` / `delete_custom` /
  `ensure_agent_role_metadata` onu yerinde değiştirdiği için (a) diske
  YAZMADAN sonlanan bir read-modify-write dosyanın mtime/size'ını
  değiştirmiyor, önbellek geçersizleşmiyor ve süreç ömrü boyunca diskle
  çelişen bir registry görünümü servis ediliyordu — `composer.load_catalog`
  bunu katalog budamasında girdi olarak kullandığı için sonuç sessiz strateji
  kaybı; (b) `list_custom()` sözlüğü kilit DIŞINDA gezdiğinden eşzamanlı bir
  yazıcı `dictionary changed size during iteration` üretebiliyordu. Artık
  `_read_registry` her çağırana `_copy_registry()` ile DERİN kopya veriyor.
  Maliyet ölçüldü: 357 kayıtlı gerçek registry'de `copy.deepcopy` 2.5 ms,
  elle özyineleme 0.6 ms, diskten yeniden parse 0.95 ms + I/O — yani kopya
  memoize'un kazancını yemiyor. `tests/test_custom_block_store_registry_isolation.py`
  (8 test) sızıntının her iki yüzünü de çiviliyor; kopya devre dışı
  bırakıldığında 8 testin 7'si kırmızıya dönüyor (kalan biri memoize'un hâlâ
  çalıştığını, yani düzeltmenin önbelleği iptal etmediğini doğruluyor).
- `token_ledger.summary()` **uygulandı (2026-08-08, DeepR ikinci tur)** — önceki
  ikisinden farklı desen: JSONL sadece append edildiği için tam mtime/size
  önbelleği yetmez (dosya her yazımda değişir); `_parsed_records()` yerine
  **artımlı** okuyor — path başına `(tüketilen_byte, parse_edilmiş_kayıtlar)`
  tutup son çağrıdan beri eklenen byte'ları okuyor. Son `\n`'den sonraki
  tamamlanmamış satır (eşzamanlı bir `record()` yazımı ortasında) offset'e
  dahil edilmiyor — bir sonraki çağrıya bırakılıyor, yoksa "torn line" kalıcı
  olarak atlanırdı. `/tokens/badge` aynı isteği için `summary()`'i 2 kez
  (session + all-time) çağırıyor, ikisi de artık aynı önbelleği paylaşıyor.
- `data._bybit_rows()` **uygulandı (2026-08-08, DeepR ikinci tur)** — önceki
  üçünden farklı desen: 72 hücre (3 sembol × 3 kategori × 8 interval) çoklu
  dosyadan geldiğinden tek bir mtime/size anahtarı yok; basit TTL önbelleği
  (`_BYBIT_ROWS_TTL_S=30s`, `strategy_studio._SYMBOLS_TTL_S`/`BARS_TTL_S` ile
  aynı desen). `refresh_row("bybit", ...)` `_invalidate_bybit_rows_cache()`
  ile önbelleği açıkça geçersiz kılıyor — yoksa force-refresh sonrası
  kullanıcıya bayat (refresh-öncesi) satır dönerdi.

Dikkat gerektiren (invalidasyon/off-by-one): `data.py`/`web/routes/backtest.py` mtime-keyed
cache'leri custom-block durumu + dir-mtime anahtarına katmalı; WFO pencere label-slice
inclusive-end off-by-one'a dikkat.

## İkinci tur — 2026-08-04 (ölçülmüş, düzeltildi)

İlk denetim CPU sıcak-yoluna bakmıştı; bu tur **sayfa gecikmesi + disk** ölçtü
(TestClient, aynı süreç, n=5). İki bulgu ve ikisi de kapatıldı.

| sayfa | önce (soğuk) | sonra (soğuk) | sıcak |
|---|---|---|---|
| `/sessions` | **114.298 ms** | **19.000 ms** | 17 ms |
| `/studio` | 459 ms | — | 35 ms (1,73 MB yanıt) |
| `/data` | 1.094 ms | — | 141 ms |

### P1 — `robustness_result` olay başına 3,5 MB yazıyordu

76 oturum dosyası **11,8 GB** (en büyüğü 4,7 GB / 304.933 satır). Tek satırın
dağılımı: `wfo_windows[]` 2,43 MB (88 pencere × train/test/naive, her biri
equity eğrisiyle), `mc.curves_sample[]` 0,44 MB (50 eğri),
`split.oos_metrics.equity_curve_mtm[]` 0,35 MB (**8.605 ham nokta**),
`split.in_sample_metrics…` 0,21 MB.

Kök neden **yüzey başına uygulanmış bir düzeltme**: equity eğrilerinin ~40
noktaya indirgenmesi `backtest_result` yolunda vardı, `robustness_result`
yolunda yoktu. Çare `_thin_curves` (`web/routes/agent_backtest.py`): yalnız
**tamamı sayı olan ve 40'tan uzun** dizileri indirger; sözlükleri, dizgeleri ve
dict listelerini (işlem kayıtları) korur, girdiyi **değiştirmez** (aynı `rob`
sözlüğü karar ve ekran yollarında da kullanılıyor). Test verisinde
0,46 MB → 44 KB; metrikler, 88 pencere ve 50 MC eğrisinin sayısı aynen kalır.

### P2 — `_session_summary` her satırı JSON parse ediyordu

Fonksiyonun docstring'i "read only key events" diyordu ama gövde her satıra
`json.loads` uyguluyordu — 3,5 MB'lık robustness satırlarını yalnız **adını**
saymak için. Çare (`web/routes/sessions.py`): olay adı ve `ts` satırın ilk 400
baytından regex ile okunur (yazıcı ikisini ilk iki anahtar olarak koyar); tam
parse yalnız gövdesi gereken dört olay için yapılır
(`session_start`, `session_end`, `token_snapshot`, `winner`). Beklenmeyen biçimde
eski yola düşülür — doğruluk hızdan önce.

Doğrulama fonksiyonun kendisiyle A/B yapıldı (`_EVENT_RE` hiç eşleşmeyecek hale
getirilip eski yol zorlandı): **tüm alanlar birebir aynı**, dosya başına
4,4–5,9× hız.

> Kalan 19 saniye **parse değil I/O**: 11,8 GB'ın okunması. P1 yeni büyümeyi
> durdurur ama mevcut dosyaları küçültmez; eski logların arşivlenmesi bunu
> ~1 sn'ye indirir. `_SUMMARY_CACHE` (mtime+size) sıcak yolu zaten kurtarıyor,
> ama süreç içi olduğu için her restart soğuk maliyeti geri getirir.

Algoritmik tarafın bulguları ayrı sayfada: [[auto_arama_ekonomisi]].

## Dördüncü tur — 2026-08-11: H4 pariteyi BOZMADAN hızlandırıldı

H4 iki tur boyunca "parite kısıtı yüzünden ertelendi" olarak duruyordu, çünkü
akla gelen çözüm (artımlı/incremental gösterge state'i) NAU_WINDOW=260 sabit
penceresini kırar. Bu tur farklı bir yol seçildi: **pencere aynen kalsın, aynı
işi daha az yorumlayıcı adımıyla yap.** Hiçbir matematik ve hiçbir işlem SIRASI
değişmedi; ölçülen kazanç tamamen ara liste / ara nesne / gereksiz tarama
eliminasyonundan geliyor.

Ne değişti (`indicators.py`):

- **`calc_adx`** — dört ara liste (`tr`/`plus_dm`/`minus_dm` ~n, `dx_values`
  ~n−period ≈ toplam 1000 float) ve bar başına **246 tek-kullanımlık
  `{"pDI","mDI"}` sözlüğü** üreten `push_dx()` closure'ı kaldırıldı; tek geçiş.
- **`calc_stoch_rsi`** — 233 adımın her birinde 14'lük dilim kopyalayıp
  `min`+`max` taraması yapılıyordu. Artık ekstremum taşınıyor; yalnız
  ekstremumu TUTAN eleman pencereden düşünce yeniden taranıyor (rastgele
  veride ~1/14 adım).
- **`calc_wave_trend`** — yedi geçişlik boru hattında (hlc3→ema→diff→ema→ci→
  ema→sma) iki komşu çift kaynaştırıldı (`esa`+`diff`, `d`+`ci`).
- **`_tail3`** — üç seri zaten aynı boydaysa (`_nau_win` yolunda her zaman öyle)
  üç gereksiz kopya üretiyordu; artık orijinali döndürüyor.
- **`sma`/`ema`/`calc_rsi_series`** — `append` ve `1−k` döngü dışına alındı.

Ne değişti (`block_library_nau.py`): `_nau_cached` — **bar başına tek hesap.**
Aynı mumda aynı (gösterge, parametre) penceresi ikinci kez isteniyorsa (aynı
blok hem entry hem exit rolündeyse, ya da ateşleyen blok için
`composer._build_reason` snapshot'ı da hesaplıyorsa) `calc_*` tekrar
çalışmıyor. Bar kimliği `(len(closes), closes[-1])`: `on_bar` her mumda tampona
ekleme/kırpma yaptığı için iki ARDIŞIK mumun anahtarı asla eşleşemez — bir
önceki mumun değeri okunamaz, yani bu bir artımlı state DEĞİL.

Ölçüm (bu kutu, CPython 3.12, 260 barlık pencere, min-of-3):

| | önce | sonra | |
|---|---|---|---|
| `calc_adx` | 117 µs | **80 µs** | 1,46× |
| `calc_stoch_rsi` | 131 µs | **83 µs** | 1,58× |
| `calc_wave_trend` | 70 µs | **59 µs** | 1,20× |

Uçtan uca 40.000 barlık `on_bar` döngüsü (DeepR'ın ölçtüğü senaryonun aynısı):

| senaryo | önce | sonra | |
|---|---|---|---|
| `adx_threshold` tek blok | 4,88 s | **3,39 s** | 1,44× |
| `stoch_rsi_cross` tek blok | 5,37 s | **3,44 s** | 1,56× |
| `wave_trend_cross` tek blok | 3,83 s | **2,52 s** | 1,52× |
| `adx` entry + `adx` exit | 9,68 s | **3,29 s** | **2,94×** |

Parite kanıtı: `tests/test_indicators_hotpath_parity.py` (34 test). Dosya
optimizasyon ÖNCESİ sürümlerin (git `43f3d83:indicators.py`) **birebir
kopyasını** `_ref_*` olarak taşıyor ve rastgele üretilmiş serilerde **641 kayan
260'lık pencerenin her biri için** (× 4 tohum) eski==yeni olduğunu `==` ile —
tolerans YOK — doğruluyor; ayrıca period/param süpürmeleri, düz ve monotonik
seriler (min==max, `avg_loss==0` kısa devreleri) ve hizasız seri boyları.

> **Python 3.12 tuzağı (parite için kritik):** `sum()` float'larda artık
> Neumaier toplaması yapıyor. Wilder tohumlarını (`sum(tr[:period])`,
> `sum(dx_values[:period])`, `ema`'nın `sum(values[:period])`) elle `+=` ile
> biriktirmek son ULP'de sapar ve eşik/kesişim bloklarında farklı sinyal
> üretebilirdi. Yeni sürüm bu yüzden tohumları hâlâ küçük listeler üzerinde
> `sum()` ile hesaplıyor.

Reddedilen (ölçülüp elenen) alternatifler bu turda:

- **Monotonik deque** kayan min/max: `period=14`'te Python seviyesindeki
  while-döngüleri C seviyesindeki `min()`/`max()`'a karşı kaybediyor —
  106 µs, tembel-yeniden-taramanın 83 µs'una karşı. Sayfanın "Güvenli hızlı
  kazanımlar" bölümündeki deque önerisi bu ölçümle düzeltilmiştir.
- **numpy'a taşımak**: n=260'ta liste→dizi dönüşümü (~5 µs × 3) vektörleşmeden
  kazanılanı yiyor; ayrıca indirgemelerde (pairwise sum) parite riski var.
- **Gerçek artımlı state**: NAU_WINDOW=260 paritesini bozar — H1940'ın
  düzelttiği sıçramayı geri getirir. Yapılmadı.

### Aynı tur — `_validate_external_data` event loop'u kilitliyordu

`web/routes/agent_backtest.py`'de dış-katalog bütünlük kapısı `async def run(...)`
gövdesinden **doğrudan** çağrılıyordu: her interval için parquet'ten tam seri
okuma + pandas maskeleme + `external_data_gap_report`, multi-TF'te üç kez.
POST /agent/run boyunca TÜM sunucu (açık her sekmedeki 1 sn'lik HTMX poll'ları
dahil) donuyordu. Aynı dosyada doğru kalıp zaten vardı (`list_catalog`,
`progress` → `asyncio.to_thread`); bu çağrı kalıptan kaçmıştı.

Düzeltme: `await asyncio.to_thread(_validate_external_data, …)`. Ek olarak
tarih aralığı daraltması **yükleyicinin içine** itildi —
`load_external_bars(instrument_id, iv, start=…, end=…)` — böylece tam seri
okunup sonradan iki pandas maskesiyle kırpılmıyor. Sınır dönüşümü birebir:
eski KESİN `index < range_end + 1 gün` maskesi, yükleyicinin KAPSAYICI
`index <= end` karşılaştırmasına `end = range_end + 1 gün − 1 ns` ile
çevrildi (ns çözünürlüklü indekste aynı satır kümesi).
`tests/test_agent_run_external_validation_offload.py` (11 test) hem
"çalıştığı thread'de koşan event loop YOK" garantisini hem de sınır eşdeğerliğini
çiviliyor.

## Metodoloji

`Workflow` 4-fazlı: Recon → Scan (12 modül paralel) → Verify (her bulgu çekişmeli, REJECTED/
PLAUSIBLE/CONFIRMED) → Synthesize. Aynı çok-ajanlı çekişmeli desen [[webapp_module_map]]'in
"Sağlamlaştırma" turlarında ve mimari incelemede de kullanıldı — tekrarlanabilir bir kalite kalıbı.

> Bu bir *denetim anlık görüntüsüdür* (2026-07-21). Fix'ler uygulandıkça bulgular kapanır;
> güncel modül-eşlemesi için [[webapp_module_map]] esastır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_deepr_dorduncu_tur_2026_08_11]]
- [[nau_guvenlik_dayaniklilik_duzeltmeleri]]
- [[nau_mimari_denetimi]]
- [[nau_token_tuketim_izleme]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
