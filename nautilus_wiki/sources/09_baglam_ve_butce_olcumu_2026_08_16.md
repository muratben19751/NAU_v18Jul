---
source: AUTO koşuları f38273f2 / ed8ba569 / 392287b2 + llama-server bayrak değişimi
retrieved: 2026-08-16
type: measurement
immutable: true
---

# Bağlam penceresi ve bütçe muhasebesi ölçümü (2026-08-16)

Aynı gün üç AUTO koşusu, iki ayrı kusuru ve bunların birbirini nasıl beslediğini
gösterdi. Üçü de `QQQC.NASDAQ` · 1H/4H/1D · 15 iterasyon · sürekli mod.

## Koşu 1 — `f38273f2`: ölü uç bütçeyi yedi

llama-server 8080'de kapalıydı. 45 LLM çağrısının 45'i ~2,9 sn'de
`APIConnectionError` verdi, `total_output` 0 kaldı.

| ölçüm | değer |
|---|---|
| süre | 4 dk 51 sn |
| bitiş | `budget` — "token ceiling (250.000) reached at 252.459" |
| gerçek çıktı | 0 token |
| maliyet | $0,00 |

İki mekanizma birlikte öldürdü:

1. **Tahmini hata harcaması.** Yanıtsız çağrıya tahmini girdi yazılıyordu. Gerekçe
   geçerliydi (deadline'da child ölse de üretim sunucuda sürüyor ve faturalanıyor)
   ama "prompt gitti" varsayımına dayanır — bağlantı hiç kurulamadıysa yanlış.
2. **Kör tavan.** `BLIND_MAX_TOKENS = 250.000`, maliyet GÖRÜNMÜYORKEN geri düşülen
   sıkı tavan; maliyet görünürken `RUNAWAY_MAX_TOKENS = 2.000.000` geçerli. Hiçbir
   çağrı başarılı olmadığı için maliyet hiç gözlenmedi → koşu kör sayıldı → şişirilmiş
   tahminler 250k'yı 5 dakikada doldurdu.

Düzeltme (commit `4512f79`): muafiyet SOMUT istisna adına bakar, üst sınıfa değil —
her iki SDK'da da `APITimeoutError`, `APIConnectionError`'dan türer, `isinstance`
ile bakmak timeout'u da muaf tutar ve tahmini-harcamanın kapattığı deliği geri açar.
Bilinmeyen tip şüphede SAYILIR.

Sahada doğrulandı (`ed8ba569`): `APIConnectionError` → `usage=None`;
`InternalServerError` → tahmin yazıldı (istek sunucuya ULAŞTI, sunucu sonra düştü).

## Koşu 2 — `ed8ba569`: `-c 16384` yetmiyor

Uç ayaktaydı, bütçe sağlıklıydı, ama `composed` çağrılarının 8'de 7'si kesildi.
Sebep `max_tokens` DEĞİL, llama-server'ın bağlam penceresiydi — kesilen her
çağrıda `in + out` **tam olarak 16.384**:

| in | out | in+out |
|---:|---:|---:|
| 13.240 | 3.144 | 16.384 |
| 13.563 | 2.821 | 16.384 |
| 13.888 | 2.496 | 16.384 |
| 14.146 | 2.238 | 16.384 |
| 14.236 | 2.148 | 16.384 |

Kritik olan trend: `composed` promptu her turda ~140 token büyüyor (geçmiş
birikiyor), üretime kalan yer daraldıkça kesilme kaçınılmazlaşıyor. Kendiliğinden
düzelmez, **giderek kötüleşir**. `idea` yolu sağlamdı (7/8 ok) çünkü promptu 1-1,5k.

Bedeli: 1,29 saat, $6,11, 21 degraded, tur 1'in en iyi adayı bir `Random …`
fallback spec'i (skor -7,79, `passed: false`).

## Koşu 3 — `392287b2`: `-c 32768` + q8_0 KV

Bayraklar: `-ngl 99 -c 32768 -fa on --jinja --reasoning-format deepseek
--cache-type-k q8_0 --cache-type-v q8_0`.

**q8_0 KV cache bağlamı ikiye katlarken VRAM'i neredeyse hiç artırmadı**
(RTX 5080 16 GB: 15.266 → 15.349 MiB, 629 MiB boşta). fp16 KV ile 32k sığmazdı.

Aynı prompt boyunda doğrudan karşılaştırma:

| | prompt | üretim | in+out | durum |
|---|---:|---:|---:|---|
| 16k pencere | 13.240 | 3.144 | 16.384 | **truncated** |
| 32k pencere | 13.282 | 6.201 | 19.483 (%59) | **ok** |

Model bu prompt için ~6.200 token üretime ihtiyaç duyuyor; 16k pencere ona 3.144
bırakıyordu.

Koşu tamamlandı — 107 dakika, $6,99, bütçenin %38'i (764.772/2.000.000):

| | 16k koşusu | 32k koşusu |
|---|---|---|
| `composed` | 8 çağrı, **7 kesik** | **24 çağrı, 0 kesik** |
| toplam LLM | 100 ok / 5 hata | **102 ok / 0 hata** |
| degraded | 21 | **0** |
| `fallback_count` | — | **0** |

## Arama sonucu: alfa kapısı bağlayıcı

Koşu `winless_limit` ile kapandı (`winless_round_limit: 3`) — 5 adayın 5'i de
`short_circuit: multi_symbol` ile elendi, hepsi 0/2, skorlar -6,3 ile -7,0 arasında
yatay. Bu bir bozulma DEĞİL; kapı tasarlandığı gibi çalışıyor.

`backtest_robustness.py` çok-sembol kapısı kârı değil **alfayı** ölçüyor:

```python
positive = [r for r in valid if (r.get("excess_return_fraction") or 0) > 0]
```

`ADX-Filtered Breakout`'un tur 1 kaydı bunu somutlaştırıyor:

| sembol | pnl | sharpe | benchmark | excess |
|---|---:|---:|---:|---:|
| AAPL.NASDAQ | **+2.283 (+%22,8)** | 0,58 | +%48,6 | **-%25,8** |
| MSFT.NASDAQ | -1.106 (-%11,1) | -0,22 | +%23,3 | -%34,4 |

AAPL'de strateji para kazandı ve Sharpe'ı pozitifti; yine de "positive" sayılmadı,
çünkü al-tut'u %25,8 geriden takip etti. NASDAQ mega-cap'lerinin %23-49 yükseldiği
bir pencerede bu, breakout/trend ailesi için çok yüksek bir çıta.

Bir kaldıraç var: eşik `pass_rate >= 0.7` ama yalnız **2** sembol test ediliyor.
İki sembolle olası değerler 0 / %50 / %100, yani 0,7 pratikte **2/2 zorunlu**
demek ve ara bant ("⚠ Limited") tek sembollük gürültüyle belirleniyor. Havuzu
büyütmek kapıyı hem daha bilgilendirici hem istatistiksel olarak daha adil yapar.

## Yeni sınır nerede

Uygulama öğrenilmiş `max_tokens=16000` ile çağırıyor, yani bağlayıcı koşul
`prompt + 16.000 ≤ 32.768` → prompt **16,7k**'yı aşarsa duvar geri gelir. Kartta
yer kalmadığı için o noktada doğru cevap bağlamı büyütmek değil, promptu kısaltmak.
