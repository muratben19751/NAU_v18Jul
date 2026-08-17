---
title: LLM maliyet kaldıraçları — AUTO ve Studio token tüketimi
type: synthesis
summary: 10 günlük defter denetimi — 21,5M token / ~$346 nominal, %99'u fable-5'te; fatura output ($103) + cache yazımı ($234, 1h TTL ×2). Ölçülen kaldıraçlar sırayla model (sonnet-5 −61%), effort (ek −52%), cache prefix sabitliği; max_tokens CLI yolunda ölü; --effort 2026-08-04'te bağlandı (seçenek var, üretim ölçümü yok); kokpit maliyeti OpenRouter koşularında 3,33× şişik (thread-local model pini), çağrıların %92'si etiketsiz.
sources:
  - sources/07_yerel_llm_hibrit_olcumu_2026_08_15.md
  - sources/09_baglam_ve_butce_olcumu_2026_08_16.md
  - https://github.com/muratben19751/NAU_v18Jul
  - https://platform.claude.com/docs/en/pricing
key_concepts:
  - strategy_studio
  - auto_mission_control
related:
  - wiki/synthesis/model_secici_ve_gorunurluk.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/nau_performans_denetimi.md
last_updated: 2026-08-17
---

# LLM maliyet kaldıraçları

`token_ledger`in ([[webapp_module_map]]) 2026-07-23 → 08-02 arası kaydı üzerinden
yapılan denetim: **3.304 çağrı · 21,5M token · ~$346 nominal**. Abonelik/OAuth
yolunda gerçek fatura yoktur — bu rakam kota tüketiminin para ölçeğidir.

## Fatura nerede

| Kalem | Token (fable-5) | $ |
|---|---|---|
| **cache yazımı** | 11,68M | **233,64** |
| output | 2,07M | 103,46 |
| cache okuma | 4,48M | 4,48 |
| gerçek input | 4,5K | 0,05 |

Girdi pratikte bedava; fatura **output + cache yazımı**. Yazım 1 saatlik TTL ile
gidiyor, yani **×2** girdi fiyatı — 5 dakikalık TTL'in ×1.25'i değil.

Modellere göre: `claude-fable-5` $341,63 · `claude-haiku-4-5` (CLI'ın kendi
iç yan-çağrıları, `cli_internal`) $3,21 · `claude-sonnet-5` $0,92.

## Kaldıraç 1 — model seçimi (ölçüldü, en büyük)

Aynı sistem+kullanıcı promptuyla, `_ClaudeCLIMessages.create`'in kullandığı
bayrakların birebir aynısıyla prob:

| model | $/MTok (in/out) | output tok | $/çağrı | fark |
|---|---|---|---|---|
| fable-5 (varsayılan) | 10/50 | 1399 | 0,0866 | — |
| **opus-5** | **5/25** | **3876** | **0,1056** | **+22%** |
| sonnet-5 | 3/15 | 2031 | 0,0335 | −61% |
| sonnet-5 `--effort low` | 3/15 | 874 | 0,0162 | −81% |
| haiku-4-5 | 1/5 | 1564 | 0,0091 | −89% |

**Opus 5 birim fiyatı Fable 5'in yarısı olmasına rağmen daha pahalı** — 2,8×
uzun yanıt yazıyor. Birim fiyata bakarak model seçmek bu iş yükünde yanıltıyor.

44 çağrılık gerçek AUTO koşusu probu doğruladı: `composed` −61% (öngörü −61%),
`idea` −69%, `custom_block` −53%.

## Kaldıraç 2 — CLI bayrakları (effort 2026-08-04'te bağlandı)

`agent.py :: _ClaudeCLIMessages.create` uzun süre yalnız `--tools ""`,
`--no-session-persistence`, `--strict-mcp-config`, `--system-prompt-file`
geçiyordu.

**`--effort` artık bağlı.** Model pini ile aynı desende, koşu-başına ve
thread-yerel: `agent.set_thread_effort()` / `current_effort()`, seviyeler
`low|medium|high|xhigh|max`, süreç geneli varsayılan `NAUTILUS_LLM_EFFORT`.
Seçim yapılmadıysa bayrak **hiç geçilmez** — ucun kendi varsayılanı korunur,
"varsayılan"ı bir seviye adıyla taklit etmek yanlış bilgi olurdu. OpenRouter
yolunda karşılığı `extra_body={"reasoning": {"effort": …}}`; sözlüğü dar
olduğu için `xhigh|max` → `high`. Kokpitte `EFFORT` seçicisi (brief) ve koşan
değeri gösteren `effort` satırı var; değer **koşu durumuna** yazılır, thread
pin'inden yeniden çözülmez (bkz. aşağıdaki üçüncü tuzak).

Kalan iki kol hâlâ kullanılmıyor:

| bayrak | etki |
|---|---|
| `--json-schema` | yapılandırılmış çıktı — "JSON only" yönergesi ve bozuk-JSON tekrarları gereksizleşir |
| `--max-budget-usd` | koşu başına sert tavan |

## Kaldıraç 3 — cache prefix sabitliği

`cache_read / cache_write` oranı sağlıkta ≫1 olmalı (yazım ×2, okuma ×0,1;
başabaş ≥3 okuma). Koşu **toplamı bu ayrışmayı gizler** — çağrı sınıfı başına
izlenmeli:

| purpose | wr | rd | oran |
|---|---|---|---|
| custom_block | 8.023 | 18.750 | **2,34** ✓ |
| composed | 16.978 | 8.839 | 0,52 ✗ |
| idea | 14.700 | 6.578 | 0,45 ✗ |

`custom_block`'un prefix'i sabitlenmiş; `composed` ve `idea` hâlâ okuduğundan çok
yazıyor. Geçmiş kayıtta 286 çağrı ~26–30K yazıp `cache_read=0` almış — etiketsiz
yazımların %78'i. Model değişimi bu oranı **etkilemiyor**: cache ve model
bağımsız kaldıraçlar.

## İki ölçüm tuzağı

**`max_tokens` CLI yolunda ölü.** `_ClaudeCLIMessages.create` imzasında:

```python
max_tokens: int = 0,  # no equivalent in the CLI; prompts already request short answers
```

Yani çağrı noktalarındaki `max_tokens=400` (propose), `900` (composed), `4000`
(custom_block) varsayılan yolda hiçbir şey yapmıyor. Defter doğruluyor: 400
isteyen çağrılarda gerçekleşen çıktı medyanı **1091**, p95 **2383**, maks
**4219**. Tehlike parametrenin çalışmaması değil, çalıştığına inanılması.

**Harcamanın %92'si etiketsiz.** `_purpose` yalnız `composed`, `custom_block`,
`idea`, `narrative`, `lab_idea` çağrılarında veriliyor; kalan altı çağrı noktası
(`propose_strategy`, breakdown, refine, chat, block_edit, blocks_edit) etiketsiz
ve **$316,55**'lik kalemi oluşturuyor. Ölçülemeyen kalem optimize edilemiyor.

**Üçüncü tuzak (2026-08-03): kokpitin maliyet göstergesi OpenRouter koşularında
3,33× şişik.** `_llm_cost_usd` fiyatı `agent.current_model()` ile çözüyor; bu
değer **thread-local** ve HTTP yoklama thread'inde koşunun pin'i görünmüyor,
dolayısıyla uygulama varsayılanına (`claude-fable-5`, $10/$50) düşüyor. Kimi K3
($3/$15) ile koşan `51a9f3ba`'da 32 çağrı / 189.832 token için:

| | tutar |
|---|---|
| fable-5 fiyatıyla — ekranda görünen | $5,09 |
| OpenRouter kimi fiyatıyla — gerçek | $1,53 |

Oran sabit 3,33× ve harmanlanmış $/MTok eğrisi fable-5'i izliyor ($26,8 vs
$8,04). Okuma hata vermediği için sayı makul görünür, artar, para birimi
doğrudur — yalnız yanlış fiyat listesinden gelir. Çare: modeli koşu durumuna
yazıp okuyucuların oradan alması (thread-local'dan yeniden çözmemesi).

## Kimi K3 ↔ Sonnet 5 — aynı birim fiyat, %65 fark (2026-08-03)

OpenRouter'ın canlı ucundan: **kimi-k3 $3/$15**, yani Sonnet 5 listesiyle
birebir aynı (Sonnet 5 ayrıca 31.08.2026'ya kadar tanıtım fiyatında: $2/$10).
Defterden ölçülen çağrı-başı maliyet:

| model | çağrı | girdi | çıktı | cache okuma | $/çağrı | çıktı/çağrı |
|---|---|---|---|---|---|---|
| kimi-k3 | 54 | 223.331 | 96.994 | **0** | **0,0397** | 1.796 |
| sonnet-5 (tanıtım) | 29 | **52** | 35.881 | 111.423 | **0,0241** | 1.237 |

Farkı iki kalem sürüyor, ikisi de fiyat listesinde görünmez: **çıktı uzunluğu**
(çıktı girdinin 5 katı fiyatlı) ve **önbellek**. Kimi'nin 54 çağrısında cache
okuma/yazma **sıfır** — her çağrı ~4.135 girdi tokenini tam fiyattan yeniden
gönderiyor; Claude yolunda 29 çağrının toplam ham girdisi 52 token. OpenRouter
kimi için cache okuma fiyatı ($0,30/MTok) *yayınlıyor*, entegrasyon kullanmıyor.
Yani önbellek desteği listede var olup senin yolunda hiç açılmamış olabilir;
bunu liste değil defterin `cache_read` sütunu söyler.

Kalite tarafında Kimi'yi tercih ettirecek ölçülmüş bir gerekçe çıkmadı: tek
somut şikâyet olan çıkış-bloğu ad↔kod uyuşmazlığı Sonnet 5 oturumlarında da var
(4/4, 2/4, 2/4) — yani üreticinin değil isteğin kusuru, model değiştirmek
düzeltmez (bkz. [[kesilme_ve_degrade_gorunurlugu]]).

## Model pini kalıcı değil

`agent.set_thread_model()` pini `threading.local()` üzerinde tutar ve
`agent_backtest.py` her koşunun başında form alanından set eder — yani seçim
**o koşuya ve o thread'e** aittir. Ölçülen −61% kazanç, koşudan 27 dakika sonra
buharlaşmış, AUTO yine `claude-fable-5`'e dönmüştü (`NAUTILUS_LLM_MODEL` set
değil → `current_model()` süreç varsayılanına düşüyor).

Deneyler için doğru tasarım — kalıcılık isteniyorsa `NAUTILUS_LLM_MODEL` (süreç)
ya da koddaki `MODEL` varsayılanı (depo) kapsamına taşınmalı. Model seçicinin
kendisi ve ücretsiz OpenRouter filtresi için bkz.
[[model_secici_ve_gorunurluk]].

**Açık risk:** `FALLBACK_MODEL = claude-opus-4-8`. Ucuz modele geçilse bile kredi
tükenince opus sınıfına düşülüyor — ve opus bu iş yükünde fable'dan pahalı
çıkmıştı. Varsayılan değiştirilirse fallback de gözden geçirilmeli.

## Defter kirliliği (açık)

Deftere 8 sentetik `custom_block` satırı düşmüş (fable-5; `in=10 out=5`, `20/7`,
`1/1`, `1/1` — batch sonlarında dörderli). `tests/test_token_ledger.py`
`LEDGER_PATH`'i monkeypatch'liyor, yani kaynak o değil; başka bir prob/test
üretim defterine yazıyor. Maliyeti sıfır, kaynağı bulunmadı.

## Uygulama sırası

1. Altı çağrı noktasına `_purpose` etiketi — %92'lik kör alan görünür olsun.
2. ~~`--effort` bayrağını CLI çağrısına geçir.~~ **Yapıldı (2026-08-04)** — brief'te
   `EFFORT` seçicisi, iki backend'de de bağlı. Seçenek artık var; **ölçümü yok**:
   −52% rakamı tek çağrılık probdan geliyor, üretim promptlarıyla doğrulanmadı.
3. Varsayılanı sonnet-5'e taşı (`NAUTILUS_LLM_MODEL` ya da `MODEL`), fallback'i
   birlikte gözden geçir. Effort ile ÇARPILIR: ölçülen bileşik −81%.
4. `composed`/`idea` prefix'ini sabitle (`custom_block`'ta uygulanan desen).

## Hibritte maliyet ATFI yanlış modele yazılıyordu (2026-08-15, KAPANDI)

Amaç-başına model eşlemesi (`NAUTILUS_MODEL_BY_PURPOSE`) geldikten sonra bir
koşuda birden fazla model para harcayabiliyor. Maliyet satırı bunu bilmiyor:
`web/routes/agent_backtest._llm_cost_usd(ti, to, tcr, tcw, model)` TEK bir model
alıyor ve `token_snapshot` bütün turu ona yazıyor.

Ölçülen vaka (koşu `14ff96e7`): `pricing_model: 'or:qwen3.8-27b'`,
`cost_usd: 1.019011`, `cost_source: 'provider_reported'`. **Sayı doğru, etiket
yanlış** — o 1,02 USD tamamen Claude'un 7 `custom_block` çağrısının bedeli
(14.050 çıktı token'ı, Claude CLI `total_cost_usd` bildiriyor). Yerel model
bedava; ekranda ise "yerel Qwen 1 dolar yaktı" gibi görünüyor.

Zararı sıradan bir etiket hatasından büyük: bu satır tam da "yerel model bedava"
olan kararı çürütür gibi duruyor. Rozet tarafındaki aynı boşluk kapatıldı
([[model_secici_ve_gorunurluk]], `hybrid_note`), maliyet tarafı **açık**.

Veri zaten vardı: defter her çağrıyı gerçek modeliyle yazıyor
(`_ledger_record(resp, called_model, purpose)`). Yanlış olan toplamaydı.

**Düzeltme (aynı gün):** `_add_tokens(run_id, usage, model)` artık kırılımı da
tutuyor (`state["by_model"]` — çağrı sayısı, dört token kalemi, sağlayıcı
maliyeti); telemetri gözlemcisi modeli iletiyor. Yeni `_run_cost(state,
fallback_model)` her modelin dilimini KENDİ fiyatıyla değerliyor (o dilim için
sağlayıcı bildirdiyse o, yoksa fiyat tablosu) ve toplamı veriyor. `pricing_model`
tek harcayan varsa onun adı, birden fazlaysa `"hibrit (N model)"` — tek bir ad
yazmak yalan olurdu. Kırılım `cost_by_model` olarak hem `token_snapshot`'a hem
kokpitin `token_info`'suna giriyor; bedava model 0 maliyetle listede kalıyor
(0 da bir bilgidir).

İki çağrı yeri (oturum logu + kokpit) artık AYNI hesabı paylaşıyor — daha önce
ayrı hesaplandıkları için aynı koşuya farklı maliyet gösterebiliyorlardı.
Kırılım yoksa (eski kayıtlar) eski tek-model yoluna düşülür, davranış korunur.
Testler: `tests/test_run_cost_is_per_model.py`.
Ölçüm: [[07_yerel_llm_hibrit_olcumu_2026_08_15]].

## Harcanmayan token bütçeyi yememeli (2026-08-16)

Yanıtsız çağrıya tahmini girdi yazmak kasıtlıydı ve ölçüme dayanıyordu: istemci
deadline'ında child öldürülse de üretim sunucuda sürüyor ve faturalanıyor, sıfır
saymak tavanı körletiyordu. Ama o gerekçe **"prompt gitti" varsayımına** dayanır ve
bağlantı hiç kurulamadıysa yanlıştır.

Koşu `f38273f2`: yerel uç kapalıyken 45 çağrının 45'i `APIConnectionError` verdi,
çıktı 0 kaldı, buna rağmen 252.459 tahmini girdi tokenı yazıldı. Koşu **4 dk 51
sn**'de "budget" gerekçesiyle kendini kapattı — kullanıcı hiçbir şey almadan
bütçesinin tamamını kaybetti ve ekranda gerçek sebep (ölü uç) yerine bütçe göründü.

Neden 250.000? Tavan tek değil, **iki**: maliyet görünürken
`RUNAWAY_MAX_TOKENS = 2.000.000`, görünmüyorken `BLIND_MAX_TOKENS = 250.000`. Hiçbir
çağrı başarılı olmayınca maliyet hiç gözlenmedi, koşu "kör" sayıldı ve sıkı tavana
düştü. İki kusur birbirini besledi: şişirilmiş tahmin, kör tavanı hızla doldurdu.

Muafiyet dar ve **somut istisna adına** bağlı, üst sınıfa değil: her iki SDK'da da
`APITimeoutError`, `APIConnectionError`'dan türer — `isinstance` ile bakmak timeout'u
da muaf tutar ve yukarıdaki tahmini-harcama ölçümünün kapattığı deliği geri açardı.
Bilinmeyen tip **şüphede sayılır**: muaf tutmak tavanı körletir, fazladan saymak
yalnız erken bitirir.

Sahada doğrulandı (`ed8ba569`): `APIConnectionError` → `usage=None`;
`InternalServerError` → tahmin yazıldı, çünkü istek sunucuya ULAŞTI ve sunucu sonra
düştü. Testler: `tests/test_auto_degradation_honesty.py`; biri SDK hiyerarşisi
değişirse uyarır. Ölçüm: [[09_baglam_ve_butce_olcumu_2026_08_16]].

**Muafiyet OLGUYA bağlıdır, tek bir SDK'nın sınıfına değil** (kod incelemesi
2026-08-16). İlk sürüm `isinstance(exc, anthropic.APIConnectionError)` istiyordu;
OpenRouter çağrılarının **iki taşıması** var — iptal edilebilir çocuk süreç ve
süreç-İÇİ istemci (`_process_config` yoksa ya da çağıran thread'de `cancel_check`
kayıtlı değilse). İkincisinde ham `openai.APIConnectionError` fırlıyor ve bu ayrı
bir hiyerarşi olduğu için muafiyet oraya hiç uğramıyordu: aynı sıfır-bayt çağrı,
hangi taşımadan geçtiğine göre ya muaf ya faturalıydı. Ayrımı yapan hâlâ **somut
ad** (yani alt sınıf `APITimeoutError` adla eleniyor); `isinstance` yalnız
"tanıdığımız bir SDK mı" diye bakar ve artık iki SDK'yı da tanır.

## `max_tokens` bu yolda BAĞLAYICI DEĞİL (ölçüldü 2026-08-17)

Sayfanın "`_ClaudeCLIMessages.create` `max_tokens`'ı DÜŞÜRÜR" notu tavanın
etkisiz kaldığı bir yönü zaten söylüyordu; canlı koşu ikinci ve daha keskin
yönü verdi. `custom_block` üretiminde tavan `1800` isteniyor, mod
`advisory_cli` — yani uç onu ZORLAMIYOR (39 çağrı, AUTO koşusu 8af5d495):

| | |
|---|---|
| tavanı aşan çağrı | **22 / 39 (%56)** |
| aşım | min +149 · medyan **+733** · maks **+1.707** |
| istenen 1800 → gerçekleşen | medyan **2.533 (×1,41)** · en kötü **3.507 (×1,95)** |

Örneklem 20 çağrıyken medyan ×1,31'di; ikiye katlanınca ×1,41 oldu, oran
(%55→%56) sabit kaldı — gürültü değil sistematik sapma. Aşım TEK YÖNLÜ: tavan
zorlanmadığı için gerçekleşen değer altına düşmüyor, yalnız üstüne çıkıyor,
dolayısıyla tavanı girdi alan her maliyet hesabı iyimser yanılıyor.

Aşım **yalnız `custom_block`** amacında: aynı koşudaki diğer 17 çağrının hiçbiri
tavanı aşmadı. Yani "CLI advisory modu genel olarak gevşek" değil — kod üretimi
istenen bütçeye sığmıyor, diğer görevler sığıyor. `status: ok`, yani kesilme yok
ve codegate'ten geçen bloklar tam; etkilenen şey kalite değil MALİYET TAHMİNİ.

Sonuç: `_admit_llm_budget` (2026-08-17) girişte para tavanını zorlarken sıradaki
çağrının maliyetini TAHMİN ETMİYOR, harcanmış parayı okuyor. İkinci gerekçe bu
ölçüm. Doğru yapı zaten rezervasyon tarafında vardı —
`output_token_bound = max(configured, observed_high_water)` gözlenen zirveyi
öğreniyor; eksik olan maliyet tarafının aynı gerçeği kullanmamasıydı.

## Üretim tavanları defterden ölçüldü (2026-08-17)

`max_tokens` bu uçta advisory; değerler tahminle konmuştu. `token_usage.jsonl`'daki
GERÇEKLEŞEN `output` dağılımı (9.795 kayıt; parantez = son 300 çağrı):

| amaç | n | medyan | p90 | p95 | p99 | maks | eski → yeni |
|---|---:|---:|---:|---:|---:|---:|---|
| `idea` | 507 | 1.542 **(4.190)** | 8.256 | 9.790 | 13.879 | 15.559 | **1.500 → 12.000** |
| `composed` | 596 | 2.216 (2.956) | 8.220 | 9.190 | 12.181 | 13.549 | **4.000 → 10.000** |
| `custom_block` | 1.566 | 1.196 (1.732) | 2.889 | 3.268 | 3.785 | 4.270 | **1.800 → 6.000** |

`idea`'nın MEDYANI tavanının 2,8 katıydı — kesilme istisna değil TİPİK durumdu.
Koşu `d515080e` tam böyle öldü (`fallback_reasons: ["TruncatedResponse"]`).

Yerleştirme kuralı: tavan **p99'un hemen ALTINA** konur, üstüne değil. Gerekçe
tırmanma yolunun korunması — `bigger = min(base×4, RETRY_CAP)` ve `bigger <= base`
olduğunda kod doğrudan hata atar, yani tavanı 16.000'e eşitlemek kuyruğu retry ile
kurtarılabilir olmaktan çıkarıp sert hataya çevirirdi.

İki yan bulgu: `AGENT_CUSTOM_BLOCK_MAX_TOKENS` ayarının `hi`'si de 1.800'dü (ayar
vardı, yukarı yolu yoktu) → 16.000'e çekildi; `AGENT_CUSTOM_BLOCK_TOKEN_LIMIT`
(25.000) kaçak-döngü freniydi ama ölçüm zaten ateşleme menzilinde olduğunu gösterdi
(çağrı başına toplam maks 16.121 → iki denemede 32.242) → 40.000.

**Canlı doğrulama:** koşu 755b7880'in üç turunda `TruncatedResponse` YOK
([[nau_auto_kosusu_755b7880_2026_08_17]]).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_mission_control]]
- [[model_secici_ve_gorunurluk]]
- [[nau_auto_kosusu_755b7880_2026_08_17]]
- [[nau_bulgu_kapatma_turu_2026_08_17]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
