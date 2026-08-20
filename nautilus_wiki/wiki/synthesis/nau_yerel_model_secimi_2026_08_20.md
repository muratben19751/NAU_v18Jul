---
title: Yerel model seçimi ölçüldü — 32B sığmıyor, 26B yazmıyor, 14B çalışıyor
type: synthesis
summary: Yerel uç llama-server'dan Ollama'ya taşındı ve üç model aynı prompt'la ölçüldü: qwen32B 426 sn (16 GB karta sığmıyor, %37'si CPU'da), gemma4:26b bütçenin tamamını `reasoning` alanına harcayıp BOŞ içerik döndürdü, qwen14B 31 sn'de geçerli JSON. Ayrıca 4.096'lık varsayılan bağlam prompt'u sessizce yarıya kırpıyordu.
key_concepts:
  - model_secici_ve_gorunurluk
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/model_secici_ve_gorunurluk.md
  - wiki/synthesis/auto_arama_ekonomisi.md
last_updated: 2026-08-20
---

# Yerel model seçimi ölçüldü (2026-08-19/20)

Yerel LLM ucu `llama-server:8080`'den **Ollama:11434**'e taşındı
(`ecosystem.config.js`). Taşımanın ardından üç ayrı arıza üst üste çıktı ve
üçü de "model kötü" gibi göründü; hiçbiri değildi.

## 1. Bağlam penceresi prompt'u sessizce yarıya kırpıyordu

İlk koşuda beş üretimin beşi rastgele fallback'e düştü. Hata `missing 'blocks'`
— yani model geçerli JSON döndürüyor ama şemanın zorunlu alanı yok.

Sebep, Ollama'nın modeli **varsayılan 4.096 bağlamla** yüklemesiydi (model
32.768 taşıyor: `n_ctx_seq (4096) < n_ctx_train (32768)`). Composed prompt'u ise
**7.787 token**. llama.cpp sığmayan prompt'u `n_ctx/2`'ye kırpıyor.

Teşhis parmak izi bedava ve genel: **uzunlukları çok farklı iki metin aynı
`prompt_tokens` sayısını bildiriyorsa okuduğun sayı ölçüm değil TAVANDIR.**

| gönderilen | uzunluk | bildirilen |
|---|---|---|
| yalnız katalog | 13.548 krktr | **2.050** |
| tam sistem prompt | 19.174 krktr | **2.050** |

`OLLAMA_CONTEXT_LENGTH=16384` ile sunucu yeniden başlatılınca sayılar 6.419 /
7.787'ye çıktı ve `blocks` gelmeye başladı. Kırpma stokastik bir arıza üretiyordu
— elle tek deneme "çalışıyor" diyordu, koşuda 5/5 düşüyordu.

**Prompt'un %82'si katalog** (7.787'nin 6.419'u). Katalog özetini kısaltmak her
model için doğrudan kazanç; ayrı bir iş olarak duruyor.

## 2. Üç modelin ölçümü (aynı prompt, aynı pencere)

| | qwen2.5-coder:32b | gemma4:26b | **qwen2.5-coder:14b** |
|---|---|---|---|
| prompt işleme | 405 sn | 337 sn | **26 sn** |
| gerçek strateji üretimi | 426 sn | 633 sn | **31 sn** |
| çıktı | 285 tok, `stop` | 3.000 tok, `length` | 342 tok, `stop` |
| `content` | JSON, 4 blok | **BOŞ** | JSON, 4 blok |
| `reasoning` | — | 7.832 krktr | — |
| toplam boyut / GPU payı | 22,5 GB / %63 | 18,1 GB / %73 | sığıyor |

**gemma4 bir düşünen model** ve çıktısını ayrı bir `reasoning` alanına yazıyor;
uyumluluk katmanı yalnız `content` okuduğu için (`openrouter_backend.py:392`)
sonuç HTTP 200 + boş metin. Bütçeyi büyütmek kurtarmıyor: 8.108 girdi + 10.000
çıktı = 18.108 > 16.384 bağlam.

**32B donanıma sığmıyor.** Kart RTX 5080 / **16 GB**; 32B Q4 tek başına 20,7 GB.
Hiçbir bağlam ayarıyla sığmaz, ~%37'si CPU'da koşar. Bağlamı küçültmek yalnız
kırpmayı geri getirir — ikisi aynı sığmama probleminin iki yüzü.

**14B seçildi.** 9,0 GB ağırlık + 16k KV ≈ 12 GB, karta sığıyor. AUTO turu:
15 iterasyon + 30 backtest + sıralama, **~4 dakika**; 32B'de yalnız üretim
tarafı 1,8 saat sürecekti.

## 3. Ölçümün kendisi kirlendi: kart doluyken alınan sayı

14B'nin ilk ölçümü **214 sn** çıktı ve "sadece 2 kat kazanç, inmeye değmez"
sonucuna götürüyordu. `/api/ps` bakılınca gemma4'ün hâlâ VRAM'de olduğu görüldü
(15.720/16.303 MiB). Runner süreçleri düşürülüp kart boşaltılınca aynı ölçüm
**31 sn**. **6,9 kat fark, ve ilk sayı kararı ters çevirecekti.**

Ders çift taraflı: kıyas ölçümünden önce kaynağın boş olduğu **ölçülmeli**; ama
"kaynak dolu" görülünce sorulacak soru "nasıl boşaltırım" değil **"kim
kullanıyor"** olmalı — o runner'lardan biri o sırada koşan bir AUTO turuna aitti
ve düşürülmesi o koşuyu bozdu.

## 4. `custom_block` yolu: 500 baytlık ".exe"

Aynı günlerde `custom_block` üretimi her çağrıda düştü:

```
_CLIError: claude CLI exited 1: This version of ...\claude.exe is not
compatible with the version of Windows you're running.
```

Mesaj Windows sürümünü suçluyordu; makine AMD64/64-bit, uyumsuzluk yok.
`claude.exe` **500 bayt** ve bir PE ikilisi değil, kabuk betiğiydi: npm paketi
(`@anthropic-ai/claude-code` 2.1.235) yerel ikili yerine yer tutucu bırakmış
(`postinstall` koşmamış). Onarım sonrası dosya **326,5 MB**, `MZ`/`PE`/x64, ve
`--version` cevap veriyor.

Etkisi ölçüldü — aynı model, aynı ipucu, tek değişken `custom_block`:

| | bozukken | onarıldıktan sonra |
|---|---|---|
| özel blok denemesi | 21, **hepsi düştü** | 14, **11 başarılı** |
| sıralamayı geçen | 1/15 | **3/15** |
| tur süresi | ~3,8 dk | ~28 dk |

Kapı oranı üçe katlandı; süre de arttı çünkü o yol artık gerçekten kod üretip
`codegate` denetiminden geçiyor.

Kod tarafı: `ecosystem.config.js` (pin + uç), `openrouter_backend.py`,
`agent.py` (`_call_claude_for_block`).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[model_secici_ve_gorunurluk]]
<!-- BACKLINKS:END -->
