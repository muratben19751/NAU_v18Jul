---
source: AUTO koşuları 0057a0cd / 1fa9870e / 5e89d42a + b3dbd2b0 / 71b90408 / 50170b68
retrieved: 2026-08-16
type: measurement
immutable: true
---

# Hibrit AUTO koşularının ölçümleri (2026-08-15/16)

Yerel uç `or:qwen3.8-27b` (llama.cpp CUDA 13.3, RTX 5080), `custom_block`
Claude'da. Altı koşu; üçü sonuçsuz durdu, üçü ölçüm verdi.

## Koşu karşılaştırması

| koşu | süre | LLM | timeout | backtest | tamamlanan tur | duran sebep | fatura |
|---|---|---|---|---|---|---|---|
| `0057a0cd` | 28 dk | 22 | **2** | 7 | **0** | token tavanı | 1,03 USD |
| `1fa9870e` | 78 dk | 81 | 0 | 26 | 1 | para tavanı | 5,09 USD |
| `5e89d42a` | 66 dk | 77 | 0 | 33 | **2** | para tavanı | 5,07 USD |

`b3dbd2b0`, `71b90408`, `50170b68`: asılma (aşağıda).

## Bulgular

### 1. `NAUTILUS_LLM_CALL_TIMEOUT` yerel uca hiç ulaşmıyordu

`_create_message_once` zamanaşımını `if not isinstance(client, _ClaudeCLIClient)`
ile enjekte ediyordu. Karar VARSAYILAN istemciye bakıyordu; varsayılan backend
CLI iken `or:` pinli çağrı yine OpenRouter'a gidiyor ve orada kwargs'ta
zamanaşımı olmadığı için `_run_openrouter_killable` kendi 120 s varsayılanına
düşüyordu. Env 300 yazıyordu, `pm2 env` doğruluyordu, davranış eskiydi.

Teşhisi veren şey hata metnindeki SAYIydı: 300 ayarlayıp 120 görmek.
Düzeltme koşulu hedefe bağladı: `model.startswith("or:") or not isinstance(...)`.

### 2. Bütçe tek sayaçtı; bedava token da fatura sayılıyordu

`0057a0cd`: bütçenin %92'sini (194.375/210.411 token) hiç para harcamayan yerel
model yedi, gerçek fatura 1,03 USD'ydi, tur 28 dakikada tek round kapatamadan
kesildi. Bedava model, bedava olduğu için değil SAYILDIĞI için koşuyu kısalttı.

Çözüm iki tavan, iki birim: para (`AGENT_DEFAULT_MAX_COST_USD`) faturayı, token
(`AGENT_RUNAWAY_MAX_TOKENS`) kaçak döngüyü. Körlük şartı: hiçbir modelde maliyet
okunamıyorsa token tavanı eski sıkı değerine iner (`AGENT_BLIND_MAX_TOKENS`).

Uygulama tuzağı: route, token tavanını worker'a ULAŞMADAN kelepçeliyordu ve
`HARD_MAX_AUTO_TOKENS`'ın varsayılanı da eski sabitten türüyordu — üçü birden
değişmeden davranış değişmiyor.

### 3. Maliyet atfı yanlış modele yazılıyordu

`token_snapshot`: `pricing_model: 'or:qwen3.8-27b'`, `cost_usd: 1.02`. Sayı
doğru, etiket yanlış — o para tamamen Claude'un `custom_block` çağrılarının
bedeliydi. Ekranda "yerel model 1 dolar yaktı" gibi görünüyordu, yani satır tam
da "yerel bedava" kararını çürütür gibi duruyordu.

### 4. Benchmark kapısı enstrümanı eliyordu, stratejiyi değil

`1fa9870e`'nin en iyi adayı Calmar'da buy&hold'u GEÇİYORDU (0,292 vs 0,269) ama
yıllık alfası −%6,8 olduğu için elendi. QQQ 22,7 yılda yılda %14,5 yapmış.

Pencereyi kısaltmak kurtarmıyor — ölçüldü:

| pencere | buy&hold CAGR | MaxDD | Calmar |
|---|---|---|---|
| 3 yıl | **%24,2** | −%22,9 | 1,06 |
| 5 yıl | %14,2 | −%35,6 | 0,40 |
| 10 yıl | %19,9 | −%35,6 | 0,56 |
| 22,7 yıl | %14,2 | −%53,5 | 0,27 |

Mevcut pencere zaten en yumuşaklardan biri. Sorun kapının mutlak getiri
istemesiydi.

### 5. Risk-ayarlı kapının seçtiği strateji SINIFI

Üç bağımsız geçen aday, üçü de aynı profilde — piyasadan AZ kazanıp düşüşü
yarıya/üçte bire indiren:

| aday | CAGR (piyasa %14,6) | MaxDD (piyasa −%54) | Calmar (piyasa 0,27) |
|---|---|---|---|
| `Williams %R Reclaim` [1H] | %8,63 | −%27,3 | 0,316 |
| `ADX ATR Trend Edge` [4H] | %8,63 | −%24,6 | 0,350 |
| `DMI ATR Regression` [4H] | %5,20 | −%17,7 | 0,294 |

`5e89d42a`: 27 adayın 2'si geçti (22 × `worse_risk_adjusted`,
3 × `not_profitable`) — %7,4 geçiş, seçici bir kapı.

## Asılmalar (çözülmedi)

| koşu | nerede | satır | nabız |
|---|---|---|---|
| `b3dbd2b0` | ilk LLM çağrısı | 10 | 0 |
| `71b90408` | ilk LLM çağrısı | 10 | 0 |
| `50170b68` | tur ortası (22 backtest sonra) | 730 | 96, sonra kesildi |

Üçünde de süreç AYAKTA kaldı (pm2 restart sayacı değişmedi, PID aynı),
`session_end` hiç yazılmadı, deadline hiç tetiklenmedi. Üçü de
2026-08-15 22:44–00:00 aralığında; sonraki koşular (sabah) asılmadı.

Hipotez `_run_openrouter_killable`'ın `proc.start()`'ında asılmaydı
(`sandbox.py` aynı sınıfı `pythonw.exe`/`set_executable()` ile çözmüş, OpenRouter
yolunda o koruma yok) ama nabız worker'dan AYRI bir thread olduğu için
`50170b68`'in profili bununla uyuşmuyor. Üç hipotez, sıfır ayırt edici kanıt.

Bu yüzden yama yerine GÖZLEM seçildi: stall watchdog (`NAU_AUTO_STALL_DUMP_SEC`,
300 s sessizlikte `faulthandler` ile TÜM thread'lerin izi `<run_id>.stall.txt`'e).
Henüz gerçek bir asılmada tetiklenmedi.
