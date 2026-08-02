---
title: AUTO Mission Control kokpiti
type: synthesis
summary: Strategy Studio'nun AUTO sekmesi; uzun dikey akıştan tek ekranlık, kaydırmayan kokpite geçiş. Aynı ajan state'inin ikinci sunumu (?view=mission), eşleme web/mission.py'de izole ve testlerle kilitli.
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - sources/02_architecture_docs.md
key_concepts:
  - strategy_and_actor
  - backtesting_guide
  - crash_only_design
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/strategy_studio.md
last_updated: 2026-08-02
---

# AUTO Mission Control kokpiti

Strategy Studio'nun **AUTO** sekmesinin (otonom araştırma döngüsü) 2026-08-02'de
yeniden tasarlanmış hâli. Tasarım kaynağı repo içinde
`design_handoff_auto_mission_control/` (seçilen yön `1c`; reddedilen `1a` split
cockpit ve `1b` command bar bağlam için aynı pakette).

## Neden değişti

Eski ekran **tek uzun dikey akıştı**: büyük konfigürasyon formu → durum çubuğu →
timeline → sürekli büyüyen adım kartları listesi. İki somut kusuru vardı:

1. **Koşu durumu kaydırınca kayboluyordu.** Kullanıcı adım kartlarına bakmak için
   aşağı indiğinde "şu an ne yapılıyor / kaçıncı iterasyon" görünmez oluyordu —
   oysa 20+ dakika süren bir döngüde ekranda tutulması gereken tek şey buydu.
2. **Üretilen stratejiler hiç görünmüyordu.** Aday backtest'ler bir tabloda
   duruyordu ama "hangisi önde" sorusu ekranın hiçbir yerinde birinci sınıf
   değildi.

Redesign bu ikisini yapıya gömer: durum ve liderlik **sabit**, ayrıntı (konsol)
akar.

## Yerleşim sözleşmesi

Kokpit `position:fixed` bir uygulama değil, kabuğun içine oturan **sabit
yükseklikli** bir panel. Üç kolon:

| Bölge | İçerik | Değişmez |
|---|---|---|
| Sol ray | Salt-okunur BRIEF (symbol/category/timeframe/model/range/robustness/iterasyon/guidance) + BÜTÇE göstergeleri | Konfigürasyon burada **düzenlenmez** — yalnız özetlenir |
| Orta | İlerleme halkası + `ŞU AN` başlığı + 5 hücreli faz şeridi + konsol | Hero `flex:none`; faz şeridi **asla sarmalanmaz** (metin ellipsize olur); konsol tek esneyen çocuk, `min-height:130px` |
| Sağ ray | İterasyon/aday kartları (equity sparkline + PF/DD/trade), altta "Lideri kataloğa ekle" | Lider kart her zaman en üstte |

Konfigürasyon 420px'lik **brief slide-over**'ına taşındı (`✎ brief`); START/STOP
AUTO üst barında. Bu ayrım kasıtlı: koşu sırasında formun ekranda yer kaplaması
için hiçbir neden yok, ama tek tıkla erişilebilir olmalı.

**Yükseklik ölçülür, sabitlenmez.** Üstteki kabuk yüksekliği (topbar + content
padding + mode switch + AUTO barı) yoğunluk kurallarına ve dar ekrandaki
sarmalanmaya göre değişir; `mcFit()` kokpitin `getBoundingClientRect().top`
değerini okuyup `--mc-offset` olarak yayınlar, CSS `calc(100vh - var(--mc-offset))`
kullanır. Hardcode edilen bir sayı ilk yoğunluk değişikliğinde bozulurdu.

~900px altında sağ ray gizlenir: üç kolon minimumlarını aynı anda tutamaz ve
aday rayı, konsolda zaten bulunan bilginin özetidir — kaybı en ucuz olan odur.

## Backend: yeni endpoint yok

Kokpit **ikinci bir poll yolu açmaz**. `GET /agent/progress/{run_id}` sorgu
parametresiyle sunum seçer:

- `?view=mission` → `fragments/auto_mission.html` (kokpit),
- parametresiz → `fragments/agent_progress.html` (klasik dikey görünüm,
  `/agent` sayfası bunu kullanmaya devam eder).

`POST /agent/run` de aynı ayrımı `view` form alanıyla yapar. Aynı state, iki
sunum: bir koşu iki ekrandan izlendiğinde ikisi de aynı gerçeği gösterir ve
ayrışacak ikinci bir durum makinesi yoktur.

Ajan state'ine üç şey eklendi (koşu başlangıcında **anlık görüntü**, worker
sonradan değiştirmez):

- `brief` — BRIEF rayının kaynağı; kokpit request formunu yeniden türetmez,
- `started_at` + `max_hours` / `max_total_tokens` — BÜTÇE göstergeleri,
- her backtest satırında `equity` — sparkline için ~40 noktaya indirgenmiş eğri.

**Sparkline neden indirgenir:** ham equity eğrisi 100k+ nokta olabilir; 1 sn'lik
poll'da bu, saniyede megabaytlarca JSON demektir. İndirgeme hem route'ta
(depolamada) hem `mission.py`'de (savunma derinliği — session replay gibi başka
bir yerden kurulan state ham eğri taşıyabilir) uygulanır.

## Eşleme nerede yaşıyor: `web/mission.py`

`mission_view(state, ...)` ajan durumunu şablonun render ettiği düz sözlüğe
çevirir. Şablon **yalnız sunumdur** — hiçbir karar orada verilmez. Bu ayrımın
bedeli bir dosya, getirisi eşlemenin test edilebilir olması.

Eşlemedeki üç bilinçli karar:

**(1) Altı ajan fazı → beş kokpit hücresi.** `_PHASES` altı faz tutar (veri →
strateji → backtest → sıralama → robustness → tamamlandı), kokpit beş hücre
gösterir: **"Ranking" ROBUSTNESS'a katlanır.** Kullanıcı açısından "en iyiyi seç"
ve "seçtiğini doğrula" tek bir aşamadır; ayrı hücre, ekranda bir sütun daha
harcayıp hiçbir yeni karar bilgisi vermezdi.

**(2) Duraklatmada halka boşalır, sayaç kalır.** `stop_requested` geldiğinde
halka `—` ve 0 dolum gösterir (duran bir döngü ilerliyormuş gibi okunmamalı), ama
`İTERASYON x/N` sayacı döngünün nereye kadar geldiğini göstermeye devam eder.
Aktif aday kartı silinmez, `duraklatıldı` rozetiyle kalır — iterasyon iptal
edilmez, mevcut adım bitince durur.

**(3) Konsol etiketleri gerçek log satırlarından türer.** `_step_kind()`
sınıflandırıcısının kalıpları `agent_backtest.py`'deki `_add_step()` çağrı
yerlerinden çıkarıldı ve **en-özel-önce** sıralıdır: robustness satırları da
"backtest" kelimesini geçirir, blok üretimi de "strateji" geçirir. Sıra
anlamlıdır ve testte verbatim log örnekleriyle kilitlidir — sınıflandırıcı
tahminle değil, kanıtla çalışır.

## Doğrulama

İki katman, ikisi de tekrar koşulabilir:

- **`tests/test_auto_mission.py` (32 test)** — formatlayıcılar, faz katlaması,
  lider seçimi (hatalı satır lider olamaz), tur filtresi, kuyruk doldurma,
  duraklatma durumu, bütçe oranları (sınırsız → ince "canlı" dilim, sınırlı →
  orantılı ve 100'de kırpılı), konsol sınıflandırması ve **şablon sözleşmesi**
  (canlı koşu poll etmeli, biten etmemeli).
- **`scripts/check_auto_cockpit.py`** — uygulamayı süreç içinde boş bir portta
  ayağa kaldırır, `scripts/fake_auto_run.py` ile sentetik bir koşu enjekte eder
  (LLM tüketmeden RUNNING durumu) ve headless tarayıcıda tasarımın sert düzen
  kurallarını ölçer: kokpit taşmıyor, faz şeridi tek satır, konsol ≥130px,
  slide-over açılıp kapanıyor, sayfada JS hatası yok. 1440×900 ve tasarımın
  belirttiği en kötü hâl 924×540'ta koşar.

Bu ayrım kasıtlı: birim testler *eşlemenin* doğruluğunu, tarayıcı kontrolü
*yerleşimin* doğruluğunu kilitler. Birincisi CSS'i, ikincisi iş mantığını
yakalayamaz.

## Uygulamada çıkan iki tuzak

**Palet `.mc` üstünde tanımlanamaz.** Kokpitin renk değişkenleri önce `.mc`
kuralına yazılmıştı; brief slide-over ve AUTO üst barı `.mc`'nin **dışında**
render edilir (konfigürasyon formuna aitler) ve hiçbir token'ı miras almadılar —
"Uygula" butonu şeffaf arka planla çıktı. Değişkenler `:root`'a taşındı.

**`or now` falsy tuzağı.** `started_at` bir unix timestamp'tir ve testte/replay'de
meşru olarak `0.0` olabilir; `state.get("started_at") or now` bu durumda geçen
süreyi sıfırlıyordu. `is None` kontrolü ile düzeltildi — birim test bunu yakaladı.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
