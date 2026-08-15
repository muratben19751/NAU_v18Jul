---
title: Kesilme (max_tokens) ve degrade görünürlüğü
type: synthesis
summary: Yapılandırılmış çıktı isteyen çağrılarda max_tokens bir doğruluk önkoşuludur; Claude'un kısa JSON'una göre ayarlanmış tavanlar reasoning tarzı bir modelde yanıtı kesip hatayı JSONDecodeError kılığına sokuyordu — tavanlar büyütüldü, kesilme kendi tipini kazandı ve fallback'ler artık sayılıp ⚠ ile ekrana taşınıyor.
sources:
  - sources/07_yerel_llm_hibrit_olcumu_2026_08_15.md
  - https://github.com/nautechsystems/nautilus_trader
  - https://openrouter.ai/api/v1/models
key_concepts:
  - strategy_studio
related:
  - wiki/synthesis/model_secici_ve_gorunurluk.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/nau_deepr_toplu_sertlestirme_2026_08.md
last_updated: 2026-08-15
---

# Kesilme (max_tokens) ve degrade görünürlüğü

2026-08-03. [[model_secici_ve_gorunurluk]] OpenRouter yolunu açtı; ilk gerçek
koşu (`362adcd1`, `moonshotai/kimi-k3`, QQQ.NASDAQ 1-HOUR) **5/5 iterasyonu**
fallback'le üretti ve bütün fazları `✓` kapattı. Sebep ne eksik paket ne 429'du —
o sayfanın "Kalan iş" bölümünün öngördüğü **üçüncü sebep** geldi.

## Teşhis: kod doğru, sabit eski

Ölçüm tek tabloda okundu. `token_usage.jsonl`, aynı koşu:

| purpose | tavan | output | sonuç |
|---|---|---|---|
| composed ×3 | 900 | **900 · 900 · 900** | 3/3 `JSONDecodeError` |
| idea ×2 | 400 | **400 · 400** | 2/2 fallback |
| custom_block ×4 | 4.000 | 1.100 · 1.190 · 1.787 · 725 | 4/4 başarılı |

Tavana **tam olarak** dayanan 5 çağrının 5'i başarısız, bolluk verilen 4
çağrının 4'ü başarılı. Model bozuk değildi: tavanlar Claude'un kısa JSON'una göre
kalibre edilmişti, kimi-k3 ise önce düşünüp sonra yazıyor ve JSON kapanmadan
bütçe bitiyordu.

> `output == max_tokens` eşitliği kesilmenin parmak izidir ve zaten tuttuğumuz
> defterde bedava durur — model bir cevabı rastgele tam o sayıda bitirmez.

Asıl kusur sayının küçüklüğü değil, **kesilmenin kendi adıyla gelmemesi**:
`json.loads` bir `JSONDecodeError` fırlatıyor, mesaj modeli suçluyor ("geçerli
JSON üretemedi") ve teşhis "bu model zayıfmış, başkasını deneyelim" yönüne
sapıyordu. Oysa cevabı kesen bizdik.

## Üç değişiklik

**1. Tavanlar ölçülmüş gerçeğe bağlandı.** `MAX_TOKENS_COMPOSED = 4000`,
`MAX_TOKENS_IDEA = 1500` (`agent.py`). Tavan promptun değil **üretenin**
özelliğine göre kalibre olduğu için bolluk tarafında hata yapılır: kullanılmayan
tavan bedava, yarım JSON toptan kayıp. Testte sabitin kendisi değil **gerekçesi**
pinlendi (`>= 2000` / `>= 1200`, ölçülen ~1.8K mertebesine karşı).

**2. Kesilme kendi tipini kazandı.** `TruncatedResponse` + `_was_truncated()`;
`_ORResponse` OpenAI'nin `finish_reason="length"`'ini Anthropic'in
`stop_reason="max_tokens"`ine çevirir, `_CLIResponse` için `None`'dır (CLI'nin
`max_tokens` karşılığı yok, o yolda kesilme olamaz). Kontrol tek choke point'te —
`_create_message` — yapılır: kesilmede tavan **bir kez** ×4 büyütülüp yeniden
denenir (kesilme kalıcı arıza değil bütçe arızasıdır, 429 gibi), yine sığmazsa
`TruncatedResponse` fırlar. Çağıranın fallback'i yine devreye girer ama açıklama
artık `fallback (TruncatedResponse)` yazar — sebep okunabilir kalır.

**3. Fallback görünür oldu.** Üç fallback yolu (`propose_composed_strategy`,
`_propose_agent_strategy_idea`, `propose_strategy`) artık makine-okunur bir
`degraded` alanı taşır; açıklamanın kuyruğundaki metni ayrıştırmak kırılgandı.
Koşu tarafında `_mark_degraded()` sayar (`fallback_count` + tekrarsız
`fallback_reasons`), `_done_phase(..., degraded=True)` fazı ✓ yerine ⚠ kapatır ve
kokpitte `⚠ DEGRADED ×N` rozeti çıkar.

**4. Öğrenilen tavan kilitlendi (2026-08-08, DeepR).** Büyütülmüş tavan
`_LEARNED_MAX_TOKENS[(model, purpose)]`'a yazılır — aynı uç bir daha ilk
çağrıyı çöpe atmasın diye. Sözlük kilitsizdi: AUTO birden fazla worker
thread'i çalıştırabildiğinden, aynı anahtarı aynı anda büyüten iki thread
birbirinin yazdığını ezebiliyordu (çökme değil, "öğrenilen tavan" ara sıra
kaybolup bir sonraki çağrının yine kesilip 2× maliyete düşmesi). Düzeltme:
`_LEARNED_MAX_TOKENS_LOCK` + yazımda `max(bigger, mevcut)` — küçük bir eşzamanlı
yazım artık daha büyük, zaten kanıtlanmış bir tavanı geriletemiyor.

Faz `status` değeri yine `"done"` kalır — `mission_view` ilerlemeyi
`all(s == "done")` ile sayar ve akışı bozmak istemiyoruz; bozulma **ayrı bir
bayrakla** taşınır. Böylece koşu ne yarıda kesilmiş görünür ne de başarı gibi.

## Neden ayrı işaret gerekiyordu

Aynı koşuda iki fallback yolu vardı ve zararları taban tabana zıttı:

| iterasyon | üretilen | skor |
|---|---|---|
| 0, 2, 4 | rastgele kompozisyon | −inf · −3.32 · −0.40 |
| 1, 3 | `_FALLBACK_IDEAS[0]` = "RSI Oversold Reversal" | **0.8181** · **0.7562** |

Rastgele kompozisyon kötü olduğu için kendini ele verdi. Kaynak kodda sabit duran
fikir ise 136 işlemle sıralamanın tepesine çıktı: koşu, `agent.py:2706`'daki bir
dizgeyi **ajanın keşfi** olarak taçlandırmak üzereydi. Zarar, yedeğin kötülüğüyle
değil iyiliğiyle orantılı — bu yüzden ayrım logda değil **sonucun kendisinde**
durmalı.

İkinci imza tekrardı: iterasyon 1 ve 3 birebir aynı stratejiyi üretti. Geçmişi
görüp yeni öneri üreten bir sistem aynı öneriyi iki kez yapmaz; üretici bir
döngüde **çeşitlilik bir sağlık göstergesidir**.

## Doğrulama

Aynı model, aynı enstrüman, 2 iterasyon (`08d207ab`):

| purpose | eski tavan | yeni çıktı |
|---|---|---|
| composed | 900 (hep kesiliyordu) | **1.195** |
| idea | 400 (hep kesiliyordu) | **492** |

İkisi de eski tavanın üstünde — yani kesilme gerçekti, tesadüf değil. Üretilen
stratejiler: "Squeeze MACD Ride" ve "Donchian Breakout Ride"; `degraded` olayı
**0**. Yeniden deneme yolu hiç tetiklenmedi: yükseltilen tavanlar tek başına
yetti, retry ağı ileride başka bir model için duruyor.

## Açık kalan

`token_usage.jsonl` kayıtlarında `run_id` yok; bir koşunun çağrıları ancak zaman
damgasından ayrıştırılabiliyor. Bir koşunun "gerçekten çalıştı mı" sorusunu
defterden yanıtlamak bu yüzden elle filtreleme gerektiriyor.

Diğer JSON çağrılarının tavanları (refine 700, chat 1000, breakdown 1500)
dokunulmadan bırakıldı — ölçülmüş bir kesilmeleri yok ve artık retry ağı
altlarında duruyor. Kesilirlerse bunu bir kez fazladan çağrıyla telafi ederler;
kalıcı çözüm tavanı modele göre çözmek olurdu.

## Anlatı düşüşü sessizdi — ve STOP'u yutuyordu (2026-08-15)

Üç anlatı yüzeyi (`backtest._generate_narrative`, `lab._lab_narrative`,
`agent_backtest._winner_narrative`) aynı deseni taşıyordu:
`except Exception:` → şablon cümlesi döndür. İki ayrı kusur:

1. **Sessizlik.** LLM hiç konuşmasa bile ekranda normal duran bir cümle çıkıyordu:
   `degraded` bayrağı yok, log yok. Ölçüm sırasında "LLM mi konuştu, şablon mu"
   sorusu ancak şablon metnini birebir yeniden üretip karşılaştırarak
   yanıtlanabildi — yani dışarıdan **ayırt edilemez** durumdaydı.
2. **STOP'un yutulması (asıl kusur).** `LLMCallCancelled` de bir `Exception`.
   Koşu iptal edilirken anlatı üretiliyorsa iptal, başarılı görünen bir cümleye
   dönüşüyordu. `llm_client._raise_if_llm_control_abort`'un docstring'i tam olarak
   bunu yasaklıyor ("Never disguise STOP/budget control flow as a successful
   fallback") — **sözleşme vardı, üç yer de çağırmıyordu.**

Düzeltme üçünde aynı: önce `_raise_if_llm_control_abort(e)` (STOP ve
`llm_control_abort` işaretli bütçe iptalleri yukarı geçer), sonra sebebi ve
yüzeyi adıyla yazan `logging.warning(..., exc_info=True)`. `llm_client` import'u
except içinde kendi guard'ında: `agent` import'u patladıysa fallback yine
çalışsın, düzeltme yeni bir sert hata yüzeyi açmasın.

Ders: bir sözleşmenin var olması her çağrı yerinde uygulandığı anlamına gelmez;
testle zorunlu kılınmalı. `tests/test_narrative_fallback_is_not_silent.py` üç
yüzeyi ayrı ayrı parametrize eder (sıradan hata log'a geçer / STOP yutulmaz /
bütçe iptali yutulmaz).

## Yerel uçta tavan tırmanışı ölçüldü (2026-08-15)

Öğrenilen-tavan mekanizması yerel Qwen3.8-27B'de tam da tasarlandığı gibi
çalıştı — ve maliyeti **süreç başına bir kerelik** çıktı, çağrı başına değil:
`idea` 1500 → 6000 → 16000 tırmandıktan sonra 6 üretimin 6'sı tek çağrıda,
`narrative` 200 → 800'den sonra 5'in 5'i tek çağrıda bitti.

Ama tırmanış yeni bir sınır doğurdu: 16000'lik tavan modelin ~10k token yazmasına
izin veriyor, ~52 tok/s'de bu ~190 s eder ve `NAUTILUS_LLM_CALL_TIMEOUT`'un 120 s
varsayılanına çarpar (ölçüm: 8 `idea` üretiminin 1'i). Üretimde 300 s'e çekildi.
**Bağlı sabit dersi:** bir sabiti kalibre ederken ona bağlı olanı da kalibre et —
tavan kesilmeyi çözerken duvar saatini zorlar. Ölçüm: [[07_yerel_llm_hibrit_olcumu_2026_08_15]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[llm_maliyet_kaldiraclari]]
- [[model_secici_ve_gorunurluk]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
