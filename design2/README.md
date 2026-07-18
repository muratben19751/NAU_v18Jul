# Handoff: Nautilus Lab — Otonom Backtest Uygulaması (Siyah Tema Redesign)

## Overview
`https://github.com/muratben19751/nautilus_web_app` (FastAPI + Jinja2 + HTMX, mavi-lacivert "trading terminal" teması) uygulamasının **saf siyah tonlarında, okunabilirliği artırılmış** yeniden tasarımı. 6 ekran: Dashboard, Agent, Strategy Lab, Backtest, Data, Reports.

Hedef codebase: mevcut repo'nun `web/templates/*.html` + `web/static/app.css` katmanı. Bu handoff, o Jinja2/HTMX şablonlarının ve CSS token'larının yeni tema ile güncellenmesi (veya istenirse başka bir frontend'e taşınması) için yeterli detayı içerir.

## About the Design Files
Bu paketteki `Nautilus Lab.dc.html` **HTML ile üretilmiş bir tasarım referansıdır** — amaçlanan görünüm ve davranışı gösteren interaktif bir prototiptir, production kodu değildir. Görev: bu tasarımı hedef codebase'in mevcut ortamında (Jinja2 + HTMX + `app.css`) yeniden yaratmak. Prototipteki veri ve "otonom döngü" simülasyonu sahtedir; gerçek uygulamada backend (loop_runner, backtest.py, agent.py) zaten mevcuttur — sadece görsel katman değişecek.

## Fidelity
**High-fidelity (hifi).** Renkler, tipografi, spacing ve durumlar (hover/disabled/running) finaldir; birebir uygulanmalıdır. Grafiklerin prototipte SVG çizilmesi tasarım tercihi değildir — gerçek uygulamada mevcut Chart.js kalabilir, sadece renkler aşağıdaki token'lara çekilmelidir.

## Design Tokens

### Renkler (app.css `:root` değişkenlerinin yeni karşılıkları)
| Token | Eski | Yeni |
|---|---|---|
| `--bg-950` (sayfa arka planı) | `#0a0e17` | `#080808` |
| `--bg-900` (panel / sidebar) | `#101827` | `#121212` (sidebar/topbar: `#0e0e0e`) |
| `--bg-850` (hover satır) | `#131d2f` | `#171717` |
| `--bg-800` (aktif nav / raised) | `#1a2332` | `#1a1a1a` (mini stat kartları: `#0d0d0d`) |
| `--border` | `#1a2332` | `#242424` (satır ayracı: `#1d1d1d`, sidebar: `#222222`) |
| `--accent` | `#60a5fa` (mavi) | **`#f2f2f2` / `#ffffff`** (monokrom — birincil butonlar beyaz, metin siyah `#0a0a0a`) |
| `--pnl-up` | `#22c55e` | `#4ade80` |
| `--pnl-down` | `#ef4444` | `#f87171` |
| uyarı/retry | — | `#f0b100` |
| `--text-primary` | `#e5e7eb` | `#f2f2f2` |
| `--text-secondary` | `#9ca3af` | `#b8b8b8` – `#c8c8c8` (panel başlıkları) |
| `--text-muted` | `#6b7280` | `#8a8a8a` – `#9c9c9c` (kontrast artırıldı) |
| input arka planı | `--bg-950` | `#0c0c0c`, border `#2c2c2c`, focus border `#8a8a8a` |
| kod bloğu arka planı | `rgba(0,0,0,.35)` | `#0a0a0a` + border `#222222` |

Yeşil/kırmızı badge dolguları: `rgba(74,222,128,0.07)` / `rgba(248,113,113,0.07)`, border `...0.35`.

### Tipografi
- Sans: **IBM Plex Sans** (Inter'in yerini aldı) — 400/500/600/700, Google Fonts.
- Mono: **JetBrains Mono** (korundu) — tüm sayısal değerler, id'ler, timestamp'ler, kod; `font-variant-numeric: tabular-nums`.
- Ölçek: gövde 14px/1.55 · tablo 13px (mono hücreler 12–12.5px) · panel başlığı 11.5px uppercase, letter-spacing .15em · form etiketi 10.5px uppercase · KPI etiketi 9.5px uppercase · KPI değeri 21px (Reports özet kartları 17px) · "Best PnL" hero değeri 30px · buton 12.5px/600.

### Spacing & Şekil
- Sayfa içi padding: `26px 28px`; paneller arası gap: `18px`.
- Panel: radius `8px`, border 1px; panel header `13px 18px`; panel body `18px 20px`.
- Tablo hücresi `10–11px 14px` (ilk/son sütun 18px); mini kart `10px 12px`, radius `6px`.
- Buton: `10px 18px`, radius `6px`; input: `10px 12px`, radius `6px`.
- Sidebar `236px`; topbar `60px`; nav linki `11px 22px`, aktifken sol `2px solid #ffffff` + bg `#1a1a1a` + 600 weight.

## Screens / Views

### 1. Dashboard
- **Amaç:** Otonom döngüyü başlat/durdur, iterasyonları canlı izle.
- **Layout:** Dikey stack → (a) kontrol şeridi, (b) `grid-template-columns: 2fr 1fr` → Iterations tablosu | Best So Far kartı, (c) tam genişlik equity grafiği.
- **Kontrol şeridi:** başlık "Autonomous Loop" + mono alt satır (iterasyon sayısı); Mode select (`🤖 LLM Agent` / `📦 Catalog cycle`); **▶ Start** (birincil, beyaz) / **■ Stop** (danger, kırmızı %10 dolgu). Çalışmayan buton: bg `#2a2a2a`, metin `#6a6a6a`, `cursor:not-allowed`. Durum rozeti: RUNNING iken yeşil + yanıp sönen 7px nokta (`@keyframes blink {50%{opacity:.25}}`, 1.4s).
- **Iterations tablosu:** sütunlar # / Time / Strategy·Params (iki satır: strateji 12.5px `#f2f2f2`, params 11px `#8a8a8a`) / PnL (sağa hizalı, 600, yeşil-kırmızı) / Sharpe / Trades / Win. Sticky thead, max-height 430px scroll, satır hover `#171717`. En yeni üstte; 2 sn'de bir yeni satır (backend'de HTMX polling zaten var).
- **Best So Far:** strateji adı + params (mono), "Realized PnL" 30px hero değer, 2×2 mini stat grid (Sharpe/Trades/Win Rate/Max DD — Max DD daima `#f87171`), altta "Claude Rationale" paragrafı (12.5px `#b8b8b8`). Header'da `iter #NN` yeşil rozet.
- **Equity grafiği:** 250px, area dolgusu = çizgi rengi + %8 opaklık (`color + '14'`), çizgi 2px `#e5e5e5` (kullanıcı tweak'i `#4ade80` seçebilir), yatay gridline'lar `#1d1d1d`, min/max etiketleri sol üst/alt köşede 11px mono `#8a8a8a`.

### 2. Agent
- **Amaç:** Claude ajanının durumu ve etkinlik geçmişi.
- **Layout:** (a) durum şeridi, (b) 3 eşit görev kartı, (c) `3fr 2fr` → Activity Log | AST Guardrails.
- **Durum şeridi:** "Claude Sonnet 4.6" başlık; yeşil ONLINE rozeti (yanıp sönen nokta); sağda mini istatistikler: API Key (maskeli `sk-ant-····f81`), Toplam Çağrı, Son Çağrı.
- **Görev kartları:** etiket (uppercase 10.5px) + 28px mono sayaç + açıklama + en altta kaynak dosya referansı (`loop_runner.py · agent.py` vb. 11px `#7a7a7a`).
- **Activity Log:** Time / Görev / Detay (mono) / Durum rozeti — `✓ OK` yeşil, `↻ RETRY` amber `#f0b100`. Otonom döngü çalışırken parametre önerileri buraya da akar.
- **AST Guardrails:** "Yasak" (kırmızı başlık) ve "İzinli" (yeşil başlık) mono kod blokları + smoke-exec açıklaması. İçerik repo README'sinden.

### 3. Strategy Lab (eski `/strategy`)
- **Amaç:** Görsel strateji besteleyici.
- **Layout:** `3fr 2fr` — sol composer stack, sağ sticky (top 86px) Nautilus Wiki paneli.
- **Sol stack sırası:** (0) ✓ SAVED flash paneli (kaydetten sonra 3 sn, yeşil border) → (1) 🧠 AI Strategy Designer şeridi (border `#363636` ile hafif vurgulu; buton "🧠 Claude'a önerttir", spinner "Claude düşünüyor…") → (2) 01·Strategy Metadata (Name 2fr + Trade size 1fr, Description textarea) → (3) 02·Add Signal Block (Block type select — 8 tip: ma_cross, rsi_threshold, bollinger_break, macd_cross, donchian_break, atr_stop, supertrend_flip, ema_ribbon; Role select; tipe göre değişen sayısal parametre alanları; **＋ Add block**) → (4) 03·Current Blocks tablosu (# / blok adı·tipi / rol rozeti — entry yeşil, exit kırmızı / mono params / ✕ sil) → (5) 03.5·Advanced Options `<details>` (Entry/Exit logic OR-AND, Order type, SL %, TP %, Sizing, Allow short checkbox `accent-color:#d4d4d4`) → (6) 04·Save şeridi (**💾 Save Strategy**) → (7) 05·Saved Strategies tablosu → (8) 06·Custom Blocks (Beta) `<details>` (Label + doğal dil açıklama + "🧠 Claude'a yazdır"; sonuç: ✓ AST OK rozeti + Python kodu `<pre>` + "💾 Kaydet ve Kullan").
- **Wiki paneli:** Block type seçimine göre içerik değişir; başlık + açıklama + `evaluate()` sözleşmesi kod bloğu.

### 4. Backtest
- **Layout:** `3fr 2fr` — sol: 01·Select Strategy (katalogdan select + Data select `BTC (Yahoo daily)` / `US Index (NFS ticks)` + **▶ Run Backtest** + "running BacktestEngine…" spinner) → Result paneli → Order Flow `<pre>` diyagramı. Sağ: sticky Order Flow Pipeline wiki'si.
- **Result paneli:** 5'li KPI grid (Realized PnL renkli / Sharpe / Trades / Win Rate / Max Drawdown kırmızı; mini kart `#0d0d0d`, değer 21px) + Equity Curve grafiği (230px, Dashboard ile aynı stil). Koşum ~1.1 sn sürer (gerçekte BacktestEngine süresi), bu sırada spinner görünür.
- Katalog boşken: boş durum paneli "Bir strateji seçip Run Backtest'e basın."

### 5. Data
- **Layout:** (a) dataset şeridi: "BTC-USD · Yahoo Finance · Daily" + parquet cache yolu; mini istatistikler Bars `4,315` / Range / Son Güncelleme; **↻ Yeniden İndir** butonu + "yfinance download…" spinner (~1.2 sn) → (b) BTC-USD Close tüm-geçmiş grafiği → (c) Veri Kaynakları tablosu: Yahoo Finance (CACHED yeşil rozet) ve NFS Ticks (ON DEMAND gri rozet).

### 6. Reports
- **Layout:** (a) 4'lü özet KPI grid — Toplam Koşum / Best PnL (yeşil) / Ort. Sharpe / Ort. Win Rate; **küçük varyant:** padding `10px 14px`, etiket 9px, değer 17px → (b) Backtest Geçmişi tablosu: Tarih / Strategy / Data / PnL / Sharpe / Trades / Win / Max DD (kırmızı).

### Shell (tüm ekranlar)
- **Sidebar (236px, `#0e0e0e`):** marka bloğu `NAUTILUS·LAB` (JetBrains Mono 14px, letter-spacing .18em) + "Otonom Backtest Ajanı" alt başlığı; nav: Dashboard ◉ · Agent ◎ · Strategy Lab ◈ · Backtest ▶ · Data ▤ · Reports ▦; en altta muted mono bilgi bloğu (model / engine / veri).
- **Topbar (60px, sticky):** sol — aktif sayfa adı (mono uppercase); sağ — 4 mini stat: Instrument `BTC/USD · YAHOO` / Bars / Range / Last (gerçek uygulamada `market` viewmodel'inden gelir).

## Interactions & Behavior
- Nav: tek sayfa state'i (prototip) → gerçek uygulamada mevcut route'lar; aktif link stili yukarıdaki gibi.
- Hover: birincil buton `#ffffff`; ghost buton bg `#1c1c1c`; tablo satırı `#171717`; sil ikonu `#f87171` + kırmızı border.
- Start/Stop: disabled durumlar karşılıklı; RUNNING rozeti + nokta animasyonu; iterasyon geldikçe tablo ve Agent log'u üstten büyür (HTMX 2s polling korunur).
- Spinner'lar mono 11.5px `#8a8a8a` metindir (htmx-indicator ile birebir eşleşir).
- `<details>` panelleri (Advanced Options, Custom Blocks) kapalı başlar; summary panel-header gibi stillenir, marker gizli.
- Block type değişince parametre alanları ve wiki içeriği güncellenir (mevcut `hx-get` + `loadWiki` akışı).

## State Management
Prototipteki state gerçek uygulamada zaten backend'de yaşar; UI tarafında gereken tek şey mevcut HTMX fragment'larının yeni markup'la güncellenmesidir. Fragment listesi: `loop_status`, `iterations`, `best`, `backtest_result`, `drafts_list`, `catalog_list`, `block_form`, `custom_blocks_list`, `advanced_options`.

## Assets
Harici asset yok. İkonlar Unicode karakterlerdir (◉ ◎ ◈ ▶ ▤ ▦ ✕ ↻). Fontlar Google Fonts: IBM Plex Sans, JetBrains Mono. Grafikler: prototipte inline SVG; production'da Chart.js konfigürasyonunda `borderColor` → grafik rengi token'ı, `backgroundColor` → aynı renk %8 opaklık, grid `#1d1d1d`, tick'ler `#8a8a8a` JetBrains Mono 10-11px.

## Files
- `Nautilus Lab.dc.html` — 6 ekranın tamamını içeren interaktif hifi prototip (tek dosya; ekranlar arasında soldaki nav ile geçilir).
