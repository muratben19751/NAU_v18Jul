---
title: NAU — söz verip yapmayan yollar (2026-08-17, 12 düzeltme)
type: synthesis
summary: Bir DeepR raporundan doğrulanan 12 bulgunun üç turda kapatılması. Ortak imza - kod bir şeyi YAPTIĞINI söylüyor ama yapmıyor ve arada iz yok; raporun 4 maddesi ise bayat çıktı.
sources:
  - https://github.com/muratben19751/NAU_v18Jul
key_concepts:
  - auto_kapi_ve_geri_bildirim
  - strategy_studio
related:
  - wiki/synthesis/nau_bulgu_kapatma_turu_2026_08_17.md
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/strategy_studio.md
  - wiki/synthesis/nau_guvenlik_dayaniklilik_duzeltmeleri.md
last_updated: 2026-08-17
---

# NAU — söz verip yapmayan yollar (2026-08-17)

`2b3c392..9d6602f`, on iki commit, üç turda dörder. Girdi: 25 maddelik bir DeepR
raporu. Çıktı: 12 düzeltme + 51 regresyon testi. Raporun geri kalanı ya bayattı
ya bilinçli tasarımdı ya da ölçümsüzdü — ayıklama bu turun asıl işiydi.

## Ortak imza

On ikisinin hepsi aynı aileden: **kod bir şeyi yaptığını söylüyor, yapmıyor, ve
söylediği yer ile yapmadığı yer arasında hiçbir iz yok.** Hiçbiri istisna
atmıyordu; hiçbiri kırmızı test üretmiyordu. Bu yüzden her düzeltmenin testi
davranışı değil **vaadi** sınıyor.

## Tur 1 — sessiz ayar, ulaşılamaz eşik, doğrulanmayan girdi

| bulgu | ne yapıldı |
|---|---|
| `NAU_STUDIO_DB` ölü ayardı | `store.DB_PATH` artık `app_constants.studio_db_path()`'ten; kurucu varsayılanı çağrı anında çözülüyor |
| max-dd kapısı geçilemiyordu | `default_gate_min(objective)`: `max_dd → -20.0`. Karşılaştırma yönü DEĞİŞMEDİ, yanlış olan varsayılandı |
| blok rolü doğrulanmıyordu | `("entry","exit")` whitelist'i; desen aynı dosyada `role_hint` için zaten vardı |
| tamsayı sweep aralığı çöküyordu | `int()` yerine floor/ceil + açık işaret clamp'i |

En değerlisi ilkiydi ve rapor onu en düşük ciddiyetle etiketlemişti:
`tests/browser/conftest.py` "gerçek `studio.db`'ye ASLA dokunmamalı" diye söz
verip `NAU_STUDIO_DB` ayarlıyordu, store dinlemiyordu — süit **gerçek DB'ye
yazıyordu**. Kırık bir izolasyon sözü YEŞİL test üretir, kırmızı değil.

`value=1` için sweep aralığı `[1,1]` oluyordu: optimizer yalnız mevcut değeri
deniyor, arıyormuş gibi görünüyordu. `value=-1` için üst uç `+2`'ye taşıyordu.

## Tur 2 — sınırsız girdi, sanitize edilmeyen çıktı, kilitsiz yol

| bulgu | ne yapıldı |
|---|---|
| aşırı geniş tarih aralığı | `app_constants.MAX_DATE_RANGE_DAYS = 36_525`, iki yerde uygulanıyor (HTTP sınırı + yükleyicinin kendisi) |
| `render_md` ham HTML geçiriyordu | escape-önce + `href`/`src` şema süzgeci (ağaç üzerinde); **varsayılan güvenli**, repo wiki'si `trusted=True` |
| optimize eşzamanlı sweep açıyordu | `_job_in_flight` çıkarıldı, backtest ve optimize aynı korumayı paylaşıyor |
| AI önerisi sahipsizdi | `_suggestion_of` — `sid` global, `strategy_id` yoldan; ikisi karşılaştırılmıyordu |

`0001-01-01`–`9999-12-31` biçim ve sıra kontrolünden geçiyor, sonra yükleyici
3.652.058 `date` nesnesi kurup `date.max + 1` adımında taşıyordu.

XSS düzeltmesinde kütüphane yoktu (`bleach`/`nh3` kurulu değil) ve elle süzgeç
yazmak tuzak. Çözüm süzmek değil **üretmemek**: kaynak markdown'a girmeden
escape ediliyor, markdown sözdizimi HTML karakteri içermediği için yapı
bozulmuyor. Ayrıca `except ImportError` yedek yolu (`f"<pre>{txt}</pre>"`) da
bir sink'ti.

## Tur 3 — ölçülen strateji ekrandakinden farklı

| bulgu | ne yapıldı |
|---|---|
| trend filtresi arızada sessizce düşüyordu | fail-closed; ama **aynı-TF atlaması meşru**, `_same_tf` ile ayrıldı |
| durdurulan deployment arka planda başlıyordu | `_abandoned` işareti; kayıt ile iptal kontrolü aynı kilidin altında |
| kapı sentetik metriği kanıt sayıyordu | `studio_runs.engine` sütunu + `engine_name()`; kapı sayıya bakmadan önce kökene bakıyor |
| kazanan anlatısı iki fatura çıkarıyordu | tek-uçuş bayrağı, kontrolle aynı kilit altında; bayrak aynı zamanda jeton |

Trend filtresi en tehlikelisiydi: sonuç "başarılı" olarak ve **trend filtreli
spec adıyla** kaydedilip aday seçimine giriyordu. Dört yoldan varılan tek bir
"yokluk" durumu vardı ve dördü birden reddedilemezdi — `trend_interval`
varsayılanı `"60"`, 1 saatlik koşuda ana TF ile çakışıyor, yani o yol en sık
kullanılanı. Ayrım yapılmadan uygulanan fail-closed çalışan koşuları kırardı.

Motor provenance'ında "SİMÜLE" rozeti 2026-08-08'den beri vardı ama kapı onu
okuyamıyordu: **rozet bir görüntü, kapının ihtiyacı olan bir kayıt.** Kapı
kapanmadı, dürüstleşti — sentetik sayıyla deploy etmek isteyen kapıyı
kapatabilir, o zaman ortada iddia da olmaz.

## Raporun kendisi hakkında öğrenilen

Raporun verdiği ham sayılar istisnasız tuttu (`agent_backtest.py` 5601 satır,
`_MAX_RESULT_SESSIONS` 500, `DEFAULT_GATE_DSR` 0.8) — kodu gerçekten okumuştu.
Buna rağmen dört madde kullanılamazdı:

- **düzeltilmiş**: sandbox bellek tavanı aynı gün commit'lenmişti
- **konusu silinmiş**: `loop_runner.py` artık yok
- **kısmen azaltılmış**: stub rozeti zaten vardı, gerçek boşluk çok daha dardı
- **bilinçli tasarım**: route'lar arası import, dev modül

Severity sıralaması raporun en zayıf çıktısıydı; işaret ettiği SATIR en
güçlüsü. Sekiz "YÜKSEK" performans maddesinin hiçbirinde ölçüm yoktu ve
hiçbirine dokunulmadı — bu deponun çıtası `80f20c8`'in commit mesajındaki
ölçüm tablosu.

## Doğrulama

51 yeni test, üç dosyada (`test_deepr_fixes_2026_08_17*.py`). Her turda
düzeltmeler `git stash` ile geri alınıp testler koşuldu: 12/12, 15/23, 12/16
kırmızıya döndü. Kalanlar değişmezlik kontrolü (varsayılan taşınmadı, meşru
yollar hâlâ geçiyor).

İlk yazdığım XSS doğrulaması **yanlıştı**: substring arıyordu ve kaçırılmış
metindeki `onerror` kelimesini tehdit sanıp altı vektörün üçünü hatalı
işaretledi. `HTMLParser` ile ayrıştırmaya çevrildi. Bir güvenlik iddiası
saldırganın temsiliyle değil, **tarayıcının temsiliyle** doğrulanır.

Aynı şekilde ilk yazdığım üç test kendi mantığını taklit ediyordu, kodu
sınamıyordu — gerçek `run_composed_backtest`, gerçek `PaperRunner` ve gerçek
`_AGENT_LOCK` çağıracak şekilde yeniden yazıldı.

## Açık kalanlar

- ~~Holdout aritmetiği kırık~~ → **kapandı** (`2db813b`): pencere sabit takvim
  yerine örneklemin oranı (`max(60 gün, %15 × süre)`), ulaşılabilirlik mühür
  anında uyarılıyor. 1-DAY'de 41 → 862 bar, gereken sıklık %49 → %2. Eşik
  oynatılmadı; ölçüm [[nau_auto_kosusu_755b7880_2026_08_17]]'de.
- Performans bölümünün sekiz maddesi: ölçülmeden dokunulmayacak.
- Auth fail-open ve CSRF/DNS-rebinding: tehdit modeli yazılmadan ciddiyetleri
  ölçeksiz.

İlgili: [[nau_bulgu_kapatma_turu_2026_08_17]] · [[strategy_studio]] ·
[[webapp_module_map]] · [[nau_guvenlik_dayaniklilik_duzeltmeleri]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_bulgu_kapatma_turu_2026_08_17]]
<!-- BACKLINKS:END -->
