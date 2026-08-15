---
source: Yerel llama-server (Qwen3.8-27B) ölçüm turları + AUTO koşusu 14ff96e7
retrieved: 2026-08-15
type: measurement
immutable: true
---

# Yerel LLM ölçümü ve hibrit koşu (2026-08-15)

Yığın: `llama.cpp b10424 CUDA 13.3` + `unsloth/Qwen3.8-27B-UD-Q3_K_XL.gguf`
(12.52 GB), RTX 5080 16 GB — tüm katmanlar GPU'da (15226/16303 MiB), ~52 tok/s.
Uç OpenAI-uyumlu; NAU'ya **kod değişikliği olmadan** bağlandı
(`OPENROUTER_BASE_URL=http://127.0.0.1:8080/v1` + `NAUTILUS_OPENROUTER_MODELS`
pin'i → picker'da `OR · qwen3.8-27b`).

Sunucu bayrakları: `-ngl 99 -c 16384 -fa on --jinja --reasoning-format deepseek`.
Sonuncusu şart: düşünme izini `reasoning_content`'e ayırır, aksi hâlde
`agent._extract_json_object` metindeki İLK dengeli `{...}` bloğunu alacağı için
düşünme izindeki taslak JSON'u nihai cevap sanabilir.

## Dört LLM yolunun tamamı ölçüldü

| yol | çıktı sözleşmesi | başarı | süre aralığı | karar |
|---|---|---|---|---|
| `narrative` | düz metin | **6/6** | 3.2 - 11.3 s | yerelde |
| `idea` | JSON | **8/8** | 28.7 - 189.2 s | yerelde |
| `composed` | JSON + şema doğrulaması | **10/10** | 27 - 222 s | yerelde |
| `custom_block` | codegate AST + rol sözleşmesi | **4/8** | 20 - 226 s | **Claude'da** |

Desen: **çıktının sözleşmesi katılaştıkça yerel modelin başarısı düşüyor.**

### `custom_block` neden düşüyor

`agent._call_claude_for_block`:
`AGENT_CUSTOM_BLOCK_MAX_TOKENS = _env_bounded(..., 1_800, lo=512, hi=1_800)` —
varsayılan VE üst sınır 1800, yani env ile yukarı çekilemiyor. Düşünen model
tavanı her çağrıda aşıyor → kesilme → emniyet ağı 7200 ile yeniden deniyor →
üretim 70-90 s → `AGENT_CUSTOM_BLOCK_TIMEOUT`'un 75 s'lik varsayılanına çarpıyor.
Deadline izin verilen tavana (120 s) çekilince başarı %25 → %50.

### İlk turun ARTEFAKTI (kayda geçsin, tekrarlanmasın)

İlk 20 çağrılık tur `custom_block`'ta 0/10 verdi ve düşüşlerin çoğu "üretilen kod
10 s duvar saatini aştı" diyordu — teşhis "model kaçak döngü yazıyor" olacaktı.
YANLIŞ. `NAUTILUS_OPENROUTER_SDK_RETRIES` varsayılanı 2; timeout'ta SDK yeniden
deniyor ama terk edilen istek llama-server'da koşmaya devam ediyor (logda 4
slotun da dolu olduğu görüldü), smoke alt süreci CPU'da aç kalıp 10 s'yi aşıyor.
Retry kapatılınca duvar-saati düşüşü **sıfır**. Kontrol deneyi: elle yazılmış
trivial blok + eski Claude blokları aynı kapıdan geçirildi — kapı **1.4 s**'de
karar veriyor.

## Hibrit: amaç-başına model (`NAUTILUS_MODEL_BY_PURPOSE`)

Model pini koşu başınaydı; ölçüm tek düğmenin pahalı olduğunu gösterdi.
`llm_client.model_for_purpose(purpose)` eşlemeyi pinin üstüne uygular; eşleme
yoksa `current_model()` ile birebir aynı cevabı verir. `llm_dispatch` iki yerde
kullanır: sağlayıcıya giden model VE **öğrenilen tavanın anahtarı** (aksi hâlde
yerelde öğrenilen 7200'lük tavan aynı turda Claude çağrısına sızar).

Üretim ayarı: `custom_block=claude-fable-5`, gerisi koşu pininde.

## AUTO koşusu 14ff96e7 (hibrit canlı, git `df59ff5`)

BTCUSDT spot, hint "Linear Regression Channel", intervals `['60','240','D']`,
15 dk, 7. turda kullanıcı durdurdu.

**19 LLM çağrısının 19'u doğru uçta:** `composed` ×8 + `idea` ×4 → yerel,
`custom_block` ×7 → claude-fable-5. 7 strateji önerildi, **hiçbiri `degraded`
değil**. Kesilme yalnız 2 (bir kerelik tavan tırmanışı). Hata/timeout **0**.
En uzun çağrı 103 s.

### Koşuyu bitiren şey LLM değil, veri penceresi

7 turun 6'sı `RuntimeError: Insufficient data`:

| interval | bulunan bar | gereken |
|---|---|---|
| 60 | 264 | çalıştı |
| 240 | 43-66 | 240 |
| D | 11 | 240 |

Tarih penceresi 2026-07-06 → 2026-07-16, yalnız **10 gün**. AUTO timeframe'leri
round-robin dağıttığı için her 3 turun 2'si daha başlamadan ölüyor; holdout da
atlandı (60 günlük OOS 10 günden çıkmaz). Koşan 3 backtest 1/2/12 işlem üretti,
`min_trades=20`'nin altında.

### Maliyet satırı hibridi bilmiyor

`token_snapshot`: `pricing_model: 'or:qwen3.8-27b'`, `cost_usd: 1.019011`,
`cost_source: 'provider_reported'`.

Sayı doğru, **etiket yanlış**: o 1,02 USD tamamen Claude'un 7 `custom_block`
çağrısının bedeli (14.050 çıktı token'ı; Claude CLI `total_cost_usd` bildiriyor).
Yerel model bedava. `_llm_cost_usd` tek bir `model` alıyor ve
`web/routes/agent_backtest.py` bütün turu ona yazıyor. Defter zaten çağrı başına
doğru modeli tutuyor (`_ledger_record(resp, called_model, purpose)`) — veri var,
toplama yanlış.
