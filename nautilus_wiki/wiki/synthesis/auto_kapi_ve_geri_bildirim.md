---
title: AUTO'nun kapısı ve geri bildirimi — neyi ölçüyor, modele ne söylüyor
type: synthesis
summary: WFO kapısı pencere başına yeniden optimize edilmiş varyantı sertifikalıyordu, kataloğa yazılan ise sabit spec'ti; ayrıca modele giden geçmişte zaman dilimi, drawdown ve komisyon yoktu. 2026-08-04 denetimi ve düzeltmeleri.
key_concepts:
  - auto_mission_control
  - auto_arama_ekonomisi
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/auto_arama_ekonomisi.md
  - wiki/synthesis/auto_mission_control.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-04
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

## Kalan açık uç

Round-robin ile `n_iterations == len(intervals)` seçilirse her strateji **tek**
TF'de bir kez sınanır ve sıralama (strateji × TF) çiftlerini tek listede
karşılaştırır. Artık en azından spec hedef TF bilinerek üretiliyor, ama bir
fikrin mi yoksa zaman diliminin mi elendiğini ayırmak için `n_iterations`'ı TF
sayısının katı seçmek gerekir.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[auto_360_canli_review_iyilestirmeleri]]
- [[auto_arama_ekonomisi]]
<!-- BACKLINKS:END -->
