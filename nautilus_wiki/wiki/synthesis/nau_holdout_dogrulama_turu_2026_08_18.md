---
title: Mühürlü holdout düzeltmesinin doğrulaması (2026-08-18)
type: synthesis
summary: 2db813b'nin aritmetiği doğrulandı ama düzeltme eksik çıktı - span sadeleşiyor (kapı gizlice "ömür boyu >=134 işlem"e dönüşüyor), uyarı yeni rejimde yapısal olarak susuyor, sıralama ile mühür 5,7x ıraksıyor ve genişleyen mühür çok-sembol kanıt penceresinin tamamını yuttu.
sources: []
related:
  - wiki/synthesis/nau_auto_kosusu_755b7880_2026_08_17.md
  - wiki/synthesis/auto_kapi_ve_geri_bildirim.md
last_updated: 2026-08-18
---

# Mühürlü holdout düzeltmesinin doğrulaması

`2db813b` mühürlü pencereyi sabit takvimden örneklemin oranına çevirdi. Bu tur
o düzeltmeyi **commit mesajına güvenmeden** denetledi: dört bağımsız mercek
(aritmetik / kalan delikler / test kalitesi / tasarım) + her bulguya ayrı bir
çürütme ajanı. **32 bulgu onaylandı, 8'i çürütüldü.**

Kısa hüküm: **aritmetik doğru, düzeltme eksik, bir de regresyon getirdi.**

## Doğrulananlar (commit haklı)

Bağımsız yeniden hesapla: 1-DAY penceresi 41 → 862 bar (bant 849-888), gereken
giriş oranı %49 → %2,3. Sızıntı iddiası da tutuyor: mühür adaydan önce, veri
yüklenirken atılıyor ve oran kuralı adaydan bağımsız. Taban da korunmuş.
Çürütülen 8 iddia arasında "taban bozuldu", "her TF farklı pencere mühürlüyor",
"uçtan uca kapsam yok" vardı — üçü de tutmadı.

## 1) Span sadeleşiyor: kapı gizlice mutlak bir sayıya döndü

Pencere örneklemin oranıysa yıl birimi denklemden düşer:

```
beklenen_mühürlü_işlem = toplam_işlem × ORAN
geçme koşulu           ⇒ toplam_işlem ≥ EŞİK / ORAN = 20 / 0,15 = 133,3
```

Yani kapı artık **"tüm geçmişinde en az 134 işlem yapmış ol"** demek — örneklem
5 yıl da olsa 50 yıl da. Doğrulandı: 21,0 / 22,0 / 22,7 / 23,5 yılda kazananın
beklentisi **her seferinde 7,80**.

Pratik sonucu sert: **daha derin katalog bağlamak kapıyı asla açmaz** (taban
rejiminin üstünde). Operatörün en doğal refleksi hiçbir şey değiştirmiyor ve
bunu söyleyen bir satır yok.

Üstelik ayarlanamıyor da: kazananın geçmesi için oran `20/52 = 0,385` olmalıydı,
ama commit'in kendi testi eğitim verisinin >%80'ini şart koşuyor, yani oranı
%20'nin altına kilitliyor. **Düzeltme kendi test kısıtları içinde çalışır hâle
getirilemez.**

## 2) Uyarı, yakalaması gereken durumda yapısal olarak susuyor

`holdout_feasibility` `HOLDOUT_MIN_TRADES / n_bars > 1/3` bakıyor, yani yalnız
pencere **60 bardan azken** konuşuyor. Yeni 1-DAY penceresi ~857 bar → oran
%2,3 → sessiz. Fonksiyon adayın kendi hızını (%0,91) göremiyor, çünkü
aday-bağımsızlığı bir testle çivilenmiş.

En rahatsız edici ayrıntı: `tests/test_holdout_window_arithmetic.py:103`
**n=862 için sessizliği assert ediyor** — yeni pencerenin tam boyu. Emniyet ağı,
yeni rejimde hiç ateşlemeyecek biçimde kalibre edilmiş durumda. Uyarının değeri
ancak adayın ölçülen hızına bağlanırsa geri gelir.

`HOLDOUT_PLAUSIBLE_ENTRY_RATE = 1/3` de düzeltme ÖNCESİ rejime kalibre; 1-HOUR
ve 4-HOUR'da hiçbir örneklem uzunluğunda uyarı üretemiyor.

## 3) Sıralama kapısı ile mühür 5,7 kat ıraksıyor

`HOLDOUT_MIN_TRADES = 20`, kendi yorumunda *"ana sıralama kapısıyla aynı asgari
kanıt"* diye gerekçelendiriliyor. Ama `_MIN_TRADES = 20` **eğitim span'ı**,
holdout'unki **mühürlü span** üzerinden sayılıyor; ölçülen oran 862/4899 =
**0,176**.

| | |
|---|---|
| gerekçeyle tutarlı eşik | 20 × 0,176 = **3,5** işlem |
| yürürlükteki eşik | **20** işlem |
| mühürde 20 beklemek için gereken eğitim işlemi | **114** |
| sıralamanın içeri aldığı taban | **20** |

Sonuç: **20-113 eğitim-işlemi bandındaki her aday sıralamayı geçer ve mühürde
aritmetik olarak ölür.** Koşu 755b7880'in kazananı tam o bantta: 52. Commit
pencereyi düzeltirken eşleşmeyi kurmadı — teşhis ettiği birim hatası bir eksen
öteye taşındı.

## 4) REGRESYON: genişleyen mühür çok-sembol penceresini yuttu

Sızıntı argümanı birincil seri için doğru ama **peer penceresi taşınmadı**.
Çok-sembol testi her peer'ı kendi verisinin son `ms_days = 730` gününe kesiyor
(`auto/robustness.py:340`, `backtest_robustness.py:1516` ve `:133`). Mühür artık
1254 gün ve katalog peer'ları da aynı tarihte bitiyor:

| | eski (60 g) | yeni (1254 g) |
|---|---:|---:|
| mühürlü dönemin içine düşen çok-sembol kanıtı | ~%8 | **%100** |

Bu bilgilendirme amaçlı bir test değil: kesin ret verirse IS/OOS + WFO + MC'yi
kısa devre ettiriyor (`auto/robustness.py:370`) ve seçim skorunu 0,15-1,0
aralığında, yani **6,7 kata kadar** çarpıyor.

## 5) Kayıt ve ekran hâlâ 60 gün diyor

- `web/templates/agent_backtest.html:167` — koşu formunda, KARAR anında "60 gün"
  yazıyor; gerçek pencere 1254 gün, yani **20,7 kat** yanlış.
- `run_config.holdout_days = 60` — hiçbir koşunun kullanmadığı değer kaydediliyor
  (`winner_holdout.days` gerçek genişliği taşıyor, ama denetim defterinin özet
  alanı tabanı gösteriyor).
- Mühür yerindeki iki yorum ve `agent_result.html:64` de eski sayıyı tekrarlıyor.

## 6) Testler ayırt edici değil

| test | sorun |
|---|---|
| taban↔oran geçiş noktası | hiç test edilmemiş — 400↔2000 gün hata sessiz geçer |
| `HOLDOUT_PLAUSIBLE_ENTRY_RATE` | iki yandan da çivilenmemiş; 0,11↔0,45 serbest |
| `test_the_min_trades_threshold_is_untouched` | `HOLDOUT_MIN_TRADES=3` ile de GEÇİYOR — gevşemeyi tespit etmiyor |
| embargo/sızıntı iddiası | totoloji; `WF_EMBARGO_DAYS` tamamen silinse testler yeşil |
| `HOLDOUT_SAMPLE_FRACTION` | yalnız sihirli sayılarla; geçen bant 0,0749-0,1675 |

Ayrıca `AGENT_HOLDOUT_FRACTION` env'den **clamp'siz** okunuyor; ~1,0 mührü
tamamen kapatır ve tek koruma 200 barlık mutlak taban.

## Sıra önerisi

1. **Çok-sembol regresyonu** — tek gerçek geri gidiş; peer penceresi mühürle
   birlikte hareket etmeli ya da peer'lar mühür öncesine kesilmeli.
2. **Sıralama↔mühür eşleşmesi** — eşiği oransal yap (`20 × mühürlü/eğitim`),
   yoksa doğmadan ölen adaylar üretilmeye devam eder.
3. **Uyarıyı adayın ölçülen hızına bağla** — bugün sustuğu yer tam da gereken yer.
4. **Ekran/kayıttaki 60 gün** — karar anında yanlış sayı gösteriliyor.

Yöntem notu: bu turun kendi dersi, bulguları çürütmeye çalışan ikinci bir
geçişin işe yaradığı — 40 iddianın 8'i ayakta kalmadı ve ikisi commit'i haksız
yere suçluyordu.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_kapi_ve_geri_bildirim]]
- [[multi_symbol_generalization]]
- [[nau_auto_kosusu_755b7880_2026_08_17]]
<!-- BACKLINKS:END -->
