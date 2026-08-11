---
title: NAU DeepR Dördüncü Tur — 2026-08-11
type: synthesis
sources:
  - sources/04_backtesting_docs.md
  - sources/06_concepts_docs_v1230.md
last_updated: 2026-08-11
summary: 557 ajanlı denetim; 3 kritik + 20 yüksek + kapı kalibrasyonu kapatıldı. Asıl bulgu, AUTO'nun kazanan bulamamasının veri değil eşik sorunu olduğunun deneysel kanıtı.
key_concepts:
  - parquet_data_catalog
  - index_backtest_via_equity_proxy
  - auto_kapi_ve_geri_bildirim
  - nau_performans_denetimi
  - us_equity_katalog_veri_butunlugu
---

# NAU DeepR Dördüncü Tur — 2026-08-11

Önceki turlar: [[nau_deepr_toplu_sertlestirme_2026_08]],
[[nau_deepr_ikinci_tur_2026_08_08]], [[nau_deepr_ucuncu_tur_2026_08_09]].

## Turu başlatan gözlem

AUTO koşuları üst üste `winless_limit` ile bitiyordu. İlk teşhis "veri bozuk"
yönündeydi ve gerçekten altı veri kusuru bulundu
([[us_equity_katalog_veri_butunlugu]]). Katalog düzeltilip **yeniden üretildi**,
sonra aynı arama tekrar koşuldu — ve yine kazanan çıkmadı:

| koşu 44cb54e2 (QQQC.NASDAQ, 3 TF, 3 tur) | |
|---|---|
| backtest | 12 |
| kârlı | 9 |
| en iyi | Sharpe 0,79 · +196.880 USD |
| **benchmark fazlası pozitif olan** | **0** |

Bu, turun en değerli çıktısı: **veriyi düzeltmek kapıyı açmadı, çünkü kapı
veriden değil kalibrasyondan bozuktu.** Sıralama yine de doğruydu — bozuk
veriyle gevşetilmiş bir kapıdan sahte bir kazanan geçerdi.

## Kapı yeniden kuruldu

Eski kural kümülatif buy&hold farkıydı; büyüklüğü tamamen veri penceresinin
uzunluğuna bağlıydı (22,7 yılda %2093, 1 yılda ~%20). Yerine:

* `annualized_alpha > 0` — yıllıklandırılmış, iki taraf da net (buy&hold
  gidiş-dönüş maliyeti düşülür, biliniyorsa temettü eklenir).
* risk-ayarlı üstünlük — stratejinin Calmar'ı buy&hold'unkini geçmeli.
* alan yoksa eski kümülatif kurala düşülür; hiçbiri yoksa `no_benchmark` ile
  **fail-closed**.

Aynı adayın yeni kapıdaki gerekçesi artık okunabilir: kümülatif −12,90 yerine
**yılda 4,3 puan geride**. Ayrıntı: [[auto_kapi_ve_geri_bildirim]].

Skor formülünde simetri: `k = n/(n+20)`, `base ≥ 0 → ×k`, `base < 0 → ÷k`.
Eskiden çarpan negatif tarafta cezayı azaltıyordu — sıralamada 27 işlemli
+244 USD, 135 işlemli +17.562 USD'yi geçiyordu. Aynı hatanın WFO ikizi de
düzeltildi.

## Motor hızlandı, parite bozulmadan

Pencere (`NAU_WINDOW=260`) ve matematik aynen korundu; kazanç ara liste ve
closure tahsislerinden geldi. Ölçüm ve reddedilen alternatifler:
[[nau_performans_denetimi]] "Dördüncü tur".

| | önce | sonra |
|---|---|---|
| `calc_adx` | 117 µs | 80 µs |
| `calc_stoch_rsi` | 131 µs | 83 µs |
| 40k bar, adx entry+exit | 9,68 s | 3,29 s |

Parite toleranssız `==` ile, git'ten alınmış referans implementasyona karşı,
641 kayan pencerede kanıtlandı. Kritik ayrıntı: CPython 3.12 `sum()` float'larda
Neumaier topluyor; Wilder tohumlarını elle `+=` ile değiştirmek son ULP'de sapıp
eşik/kesişim bloklarında **farklı sinyal** üretebilirdi.

## Kritik güvenlik

* `codegate`: `**`/`<<` denetimleri `BinOp` düğümüne bakıyordu, `AugAssign`'a
  değil — `x **= 999999` denetimsiz geçiyordu ve önizleme yolu sunucu
  sürecinin İÇİNDE exec ediyor. Denetim artık **operatöre** göre.
* `sandbox`: bellek tavanı ağır import'lardan ÖNCE kuruluyordu; 512 MB altında
  `agent → pandas → nautilus` zinciri import edilemiyor, çocuk ölüyor ve hata
  **kullanıcının bloğuna** atfediliyordu. Tavan import'tan sonraya alındı ve
  ölçülerek 2048 MB'a çekildi.
* Erişim kapısı: `NAU_ACCESS_TOKEN` pm2 ortamında tanımsızdı, yani cloudflared
  tüneli kapısız açıktı. Sır artık env ya da (yalnız `PM2_HOME` varken)
  `~/.nau_access_token` dosyasından okunuyor.

## Dürüstlük

`fallback_count` gerçek bozulmanın %38'ini sayıyordu; faz satırı her turda
"all iterations failed" diyordu (oysa 8/8 backtest başarıyla koşmuştu);
yanıtsız LLM çağrılarının girdisi deftere hiç geçmiyordu (250k tavan %7,5
aşılmıştı). Üçü de kapatıldı — ayrıntı [[auto_kapi_ve_geri_bildirim]].

## Sayılar

557 ajan · 3 kritik + 20 yüksek + 41 orta bulgu · ~500 yeni test ·
süit 1494 → 2106 · disk 12,1 GB → 1,9 GB · bağımlılık kilidi (98 paket,
kurulu ortamdan üretildi).

## Açık kalanlar

* 41 orta bulgunun bir kısmı (ajanlar oturum limitine takılıp yarıda kesildi).
* Mimari: `agent_backtest.py` 5227 satır; `sandbox.py` bir web route modülünü
  import ediyor (izolasyon katmanı → web katmanı, ters bağımlılık);
  `server.py` ↔ `web/routes` çift yönlü; ~45 ortam değişkeni 18 dosyada.
* `TestRunUnitsTimeout` kararsızlığı bütçe büyütülerek giderildi ama altındaki
  `parallel_exec.run_units` havuz-timeout yarışı incelenmedi.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[index_backtest_via_equity_proxy]]
<!-- BACKLINKS:END -->
