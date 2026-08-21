---
title: AUTO'nun kapısı ve geri bildirimi — neyi ölçüyor, modele ne söylüyor
type: synthesis
summary: WFO kapısı pencere başına yeniden optimize edilmiş varyantı sertifikalıyordu, kataloğa yazılan ise sabit spec'ti; ayrıca modele giden geçmişte zaman dilimi, drawdown ve komisyon yoktu. 2026-08-04 denetimi ve düzeltmeleri.
key_concepts:
  - auto_mission_control
  - auto_arama_ekonomisi
sources:
  - sources/08_hibrit_kosu_olcumleri_2026_08_16.md
  - sources/09_baglam_ve_butce_olcumu_2026_08_16.md
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_arama_ekonomisi.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-20
---

# AUTO'nun kapısı ve geri bildirimi

2026-08-05 canlı üretim takip denetimi ve kapıların ikinci sertleştirme turu için
[[auto_360_canli_review_iyilestirmeleri]] sayfasına bakın.

2026-08-04'te canlı bir AUTO koşusu (`1376c812`, QQQ.NASDAQ, 4 TF, relaxed,
sürekli mod) izlenerek yapılan denetim. [[auto_arama_ekonomisi]] aramanın
*maliyet* tarafını ele alıyor; bu sayfa *karar* tarafını.

## 1. Kapı, kaydedilmeyen bir artefaktı sertifikalıyordu

`run_walk_forward` her pencerede parametreleri GA ile eğitim diliminde yeniden
fit eder ve **iki** OOS sonucu üretir:

| alan | ne ölçer | kim kullanır (eski) |
|---|---|---|
| `test_metrics` | pencere başına YENİDEN OPTİMİZE edilmiş spec | **kapı** |
| `test_metrics_naive` | değişmemiş spec — kataloğa yazılan | hiç kimse |

`_robustness_passed` birinciyi okuyordu. Yani sertifikayı "her 3 ayda bir
yeniden kalibre edilirse" iyi olan bir şey alıyordu; `append_to_catalog` ise
hiç kalibre edilmeyeni yazıyordu.

Ölçüm (63 geçerli pencere, cezalı OOS Sharpe = `mean − 0.5·std`, geçme eşiği
`> 0`):

```
ADX Slope ATR Squeeze   optimize −0.069   |  naive −0.896
ADX Rising ATR Squeeze  optimize −0.768   |  naive −0.569
RSI ADX ATR Uyumu       optimize −1.209   |  naive −0.841
```

İlk satır kritik: kapı eşiğe 0,07 uzaktaydı, deploy edilecek şey 0,90.

**Düzeltme** — `_wfo_test(w)` tek bir yerden karar serisini seçer
(`test_metrics_naive`, yoksa `test_metrics`); `_robustness_passed`'ın pencere
geçerliliği, cezalı Sharpe'ı ve pozitif-oran yedeği artık bunu okur.
`backtest_robustness.wfo_aggregate` `oos_sharpe_naive_penalized` alanını üretir.
Ekrandaki `wf_pass` sayısı da aynı seriden gelir — kapı ile gösterge
çelişmesin. Optimize edilmiş seri **silinmedi**: "yeniden fit etmek yardım etti
mi?" teşhisi olarak kalıyor ve adım logunda ayrıca yazılıyor.

**Geriye uyum:** payload'da naive seri hiç yoksa (eski koşular; ya da spec'te
optimize edilebilir sayısal parametre olmadığı için `space` boş kalmışsa —
o durumda `test_metrics` zaten optimize edilmemiş koşudur) eski toplam
kullanılır. Naive seri VARSA optimize edilmişe düşülmez.

## 2. Modele giden geri bildirim üç şeye kördü

`_summarize_composed_history` satırı şuydu:

```
· composed:X [rsi+adx] pnl=-8514.77 sharpe=-7.21 trades=4283 winrate=0.306
```

Eksik olanlar ve her birinin ölçülen bedeli:

- **Zaman dilimi.** TF, spec üretildikten SONRA round-robin ile atanıyordu
  (`intervals[i % len(intervals)]`), yani model periyotlarını hangi bara göre
  yazacağını bilmiyordu. Sonuç: 1-DAY iterasyonu **1 işlem** açtı ve `<20`
  kapısında elendi — fikir hiç sınanmadı, yalnız uyumsuzluk ölçüldü.
- **`max_dd`.** Kullanıcının brief'i birebir "minimum dd" diyordu; drawdown
  modele hiç gösterilmiyordu.
- **`commission_total`.** 15-DK adayında komisyon 8.566 $, brüt kâr 52 $ →
  net −%85. Model yalnız net PnL'i görüyor, bunu "kötü strateji" diye okuyor;
  doğru okuma "çok sık işlem".

**Düzeltme** — satır `tf=… max_dd=… commission=…` taşıyor, altına nasıl
okunacağını söyleyen bir yönerge eklendi; iterasyon sonucuna `bars_info`
damgalanıyor (TF oradan geliyor). Ayrıca `propose_composed_strategy` ve
`_propose_agent_strategy_idea` artık `timeframe=` alıyor: round-robin sırası
zaten çağıran tarafta belli olduğu için spec **hedef bara göre** isteniyor
(`_timeframe_line`). Dış piyasa bağlamı da tek TF adı taşıyor — dört TF'lik
liste değil.

## 3. Yan düzeltmeler

- `profit_factor` artık **işlem bazlı** (kapanan pozisyonların brüt kâr/zarar
  oranı). Önceki değer Nautilus'un *getiri serisi* istatistiğiydi: aynı koşuda
  PF 20,27 ile %36 kazanma oranı ve 0,19 Sharpe yan yana duruyordu. Eski değer
  `profit_factor_returns` olarak korunuyor. (BacktestNode yolunda işlem serisi
  yok; orada iki alan da getiri serisini taşır — runner'lar arası
  karşılaştırılmaz.)
- Oturum logu: WFO pencereleri adli çekirdeğe indirgendi
  (`_compact_wfo_windows`) ve ilerleme sayaçları (`… 550/768 completed`,
  `window N selected:`) log'a yazılmıyor — canlı konsolda kalıyor. Ölçüm:
  `wfo_windows` 103,4 MB → 4,66 MB (56 olay, %95), step olayları 5,1 MB düştü.
- `_sealed_holdout_stats` sabit `10_000.0` yerine `_starting_cash()` kullanıyor.
- Custom blok üretimindeki yazı-tura `Random(f"{run_id}:{iter}")` ile
  tohumlandı — koşu yeniden üretilebilir.
- Tavansız sürekli mod artık koşunun kendi logunda uyarı satırı yazıyor
  (tek fren: stop, 25 kazanansız tur, 3 aynı hata).

## 4. Doğrulama koşusu (3cad3325) ve orada çıkan dört kusur

Düzeltmelerden sonra aynı brief strict modda koşuldu. Ölçülen etki: 1-DAY
iterasyonu 1 işlemden **22 işleme** çıktı (aday havuzu 3/4 → 4/4);
`profit_factor` 20,05 → 1,235 (win_rate %39,6 ile artık tutarlı); robustness
olayı 329 KB → 96 KB; step olayı robustness başına ~595 → ~137. Koşu **iki
kazanan** üretti (turlar 2 ve 4) — bu kod tabanında ilk kez.

Ama o iki kazananı incelerken dört kusur daha çıktı:

**a) Custom blok adı turu içermiyordu — sertifikalanan strateji üzerine
yazılıyordu.** `agnt_{e|x}_{run_id}_{iter}` sürekli modda her tur aynı adı
üretiyor, `save_custom` son-yazan-kazanır. 7 turluk koşuda
`agnt_e_3cad3325_1` **7 farklı kodla** yazıldı; 2. ve 4. turda kataloğa giren
kazananlar bu adı referans ettiği için koşu bittiğinde ikisi de 7. turun
mantığını çalıştırıyordu. Ad artık `agnt_e_{run_id}_r{round}_{iter}`;
`custom_block_store.save_custom` de var olan bir adı FARKLI kodla ezerken
uyarı yazıyor (reddetmiyor — meşru yeniden kaydetme var).

**b) `— (yetersiz veri)` üç ayrı durumu tek etikete yıkıyordu.** Üretim koşulu
`not in_sharpe or in_sharpe <= 0 or oos_sharpe is None`; yani "ölçülemedi" ile
"in-sample kaybediyor" aynı dizgeyi veriyordu. Kapı bunu **failed** sayarken
aynı işareti multi-symbol'da **skip** sayıyordu. Artık üç ayrı etiket:
`— (ölçülemedi: …)` → kapı ATLAR, `✗ IS negatif (in-sample kenar yok)` → kapı
DÜŞÜRÜR, normal oran → `✓ Robust` / `⚠ Caution` / `✗ Overfitting suspected`.

**c) Sıfıra yakın payda "✓ Robust" üretiyordu.** IS Sharpe 0,0037 (55 işlemde
+1,10 dolar) → oran 54,18 → "Robust". Yeni `IS_SHARPE_MIN` (0,05,
`NAUTILUS_IS_SHARPE_MIN`) altında oran hiç kurulmuyor, kriter "ölçülemedi"
diyor. Kenarı olmayan bir strateji artık kenarı olmadığı için sağlam
sayılmıyor.

**d) Mühürlü holdout 1 işlemle "ölçüldü" sayılıyordu.** Tur 2 kazananının
holdout'u n=1 (Sharpe `None`, çünkü standart sapma iki gözlem ister) ama
`measured=True` dönüyordu. Yeni `HOLDOUT_MIN_TRADES=2` eşiği altında bayrak
False ve adım logu kaç giriş olduğunu yazıyor.

Ayrıca `backtest_result` olayı, robustness düzeltildikten sonra log'un en ağır
kalemi hâline gelmişti (~301 KB/olay: `equity_curve` + `equity_dates` ham).
`_thin_pair` ikisini **aynı indekslerle** 400 noktaya indiriyor — ayrı ayrı
seyreltmek değer/tarih hizasını sessizce bozardı.

## Para doğruluğu: iki sessiz çarpıtma (2026-08-11)

**Komisyon kalem bazlı sıfırlanıyordu.** `backtest._metrics`'in dıştaki komisyon
handler'ı loglanır hâle getirilmişti, ama içteki ikisi hâlâ sessizdi (liste
yolunda `except: pass`, skaler yolda `except: return 0.0`) — ve iç handler
hatayı yuttuğu için dıştaki LOGLAYAN handler hiç tetiklenmiyordu. Beş komisyon
kaleminin ikisi ayrıştırılamayınca toplam sessizce eksik çıkıyor, **net P&L
olduğundan iyi görünüyordu**; kapı kalibrasyonunun tamamı net/brüt tutarlılığı
üzerine kurulu olduğu için bu doğrudan karar bozan bir hatadır. Ayrıştırılamayan
kalem artık 0 sayılmıyor: sayılıyor (`commission_unparsed`), toplam alt sınır
olarak işaretleniyor (`commission_total_is_partial`) ve uyarı loglanıyor.
Gerçekten "komisyon yok" demek olan değerler (None/NaN/boş dize/boş liste) hata
sayılmaz — aksi hâlde bayrak her temiz koşuda yanar ve hiçbir şey anlatmazdı.

**WFO güven sönümlemesi negatifte ters çalışıyordu.** `wfo_optimizer.objective_value`
skoru `val *= n/(n+20)` ile sönümlüyordu; çarpan 0<k<1 olduğu için NEGATİF
skorlarda etki tersine dönüyor, değeri sıfıra yaklaştırarak İYİLEŞTİRİYORDU. İki
aday da fold başına sharpe=-1.0 üretse 5 işlemli aday -0.20, 200 işlemli aday
-0.909 alıyor ve GA turnuvası yüksek skoru seçtiği için **az işlemli kaybeden
kazanıyordu** — düşen piyasa dilimlerinde ve erken jenerasyonlarda tipik durum.
Duruş `web/routes/agent_backtest.py::_score`'daki ikiziyle birleştirildi: pozitif
skor `× k`, negatif skor `÷ k`. Az örneklem her iki yönde de "emin değiliz"
demektir, "daha az kötü" değil.

## Kalan açık uç

Round-robin ile `n_iterations == len(intervals)` seçilirse her strateji **tek**
TF'de bir kez sınanır ve sıralama (strateji × TF) çiftlerini tek listede
karşılaştırır. Artık en azından spec hedef TF bilinerek üretiliyor, ama bir
fikrin mi yoksa zaman diliminin mi elendiğini ayırmak için `n_iterations`'ı TF
sayısının katı seçmek gerekir.

## Benchmark kapısı risk-ayarlıya çevrildi (2026-08-15, `AGENT_BENCHMARK_GATE`)

Kapı "buy&hold'u MUTLAK getiride geç" diyordu ve bu, stratejiyi değil
ENSTRÜMANI eliyordu. Koşu `1fa9870e`'nin en iyi adayı Calmar'da buy&hold'u
GEÇİYORDU (0,292 vs 0,269) ama yıllık alfası −%6,8 olduğu için elendi — QQQ
22,7 yılda yılda %14,5 yapmış ve long-only bir strateji piyasadan zaman zaman
çıktığı için mutlak getiride kaybediyor.

Pencereyi kısaltmak kurtarmıyor; ölçüldü: QQQ'nun HER penceresinde buy&hold CAGR
%14-24 arası, 3 yıllıkta %24,2 ile daha da zor. Mevcut 22,7 yıllık pencere zaten
en yumuşaklardan biri.

`AGENT_BENCHMARK_GATE` (varsayılan `risk_adjusted`): asıl ölçü **Calmar
üstünlüğü**, alfanın pozitif olması şart değil, ama **CAGR > 0 tabanı** var —
para kaybeden bir strateji düşüşü küçük diye geçemez. Calmar ölçülemezse eski
mutlak kurala düşülür (ölçülemeyen üstünlük üstünlük sayılmaz). `absolute` ile
karar geri alınabilir.

Mühürlü holdout kapısı da AYNI ölçüye bağlandı: ikisi ıraksarsa bir aday
sıralamayı geçip yayımda takılır ve kullanıcı "buy&hold'u geçti mi" sorusuna iki
farklı cevap görür.

**Kapıların birbiriyle çelişebileceği ikinci eksen: FREKANS** (2026-08-17,
`2db813b`). Buradaki maliyet-ayarlı ölçü düşük frekansı ödüllendiriyor — aynı
koşuda 1.223 işlemli aday %1,2 CAGR alırken 52 işlemli aday %9,4 aldı. Mühürlü
holdout ise sayım cinsinden bir eşik istiyordu (`HOLDOUT_MIN_TRADES=20`) ve
penceresi sabit takvimdeydi (60 gün). İkisinin birimi farklı olduğu için
aralarındaki dönüşüm katsayısı — işlem hızı — gizli bir serbest değişkendi, ve
bu kapı onu aktif olarak küçültüyordu. Sonuç: sistem aradığı profili üretip son
kapıda kendi eliyle eliyordu (1-DAY'de 41 barlık pencerede 20 giriş = barların
%49'u). Pencere örneklemin oranına çevrildi; eşik oynatılmadı. Ölçüm
[[nau_auto_kosusu_755b7880_2026_08_17]]'de.

Bu eksen KAPANMADI, taşındı (doğrulama 2026-08-18). Pencere oranlı yapılınca
span sadeleşiyor ve mühür kapısı fiilen "tüm geçmişte ≥ `HOLDOUT_MIN_TRADES` /
`HOLDOUT_SAMPLE_FRACTION` = 134 işlem" oluyor. Sıralama kapısı ise aynı 20'yi
**eğitim span'ında** sayıyor; ölçülen oran 0,176, yani 20-113 eğitim-işlemi
bandındaki her aday sıralamayı geçip mühürde aritmetik olarak ölüyor — koşunun
kazananı (52) tam o bantta. Eşiğin de pencereyle ölçeklenmesi gerekirdi:
`20 × mühürlü/eğitim ≈ 3,5`. Ayrıntı: [[nau_holdout_dogrulama_turu_2026_08_18]].

Genel kural: iki kapı aynı adayı farklı BİRİMLERDE ölçüyorsa, aralarındaki
dönüşüm katsayısını adlandır ve boru hattının onu bir yöne itip itmediğini sor.

### Kapının seçtiği strateji SINIFI (üç bağımsız koşu, tutarlı)

| aday | CAGR (piyasa %14,6) | MaxDD (piyasa −%54) | Calmar (piyasa 0,27) |
|---|---|---|---|
| `Williams %R Reclaim` [1H] | %8,63 | −%27,3 | 0,316 |
| `ADX ATR Trend Edge` [4H] | %8,63 | −%24,6 | 0,350 |
| `DMI ATR Regression` [4H] | %5,20 | −%17,7 | 0,294 |

Üçü de piyasadan AZ kazanıp düşüşü yarıya/üçte bire indiriyor. Kapı "piyasayı
yenen" değil "sarsıntısı düşük" bir sınıf seçiyor — kabul edilen takas bu.
`5e89d42a`'da 27 adayın 2'si geçti (%7,4), yani gevşetme lastik damgaya
dönüşmedi. Testler: `tests/test_benchmark_gate_is_risk_adjusted.py`.

## Çok-sembol kapısı alfa ölçüyor — ve 2 sembolde eşik 2/2 demek (2026-08-16)

Koşu `392287b2` (`QQQC.NASDAQ`, hint "adx") `winless_limit` ile kapandı: 5 adayın
5'i de `short_circuit: multi_symbol` ile, hepsi **0/2**, skorlar -6,3 ile -7,0
arasında yatay. Altyapıda tek bir kusur yoktu (102 LLM çağrısı, 0 hata, 0 kesilme,
`fallback_count: 0`), yani sonuç aramanın kendisine ait.

Kapı kârı değil **alfayı** sayıyor (`backtest_robustness.py`):

```python
positive = [r for r in valid if (r.get("excess_return_fraction") or 0) > 0]
```

Bu ayrım tur 1'in en iyi adayında somut: AAPL.NASDAQ'ta strateji **+%22,8 kazandı**,
Sharpe 0,58, 35 işlem — ama al-tut %48,6 yaptığı için excess -%25,8 ve "positive"
sayılmadı. MSFT'de zaten -%11,1. Mega-cap'lerin %23-49 yükseldiği bir pencerede
breakout/trend ailesinden alfa istemek çok yüksek bir çıta; kapı bunu bilerek
istiyor (bkz. yukarıdaki risk-ayarlı kapı tartışması — orada kabul edilen takas
"piyasayı yenen değil sarsıntısı düşük" idi, burada ölçüt yine excess).

Asıl gözden geçirilecek yer eşiğin ÇÖZÜNÜRLÜĞÜ: `pass_rate >= 0.7` ama yalnız **2**
sembol test ediliyor. İki sembolle olası değerler 0 / %50 / %100, yani 0,7 pratikte
**2/2 zorunlu** demek ve ara bant ("⚠ Limited") tek sembollük gürültüyle belirleniyor.
Tek zayıf akran adayı komple eliyor. Ölçüm: [[09_baglam_ve_butce_olcumu_2026_08_16]].

**Düzeltildi — ve altından sessiz bir kusur çıktı.** Havuz 5'e çıkarılmak istenince
sınırın (`[:3]`) bağlayıcı olmadığı görüldü: uygun peer sayısı zaten 2'ydi. Sebep
`EXTERNAL_PEER_BASKET`'in venue ekiydi — sepet `SPY.ARCA` / `IWM.ARCA` yazıyordu
(piyasa gerçeği doğru, ikisi de NYSE Arca'da listeli) ama ingest bu kutuda 16
enstrümanın 16'sını `.NASDAQ` damgasıyla yazmış. Eşleşmeyen id `_external_bar_dir`
filtresinden **sessizce** düşüyordu: beş peer'lık sepet fiilen üçle, QQQC koşusunda
ikiyle karar veriyordu ve hiçbir yerde uyarı yoktu.

Üç değişiklik: (1) `resolve_peer_ids` peer'ı venue'ya değil **bare ticker**'a göre
katalogla eşliyor — sabit venue yazmak kırılgan, katalog değişince aynı sessiz düşme
tekrarlar; (2) sepet 7 girdiye çıktı (3 endeks ETF + 4 mega-cap) ki QQQC gibi üç
ticker'ı birden dışlayan dikilmiş bir seride bile 5 peer kalsın;
(3) `PEER_SAMPLE_SIZE = 5`, ve havuz hedefin altında kalırsa koşu logu bunu **yazıyor**
— sessiz daralma bu kapının 5 peer sanılırken 2 ile karar vermesine yol açmıştı.

Etki: QQQC.NASDAQ için test edilen peer 2 → **5** (SPY, IWM, AAPL, MSFT, NVDA).
Eşik değişmedi; 4/5 geçer, 3/5 "⚠ Limited" olur — kapı aynı sertlikte ama kararı
artık tek bir zayıf akrana bağlı değil. Testler:
`tests/test_multi_symbol_generalization.py`.

**Geniş örneklem sonucu çürütmedi, sebebini değiştirdi.** 5 peer'lı koşu
(`38bdfeff`, EMA/MACD ailesi — dünkü ADX'ten farklı) yine `winless_limit` ile
kapandı ve dört adayın dördünde de tablo aynıydı: **ortalama Sharpe pozitif
(+0,21 … +0,59), üstünlük 0/5.** Yani sorun ne stratejilerin kalitesi ne dar
örneklem; ölçütün kendisi.

Peer başına ölçülen rakamlar bunu somutlaştırıyor (tur 1, `MACD EMA RSI`):

| sembol | pnl | sharpe | işlem | benchmark | excess |
|---|---:|---:|---:|---:|---:|
| SPY | +268 | 0,27 | 10 | +%48,8 | -%46,1 |
| IWM | +1.202 | 0,69 | 8 | +%48,2 | -%36,1 |
| AAPL | +355 | 0,21 | 7 | +%48,6 | -%45,1 |
| MSFT | -684 | -0,75 | 4 | +%23,3 | -%30,2 |
| NVDA | -317 | -0,03 | 11 | +%117,9 | -%121,1 |

Üç sembolde para kazanılmış ve Sharpe pozitif; hepsi sıfır yazılmış. Bu, ana
kapının 2026-08-15'te **tam olarak bu gerekçeyle** terk ettiği ölçüt.

## Çok-sembol kapısı da risk-ayarlı (2026-08-16)

Kullanıcı kararı. `peer_is_superior` artık ana kapıyla aynı kuralı uyguluyor:
asıl ölçü **Calmar üstünlüğü**, alfanın pozitif olması şart değil, ama **taban**
duruyor — `strategy_cagr > 0`, yani para kaybeden bir strateji düşüşü küçük diye
üstün sayılmaz. Calmar iki taraf için de ölçülemezse eski mutlak kurala düşülür
(fail-closed: ölçülemeyen bir üstünlük üstünlük değildir).

Anahtar ana kapıyla **ortak**: `AGENT_BENCHMARK_GATE` (`risk_adjusted` |
`absolute`). Sabit route modülünden ithal edilmiyor — `agent_backtest` zaten
`backtest_robustness`'ı içeri alıyor, ters yön döngü olurdu; aynı ortam değişkeni
doğrudan okunuyor, operatör için tek düğme.

İki yan detay: sembol satırları artık `strategy_calmar` / `benchmark_calmar` /
`strategy_cagr` taşıyor (karar yalnız satırı gören bir fonksiyonda verildiği için
şart; taşınmasaydı kapı sessizce mutlak kurala düşerdi), ve ilerleme satırındaki
`✓/✗` ikonu artık kapının GERÇEK ölçütünü gösteriyor — eskiden excess'e bakıyordu,
yani kapı değiştikten sonra ekranda "✗" yazan bir peer skorda geçmiş olabilirdi.

## Reddin BÜYÜKLÜĞÜ de kayda geçer (2026-08-17)

Kapının ölçüsü Calmar üstünlüğü ve reddi tek bir etiketti: `worse_risk_adjusted`.
O etiket barın %98'ine ulaşan adayla %2'sinde kalanı ayırt edilemez kılıyordu, yani
"eşiği biraz gevşetsem kaç aday girerdi" sorusu ancak defteri elle yeniden
oynatarak cevaplanabiliyordu.

`app_constants._stamp_annualized_comparison` artık
`calmar_ratio_vs_benchmark = strategy_calmar / benchmark_calmar` damgalıyor. ORAN
seçildi, fark değil: Calmar zaten bir oran ve barın büyüklüğü pencereye göre
değişiyor. `1.0` = tam eşikte.

İki yüzey: aday satırı `… · Calmar ×0.98 vs b&h`, ve 0/N faz satırı
`(Calmar en iyi ×0.98, medyan ×0.22 vs b&h)` — yalnız `worse_risk_adjusted` ile
elenenlerden. Ölçülen tur 1 tam da bunu gösteriyordu: "0/15 elendi" cümlesi
"hiçbiri yaklaşamadı" diye de okunabiliyordu, oysa biri barın %98'indeydi.

TEŞHİS, karar değil: kabul kuralı tek kopya (`benchmark_rejection`) ve bir test
kararın alanla/alansız BİREBİR aynı kaldığını çiviliyor.

**Bayat metin de düzeltildi.** Faz satırı "positive benchmark excess" diyordu;
o kümülatif kural 2026-08-15'te risk-ayarlı ölçüyle değiştirilmişti ve operatör
faz satırını okuyup YANLIŞ eşiği kurcalıyordu. Tarif artık
`_gate_description()` üzerinden `benchmark_gate_mode()`'a soruluyor — sabit
yazılmış bir tarif, kural değiştiğinde sessizce yalan söyler.

Kapının canlı davranışı için bkz. [[nau_auto_kosusu_755b7880_2026_08_17]]:
alfa kapısı üç turda 0/15 → 2/15 → 3/15 açıldı, **eşiğe hiç dokunulmadan**.

## Ekrandaki sayı kararın sayısı değildi — WFO (2026-08-18)

Kapının WFO bacağı üç ayrı yerde sayılıyordu ve **ikisi başka şeyi sayıyordu.**
Ölçüldü (koşu `8aa18365`, tur 3, artefaktdan):

| | |
|---|---|
| geçerli pencere | 60 |
| **PnL'i pozitif** — adım satırı + elenme satırı | 37/60 = %62 |
| **al-tut'u geçen** — kapının kendisi | 28/60 = **%47** |

Aday `❌ Failed — IS/OOS: ✓ · WFO: 37/60 · MC −%21,5 · Multi-symbol: ✓` satırıyla
elendi: dört ölçüt de çıtayı geçiyor görünürken. Gerekçesi EKRANDA OLMAYAN bir
ret. Aynı turun ikinci adayı da aynı sebeple düştü (alfa 0/2, ekranda 1/2), yani
2/2 — ve ben de sebebi ilk bakışta ekrandaki en kötü kaleme (`⚠ Limited`)
yazdım, yanlış.

Neden ıraksıyorlar: 22 yıllık boğada bir pencerede **kâr etmek kolay**, al-tut'u
geçmek değil. Karar alfayı istiyor, ekran kârlılığı gösteriyordu.

Düzeltme: `auto.robustness.wfo_verdict` — karar VE sayıları tek NamedTuple'da.
Üç tüketici de onu okuyor; `display` kararın oranını veriyor. Kârlılık sayısı
teşhis olarak yanında duruyor (`28/60 beat buy&hold · 37/60 merely profitable`)
çünkü aradaki fark "boğayı mı taşıyor, alfa mı üretiyor" sorusunun cevabı.
Ayrıca sessiz `failed += 1` kaldırıldı: ret artık gerekçesiyle yazılıyor.

Kural DEĞİŞMEDİ — aynı eşik, aynı fail-closed davranış (ölçülemeyen alfa olumlu
sayılmaz), aynı sönümlenmiş-Sharpe koşulu. Değişen tek şey kaç yerde
hesaplandığı ve ne yazdığı. İlke zaten bu depoda yazılıydı
(`_holdout_promotion_verdict`: *"gerekçe metni boolean ile TAM OLARAK aynı
bayraklardan türetilmelidir"*); WFO bacağı onu uygulamıyordu.

## Geçiş cümlesi kendi kapsamını söylemiyordu (2026-08-18)

Koşu `9016d12a`, tur 1'de bir aday şu satırla kazanan ilan edildi:

    ✅ ALL TESTS PASSED! IS/OOS: ✓ Robust · WFO: — · Multi-symbol: — (yetersiz veri)

Aynı satırın içinde iki TİRE var. WFO'nun tek geçerli penceresi yok, beş akranın
beşi de az işlemden sayılmamış: **dört ölçütün ikisi hiç koşmadı.** Kural
dürüsttü (gevşek modda `evaluated ≥ 2`), yanlış olan cümleydi — "geçti" ile
"koşulamadı" aynı kovaya girince kanıtın genişliği tam da EN DAR olduğu anda
gizleniyor.

Artık `✅ PASSED on 2/4 criteria (≥2 required; the rest could not be evaluated)`.
Sayı ikinci kez hesaplanmıyor: `_robustness_passed` ikiye ayrıldı —
`_robustness_tally` kararı ve sayaçlarını birlikte döndürüyor, eski ad ince bir
bool sarmalayıcı olarak kaldı (çağıranların `is True` sözleşmesi bozulmasın).

Bölme bir yan ders de verdi: eski adı hedefleyen beş test hedefini kaybetti.
Üçü monkeypatch'ti ve GÜRÜLTÜLÜ kırıldı; ikisi `inspect.getsource` denetimiydi
ve **sessizce anlamsızlaşacaktı** — iki satırlık kabukta aradıkları kusur zaten
bulunmaz, yani yeşil kalırlardı.

## Kapının canlı sınavı: mühürlü holdout ilk kez koştu (2026-08-18)

Beş koşu ve on üç tur boyunca hiçbir aday robustluk zincirini geçemediği için
mühürlü kapı hiç açılmamıştı. `9016d12a` turu 1'de açıldı ve
[[nau_holdout_dogrulama_turu_2026_08_18]] turunun üç düzeltmesi de doğrulandı:

* **WFO uyarısı** pencereyi adayın hızından 48/16 aya genişletti, taban
  sınırına dayandı ve *"muhtemelen susacak"* dedi — sustu (geçerli pencere yok).
* **Mühürlü tahmin** koşudan ÖNCE `~3,9 giriş` dedi; gerçekleşen **4**.
* **Oransal eşik** 5'ti (eski sabit 20 olurdu). Ret artık adayın hızı hakkında:
  `only 4 holdout trades; need 5`. Mühürde `excess −%71` — al-tut +%144 yaparken
  strateji +%73'te kalmış, yani ölçülebilseydi de reddedilecekti.

Kayıt artık yargılandığı eşiği taşıyor: `min_trades_required: 5, train_bars:
4899, train_trades: 22`.

## Kapının üç düzeltmesi ve çıtanın ölçülmesi (2026-08-20)

Beş koşu üst üste aynı yerde takıldı — adaylar dört ölçütün üçünden geçip
**yalnız WFO'dan** düşüyordu (payda sınırını geçen 12 adayın alfa oranı
%10-47, medyan %34; rastgele tabanın medyanı %23). Çıta ölçüldü ve üç şey çıktı; ayrıntı ve sayılar
[[wfo_cita_kalibrasyonu_2026_08_20]]'de.

**1. Açıklama satırı yanlış ölçütü öğretiyordu.** Adım başlığı "≥50% of windows
must have positive PnL" diyordu, karar ise AL-TUT'U GEÇEN pencereleri sayıyor.
Operatöre önce yanlış kural söyleniyor, sonra o kurala göre okunamayan bir sayı
(`1/7`) gösteriliyordu. Bu, aynı gün düzeltilen "ekrandaki sayı kararın sayısı
değildi" hatasının başlık satırındaki ikizi — sonuç satırı düzeltilmiş ama
başlık atlanmıştı.

**2. Kapı, "karar ölçütü olamaz" diye belgelenmiş alandan karar veriyordu.**
`app_constants` iki karşılaştırma üretiyor ve ikisi aynı sözlükte duruyor:

| alan | benchmark tabanı |
|---|---|
| `excess_return_fraction` | `gross_buy_and_hold_no_costs` — brüt, kümülatif |
| `annualized_alpha` | `round_trip_cost_and_optional_dividends` — iki taraf da net |

Birincisinin docstring'i *"geriye uyumluluk için duruyor ama KARAR ölçütü
olamaz"* diyordu; `wfo_verdict` yine onu okuyordu. Kapı ikincisine çevrildi.
**Etkisi önceden ölçüldü: 54 artefakt, 973 pencere, ayrışan pencere sayısı 2** —
maliyet %0,02, pencere getirileri ±%1-20. Yani doğruluk düzeltmesi, kalibrasyon
değil; kod içine bu sayı yazıldı ki "kapıyı düzelttik, artık geçerler" beklentisi
doğmasın.

**3. Oranın paydasında alt sınır yoktu.** `1/2 = %50` ile bir aday gerçekten
geçmişti (koşu `4f7849df`). `WFO_MIN_VALID_WINDOWS = 10` geldi; sınırın altı
"atlama" değil **ret** — çünkü kapıda `measured=False` `_skip()`e düşüyor,
`failed` artmıyor ve aday kalan üç ölçütle terfi edebilirdi.

## Uçtan uca doğrulama: kapı doğru geçiriyor, doğru reddediyor (2026-08-21)

WFO'nun "al-tut'u geçti" tanımı sistemin ortak kuralına (`benchmark_rejection`)
hizalandı — dört ay boyunca terk edilmiş bir kuralı uyguluyordu. Ardından kapı
uçtan uca sınandı ve ilk kez al-tut'u risk-ayarlı geçen bir strateji bulundu.
Sonuç, kalan sınır ve karar gerektiren soru:
[[kapi_ucdan_uca_dogrulandi_2026_08_21]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_360_canli_review_iyilestirmeleri]]
- [[auto_arama_ekonomisi]]
- [[multi_symbol_generalization]]
- [[nau_auto_kosulari_2026_08_18]]
- [[nau_deepr_dorduncu_tur_2026_08_11]]
- [[nau_deepr_mimari_katman_ayrimi]]
- [[webapp_module_map]]
- [[wfo_cita_kalibrasyonu_2026_08_20]]
<!-- BACKLINKS:END -->
