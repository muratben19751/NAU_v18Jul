# X (Twitter) anahtar kelime izleyicisi

`ttkom` gibi bir anahtar kelimeyi X'te 5 dakikada bir yoklar, **yeni** tweetleri
bir deftere yazar, konsola tek satır özet basar ve toplu e-posta gönderir.

> ⚠️ **Bu klasör Nautilus projesinin parçası değildir.** Aynı depoda durur ama
> ondan hiçbir modül import etmez, `NAU_*` ortam değişkenlerini ve veri kökünü
> kullanmaz, onun test süitine veya PM2 girdisine karışmaz. Ayrı çalıştırılır,
> ayrı test edilir, ayrı bağımlılığı vardır (yalnız Playwright).

## Uyarı — bunu bilerek kullanın

Bu araç X'e giriş yapmış bir tarayıcı oturumuyla arama sayfasını okur.
**X'in kullanım şartlarına aykırıdır ve hesabınız askıya alınabilir.**
5 dakikalık ritim, otomasyon tespitinin aradığı desendir. Aralığı
`XWATCH_INTERVAL_S` ile yükseltebilirsiniz.

Neden API değil: X API v2'nin `search/recent` ucu 2026'da kullandıkça-öde kredi
modeline geçti ve ücretsiz katman kalmadı.

## Kurulum

```bash
pip install -r twitter/requirements.txt
playwright install chromium

cd twitter
python x_login.py          # açılan tarayıcıda KENDİ X hesabınızla giriş yapın
```

`x_login.py` parolanızı ne sorar ne saklar — tarayıcıya siz yazarsınız, betik
yalnız sonuçtaki oturum çerezini `~/.cache/x_watch/x_storage_state.json`
dosyasına kaydeder. **O dosya hesabınıza tam erişim demektir**; repo dışındadır
ve `.gitignore`'dadır.

E-posta için Gmail **uygulama şifresi** gerekir (normal hesap şifreniz SMTP'de
çalışmaz — Google Hesabı → Güvenlik → Uygulama şifreleri):

```powershell
setx XWATCH_SMTP_PASSWORD "<gmail uygulama şifresi>"
```

Sonra **yeni** bir terminalde:

```bash
python x_watch.py --once     # tek tur: bulunanları basar, bir test maili atar
```

## 7/24 çalıştırma

```bash
pm2 start twitter/ecosystem.config.js
pm2 save
pm2 logs x-watch
```

Log satırı şöyle görünür:

```
[x_watch] q=ttkom fetched=23 new=2 mailed=yes next=302s
```

## Ayarlar

Hepsi ortam değişkeni; hiçbiri zorunlu değil.

| Değişken | Varsayılan | Ne yapar |
|---|---|---|
| `XWATCH_QUERY` | `ttkom` | Aranacak anahtar kelime |
| `XWATCH_INTERVAL_S` | `300` | Yoklama aralığı (gerçek bekleme ±%15 jitter'lı) |
| `XWATCH_HEADLESS` | `1` | `0` yapılırsa tarayıcı görünür açılır (hata ayıklama) |
| `XWATCH_DATA_DIR` | `~/.cache/x_watch` | Defter, durum ve oturum dosyalarının kökü |
| `XWATCH_MAIL_TO` | *(boş)* | Alıcı. **Boşsa mail kapalıdır**, yalnız konsol + defter |
| `XWATCH_MAIL_MIN_S` | `900` | İki mail arası asgari süre; arada bulunanlar biriktirilir |
| `XWATCH_SMTP_HOST` | `smtp.gmail.com` | |
| `XWATCH_SMTP_PORT` | `465` | `465` → SSL, aksi hâlde STARTTLS |
| `XWATCH_SMTP_USER` | *(boş)* | Gönderen adres / SMTP kullanıcısı |
| `XWATCH_SMTP_PASSWORD` | *(boş)* | **Uygulama şifresi.** Asla dosyaya yazmayın |
| `XWATCH_X_USER` | *(boş)* | Otomatik yeniden giriş için X kullanıcı adı (opsiyonel) |
| `XWATCH_X_PASSWORD` | *(boş)* | Otomatik yeniden giriş parolası; **boşsa parola saklanmaz** |

Son ikisi ayarlanmazsa (önerilen) oturum düştüğünde izleyici durur ve size
"yeniden giriş gerekiyor" maili atar. 2FA açık bir hesapta otomatik giriş zaten
tamamlanamaz.

## Nasıl davranır

```
main()  ──►  run_once()  ──►  fetch_search_html()   Playwright + kayıtlı oturum
                  │                                  login/429 tespiti burada
                  ├──►  parse_tweets(html)           stdlib html.parser, ağa dokunmaz
                  ├──►  dedupe (tweet id)            defterin son diliminden
                  ├──►  append_tweets()              x_watch.jsonl (append-only)
                  └──►  kısmalı send_mail()          mail_min_s başına tek özet
```

Üç davranış özellikle düşünülmüş:

**Jitter.** Tam 300.0 saniyelik metronom, otomasyon tespitinin en kolay
yakaladığı imzadır; bekleme ±%15 rastgeledir. 429'da üstel geri çekilme
(tavan 1 saat).

**Sessiz sıfır yoktur.** X'in HTML'i habersiz değişirse parser boş döner ve bu
"bugün tweet yok"tan ayırt edilemez — bozuk bir izleyicinin en tehlikeli hâli.
Sayfa geldiği hâlde hiç tweet kartı çıkmazsa sayaç artar, 5 turda bir kez uyarı
maili gider (her turda değil).

**Oturum düşünce durur.** Boşuna istek atmaya devam etmek hem beyhude hem de
hesabı daha çok riske atar. `relogin()` tazeleyemezse mail atıp `exit 2` ile
çıkar.

**Mail kısması diskte tutulur.** pm2 `autorestart` süreci habersiz yeniden
başlatır; bellekte tutulsa her restart kısmayı sıfırlar ve gönderilmemiş
tweetler kaybolurdu. Gönderim başarısız olursa bekleyenler temizlenmez.

## Veri dosyaları

`~/.cache/x_watch/` altında (`XWATCH_DATA_DIR` ile taşınır):

- `x_watch.jsonl` — append-only tweet defteri; 20 MB'ta `.jsonl.1` arşivine devreder
- `x_watch_state.json` — mail kısması + parser sağlığı sayaçları
- `x_storage_state.json` — X oturum çerezi (**hesaba tam erişim**, POSIX'te 0600)

## Test

Ağa hiç çıkmaz; kaydedilmiş bir arama sayfası üzerinden koşar.

```bash
pytest twitter/tests -q          # 40 test
```

Ağa çıkmadan tam boru hattını görmek için:

```bash
python twitter/x_watch.py --dry-run --html twitter/tests/fixtures/x_search_ttkom.html
```

## Sorun giderme

| Belirti | Sebep |
|---|---|
| `Oturum dosyası yok` | `python x_login.py` hiç koşulmamış |
| `X giriş sayfasına yönlendirildi` | Çerez düşmüş — `x_login.py`'yi tekrar koşun |
| `parse=FAILED` logda | X'in HTML'i değişmiş olabilir; `parse_tweets` gözden geçirilmeli |
| Mail gelmiyor | `XWATCH_MAIL_TO` boş, ya da SMTP kullanıcı/şifre eksik (log uyarır) |
| `playwright kurulu değil` | `pip install playwright && playwright install chromium` |
