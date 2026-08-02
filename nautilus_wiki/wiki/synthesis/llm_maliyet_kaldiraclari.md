---
title: LLM maliyet kaldıraçları — AUTO ve Studio token tüketimi
type: synthesis
summary: 10 günlük defter denetimi — 21,5M token / ~$346 nominal, %99'u fable-5'te; fatura output ($103) + cache yazımı ($234, 1h TTL ×2). Ölçülen kaldıraçlar sırayla model (sonnet-5 −61%), effort (ek −52%), cache prefix sabitliği; max_tokens CLI yolunda ölü, çağrıların %92'si etiketsiz.
sources:
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
last_updated: 2026-08-02
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

## Kaldıraç 2 — kullanılmayan CLI bayrakları

`agent.py :: _ClaudeCLIMessages.create` şu an yalnız `--tools ""`,
`--no-session-persistence`, `--strict-mcp-config`, `--system-prompt-file`
geçiyor. Kullanılmayan üç maliyet kolu:

| bayrak | etki |
|---|---|
| `--effort low\|medium` | sonnet-5'te ek −52%; fable'da yalnız −11% (getiri modele bağlı) |
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
2. `--effort` bayrağını CLI çağrısına geçir.
3. Varsayılanı sonnet-5'e taşı (`NAUTILUS_LLM_MODEL` ya da `MODEL`), fallback'i
   birlikte gözden geçir.
4. `composed`/`idea` prefix'ini sabitle (`custom_block`'ta uygulanan desen).

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_mission_control]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
