---
title: Tear sheet overlay'i
type: synthesis
summary: Her backtest listesindeki satırın açtığı salt-okunur performans sayfası; dört farklı depo tek render modelinde birleşir ve yeniden koşu yapılmaz, çünkü metrikler ve equity eğrisi zaten saklıdır.
sources:
  - https://github.com/nautechsystems/nautilus_trader
  - sources/02_architecture_docs.md
key_concepts:
  - backtesting_guide
  - strategy_studio
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/strategy_studio.md
last_updated: 2026-08-17
---

# Tear sheet overlay'i

2026-08-02'de eklendi. İstek: *"her backtest üzerinde link olsun, bastığımda o
backtestin tear sheet'i ekranda açılsın, kapattığımda eski ekrana dönsün."*
Uygulama `GET /tearsheet` + `fragments/tearsheet.html` + `base.html`'deki global
overlay.

## Belirleyici bulgu: veri zaten diskteydi

Tasarımı belirleyen şey istek değil, verinin nerede durduğuydu.
`backtest_log.jsonl`'deki her kayıt tam `metrics` sözlüğünü **ve iki equity
eğrisini** taşıyor: `equity_curve_realized` (işlem başına adım eğrisi) ve
`equity_curve_mtm` (bar çözünürlüklü mark-to-market, `[[iso_ts, eq], …]`).
Ölçüm: tek bir kayıtta 509 ve 5051 nokta.

Yani tear sheet için stratejiyi **yeniden koşmaya gerek yok**. Bu, mevcut
`/reports/detail`'den ayrı bir uç nokta olmasının tek sebebi: o, işlem
gerekçelerini üretebilmek için spec'i sandbox'ta bilerek replay eder (~2-5 sn).
İkisini tek uç noktada birleştirmek, ucuz okumayı pahalı replay'in hızına
indirirdi.

## Dört depo, tek render modeli

Listeler dört ayrı depoya dayanıyor ve şemaları **eşit değil**:

| `src` | Depo | Besleyen ekranlar |
|---|---|---|
| `log&ts` | `backtest_log.jsonl` (+ rotasyon arşivi `.jsonl.1`) | Reports, Studio Run History, AUTO kokpiti, Backtest sonuç ekranı, Strategy Lab, Dashboard iterasyonları |
| `session&run_id&i` | `agent_sessions/<run_id>.jsonl` | Session Logs |
| `strategy&run_id` | `studio_runs` tablosu | Strategy Builder koşuları |
| `suggestion&id` | `ai_suggestions.trial_metrics` | Strategy Builder AI deneme backtest'leri |

İlk ikisi tam metrik + eğri taşır. Üçüncü/dördüncü, Strategy Builder'ın
bilinçli olarak dar tuttuğu `BacktestMetrics`'i taşır: yüzdeler (`12.5` = %12,5),
normalize eğri (1.0 başlangıç), **tarih yok**, maliyet kırılımı yok.

Karar — **birleşim al, eksiği gizle**:

> Kaydedilmemiş bir metrik tile olarak basılmaz.

Alternatif (her tile'ı basıp değeri `—` yapmak) ince kaynakta on altı em-dash'lik
bir ızgara üretirdi; bu, "ölçülmedi" ile "sıfır" arasındaki farkı da silerdi.
Aynı gerekçeyle çizilemeyen bölümün sebebi sayfanın altına `notes` olarak yazılır
("aylık getiri için nokta başına zaman damgası gerekir; bu kayıt eğriyi damgasız
saklıyor").

İkinci kural, ton: **drawdown ve maliyet asla \"iyi\" renklenmez**, profit factor
eşiği 1.0'dır (altı zarar). Renk bir yorum taşır; yorum yanlışsa sayı doğru olsa
bile ekran yalan söyler.

## Overlay olmak, sayfa olmamak

"Kapattığımda eski ekrana dönsün" şartı doğrudan mimariyi belirledi. Navigasyon
olsaydı altdaki ekranın scroll pozisyonu, HTMX yoklaması (AUTO kokpiti saniyede
bir yokluyor) ve Chart.js grafikleri yeniden kurulurdu. Overlay `base.html`'de
yaşar; ✕ / scrim / **Esc** / **tarayıcı Geri** kapatır — tek `pushState`,
`popstate`'te `back()` tekrarlanmaz, kapanışta Chart.js tutamakları `destroy()`
edilir (overlay bir sonraki koşu için yeniden kullanılıyor).

**Kritik ayrıntı — id izolasyonu.** Fragment'in her DOM id'si `tsh-` önekli ve
grafik tutamakları `window.__tshEq` / `__tshDd`. Overlay, canlı backtest sonuç
ekranının **üstünde** açılır; o ekran `#equity-data`, `#equity-single`, `#dd-single`
ve `window.__eq` sahibidir. İd ya da global paylaşmak, altta duran grafiği yok
ederdi — kullanıcı overlay'i kapattığında geri döndüğü ekran bozulmuş olurdu.
Kural testle kilitli: fragment `#equity-single` **içermemeli**. (Aynı kapsama
tuzağının CSS'teki hâli için `murat_obsidian` vault'unda
`global_css_kapsama_tuzagi` sayfası var.)

## Kimlik anahtarları

Her listenin satırını doğru kayda bağlamak, üç ayrı karar gerektirdi:

- **Log:** `web.shared.log_backtest()` artık yazdığı kaydın `ts`'ini
  **döndürür**. Kendi satırını tutan çağıranlar (AUTO döngüsü, Lab, manuel koşu,
  Dashboard loop'u) bunu saklayınca link birebir olur. Alternatif — sonradan
  (spec adı, TF, PnL) üçlüsüyle eşleştirmek — kırılgandı.
- **Manuel koşuda sıra:** log yazımı, sonuç snapshot'ından **öne** alındı.
  Snapshot önce yazılsaydı `_result_viewmodel` henüz `log_ts` görmezdi ve o
  koşunun kalıcı görünümü tek başına linksiz kalırdı.
- **Session:** iterasyon, dosyadaki `backtest_result` olaylarının **sırasıyla**
  adreslenir (`ev_i`). Şablon tabloyu skora göre sıraladığı için indeks
  sıralamadan **önce** damgalanmalı. Bunun kazancı: ts damgası yalnız yeni
  koşularda olduğu hâlde **eski oturum dosyaları da** bağlanabilir.

## Buy & hold (2026-08-14)

`stamp_buy_hold_benchmark` bir süredir her koşunun metriklerine benchmark
alanlarını damgalıyordu (diskteki son 43 log kaydının 39'unda var) ama tear
sheet o anahtarları düşürüyordu: okuyan kişi "Return −12,76%" görüp aynı
pencerede piyasanın +45% yaptığını hiç öğrenmiyordu. Return'ün yanına üç kutu
eklendi — **Buy & Hold** (nötr renk: benchmark bir sonuç değil cetvel), **vs Buy
& Hold** ve **Alpha (annual)**, farklar işaretli (`+0.40%`; işaretsiz bir fark
hangi tarafta olduğunu söylemez) — ve **Max Drawdown** kutusuna alt satır olarak
benchmark'ın düşüşü: buy&hold'u iki katı düşüşle geçmek geçmek değildir, iki
sayı aynı kutuda olunca bu aritmetik yapmadan görülür.

Notlarda bir maliyet-tabanı uyarısı var ve gerekli: kümülatif **Buy & Hold**
brüt kapanış-kapanış getirisi, **Return** ise simüle maliyetlere göre nettir —
ikisi tek tabanda değildir. Aynı tabandaki bacak `Alpha (annual)`. Temettü
verimi varsayılan 0 olduğu için not, fiyat serisi temettü ayarlı değilse
buy&hold'un gerçekte daha fazla getirdiğini söyler: fark uydurulmuyor,
sayılmadığı yazılıyor. Benchmark'ı olmayan kaynak (Strategy Builder) ne kutu ne
not kazanır — "olmayanı çizgiyle doldurma" davranışı ayrı testle korunuyor.

## Doğrulama

`tests/test_tearsheet.py` (24 test): render modelinin eğrileri KPI ızgarasından
çıkardığı, olmayan metriği tile yapmadığı, tarihsiz eğride aylık haritayı
kapatıp sebebini yazdığı; üç çözücünün gerçek kayıt şekilleriyle çalıştığı;
session indeksinin dosyadaki *satır* değil *backtest* sırası olduğu; fragment'in
kendi DOM id'lerini kullandığı. Canlı doğrulama: Reports'ta 48 link, Session
Logs `ad4933e4` sayfasında 7 link, gerçek bir kayıt açıldığında KPI'lar ve 5051
noktalı MTM eğrisi geldi. Süit 700 geçti / 1 atlandı.

## Blocks paneli (2026-08-17)

Tear sheet "bu koşu NE yaptı"yı sayılarla anlatıyordu; "hangi STRATEJİ koştu"
sorusunun cevabı yalnız başlıktaki addı — iki koşuyu karşılaştıran kişi
metriklere bakıp spec'i hatırlamak zorundaydı. `web.tearsheet.blocks_view`
artık spec'in bloklarını kart listesine çeviriyor ve şablon onları
`Strategy Builder · Canvas`'ın node diliyle basıyor: sol kenarda rol rengi,
üstte büyük harfli çip, altında kalın blok adı ve soluk parametre satırı,
aralarında `›` akış oku. Aynı nesne için iki ayrı görsel dil, okuyanı
eşleştirme işine sokar.

Gerçek bir kayıtta:

```
[DATA        ] QQQC.NASDAQ          4-HOUR
[ENTRY · LONG] macd_cross           fast=10 · slow=30 · direction=up
[ENTRY · LONG] agnt_e_25674e00_13   period=14 · adx_threshold=22
[EXIT        ] macd_cross           fast=10 · slow=30 · direction=down
[EXIT        ] momentum             lookback=20 · sign=negative
[RISK        ] Position & risk      size 1 · SL 3.0×ATR
```

Üç karar:

* **Sıra Canvas akışı, kaydın dizilişi değil** — kayıtta exit blokları entry'den
  önce gelebiliyor; panel `DATA → TREND → ENTRY → EXIT → RISK` diziyor. Panel bir
  AKIŞ anlatıyor, kaydın sıralama kazasını değil.
* **Parametre sırası kayıttaki sıra** — alfabetik dizmek aynı bloğu iki koşuda
  iki farklı biçimde gösterir ve kartlar karşılaştırılamaz hâle gelir. Dörtten
  sonrası `+N` ile kırpılır.
* **Bloğu olmayan kayıt boş panel göstermiyor**, sebebini `notes`'a yazıyor —
  sayfanın zaten yazılı kuralı.

KAYNAK KAPSAMI üç depoda farklı: `log` spec'i tam taşıyor (RISK kartı dahil),
`session` yalnız `spec_blocks` taşıyor (risk alanları kayıtta yok → RISK kartı
çıkmaz, uydurulmaz), `strategy` (Studio) blok listesi tutmuyor.
`strategy_studio.graph.to_graph` bu node modelini ZATEN üretiyor ama girdisi
`StrategyDefinition`, composer spec'i değil — iki ayrı veri modeli, aynı görsel
sözleşme; Studio'yu bağlamak ayrı bir iş.

CSS'te sarmalama bilinçli olarak yok: akış oku bir sıra anlatıyor, satır
atlayınca ok yanlış yeri gösterir. Dar ekranda yatay kaydırılır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_mission_control]]
- [[webapp_module_map]]
<!-- BACKLINKS:END -->
