---
title: X (Twitter) Anahtar Kelime İzleyicisi
type: synthesis
sources:
  - https://docs.x.com/x-api/introduction
  - sources/02_architecture_docs.md
last_updated: 2026-08-15
summary: PM2 altında ayrı süreç olarak koşan, giriş yapmış X oturumuyla 5 dakikada bir anahtar kelime yoklayan, yeni tweetleri JSONL defterine yazıp kısmalı e-posta özeti gönderen izleyici.
key_concepts:
  - crash_only_design
  - webapp_module_map
  - surec_yoneticisi_ortami_dondurur
---

# X (Twitter) Anahtar Kelime İzleyicisi

`x_watch.py`, X üzerinde bir anahtar kelimeyi (varsayılan `ttkom` — Türk Telekom,
BIST) 5 dakikada bir yoklar. `nau-web`'ten **ayrı** bir PM2 sürecidir: sunucu
yeniden başlatıldığında izleme kesilmesin, izleyici çöktüğünde web ayakta kalsın.

## Neden kazıma, neden API değil

X API v2'nin `search/recent` ucu 2026'da kullandıkça-öde kredi modeline geçti ve
ücretsiz katman kalmadı. Operatör anahtarsız yolu **bilerek** seçti (2026-08-15):
Playwright + kendi hesabının oturum çerezi.

Bunun bedeli kayıt altında olmalı, çünkü tasarımın yarısı bu bedeli yönetmekle
geçiyor:

| Risk | Karşılığı |
|---|---|
| X ToS ihlali, hesap askıya alınabilir | Operatörün kararı. Aralık tek düğmeden (`NAU_XWATCH_INTERVAL_S`) yükseltilebilir; bekleme ±%15 jitter'lıdır — tam 300.0 sn'lik metronom, otomasyon tespitinin en kolay yakaladığı imzadır. |
| X'in HTML'i habersiz değişir | "Sıfır tweet" ile "parser bozuldu" AYRI durumlar: sayfa geldiği hâlde hiç `article[data-testid="tweet"]` yoksa `parse_fail_streak` artar, eşiği geçince TEK seferlik uyarı e-postası gider. Sessiz sıfır, bozuk bir izleyicinin en tehlikeli hâlidir. |
| Oturum çerezi düşer | Login duvarı görülünce döngü boşuna istek atmaz: ya `relogin()` (kimlik ayarlıysa) tazeler, ya durup "yeniden giriş gerekiyor" maili atar ve `exit 2` ile çıkar. |

## Akış

```
x_watch.main()  ──►  run_once()  ──►  fetch_search_html()   Playwright, storage_state
                          │                                  login/429 tespiti burada
                          ├──►  parse_tweets(html)           stdlib html.parser, ağa dokunmaz
                          ├──►  dedupe (tweet id)            load_seen_ids: defterin SON dilimi
                          ├──►  append_tweets()              DATA_DIR/x_watch.jsonl (append-only)
                          └──►  kısmalı send_mail()          mail_min_s başına tek toplu özet
```

`run_once` çağrı yoluna **asla** exception sızdırmaz — [[crash_only_design]] ve
`token_ledger.record`'un sözleşmesiyle aynı duruş: tek bir kötü tur izleyiciyi
öldüremez.

## Kalıcı durum

Hepsi `app_constants.DATA_DIR` altında ([[import_aninda_yakalanan_referans]]);
`tests/test_data_dir_is_the_only_storage_root.py` üçünü de bağlar.

- `x_watch.jsonl` — append-only tweet defteri, 20 MB'ta tek kuşaklık arşive devreder.
- `x_watch_state.json` — mail kısması (`last_mail_ts`, `pending`) ve parser sağlığı.
  **Diskte**, çünkü PM2 `autorestart` süreci habersiz yeniden başlatır; bellekte
  tutulsa her restart kısmayı sıfırlar ve gönderilmemiş tweetler kaybolurdu.
- `x_storage_state.json` — X oturum çerezi. Hesaba **tam erişim** demektir:
  `.gitignore`'da, POSIX'te 0600.

## Kurulum

```
pip install playwright && playwright install chromium
python scripts/x_login.py          # kendi X hesabıyla ELLE giriş, parola saklanmaz
setx NAU_XWATCH_SMTP_PASSWORD "<gmail uygulama şifresi>"
python x_watch.py --once           # tek tur
pm2 start ecosystem.config.js && pm2 save
```

Sırlar `ecosystem.config.js`'e **yazılmaz** — o dosya git'te
([[surec_yoneticisi_ortami_dondurur]]); işletim sistemi ortamına konur, pm2 miras alır.

## Test edilebilirlik

Tüm ağ teması `fetch_search_html` içine hapsedilmiştir; testler onu monkeypatch
eder (`data._fetch_bybit_page` kalıbı). `parse_tweets` sözleşmesi
`tests/fixtures/x_search_ttkom.html` üzerinde yazılıdır ve gerçek sayfanın
parser'ı zorlayan özelliklerini taşır: alıntı kartı (iç içe `<article>` + ikinci
`/status/` linki), emoji `<img alt>`, metinsiz tweet, tweet **dışı** `/status/`
linkleri.

Kuru koşu (ağa çıkmadan tam boru hattı):

```
python x_watch.py --dry-run --html tests/fixtures/x_search_ttkom.html
```

## İlgili

[[webapp_module_map]] · [[crash_only_design]] · [[import_aninda_yakalanan_referans]]

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
