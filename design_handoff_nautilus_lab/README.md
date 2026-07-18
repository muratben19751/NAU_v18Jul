# Handoff: Nautilus Lab — Web Arayüzü Yeniden Tasarımı

Hedef codebase: **`muratben19751/nautilus_web_app`** (FastAPI + Jinja2 + HTMX + Chart.js, stiller `web/static/app.css`).

## Overview
Nautilus Lab'ın üç sayfasının (Dashboard, Strategy Composer, Backtest) profesyonelleştirilmiş yeniden tasarımı. Mevcut koyu "trading terminal" ruhu korunur; tema rafine edilir (yeni yüzey/palet değerleri, IBM Plex Sans + JetBrains Mono), grafikler zenginleştirilir (gradyanlı equity eğrisi, drawdown alt grafiği, aylık getiri ısı haritası, topbar sparkline) ve Strategy Composer'a canlı bir "Strategy Preview" akış diyagramı eklenir.

## About the Design Files
Bu paketteki `Nautilus Lab.dc.html` **HTML ile yapılmış bir tasarım referansıdır** — amaçlanan görünümü ve davranışı gösteren bir prototiptir, doğrudan kopyalanacak production kodu değildir. Görev: bu tasarımı hedef codebase'in **mevcut ortamında** (Jinja2 template'leri + `app.css` + HTMX) yeniden üretmek. Prototipteki React-benzeri state simülasyonları (sahte iterasyon akışı, sahte Claude yanıtları) gerçek backend endpoint'lerine (HTMX polling, `/loop/*`, `/strategy/*`, `/backtest/*`) bağlanmalıdır. Chart'lar mevcut Chart.js kurulumuyla ya da hafif inline SVG ile üretilebilir; prototip SVG path yaklaşımını kullanır.

## Fidelity
**High-fidelity (hifi).** Renkler, tipografi, boşluklar ve durumlar finaldir; piksel hassasiyetinde uygulanmalıdır. Veriler sahtedir.

## Uygulama stratejisi (mevcut repoya eşleme)
1. `web/templates/base.html` — Google Fonts linkini değiştir: `IBM+Plex+Sans:wght@400;500;600;700` + `JetBrains+Mono:wght@400;500;600;700`. Sidebar'a logo bloğu ve alt "Engine" durum kartı eklenir; nav ikonları unicode yerine inline SVG olur (aşağıda).
2. `web/static/app.css` — `:root` token'ları aşağıdaki **Design Tokens** ile güncelle; `.panel`, `.btn-primary`, `.badge`, `.kpi`, `table.data` bileşenlerini yeni değerlerle revize et.
3. Yeni parçalar: drawdown grafiği (dashboard + backtest), aylık ısı haritası (backtest), Strategy Preview paneli (strategy sağ kolon), toast bildirimi, Run History paneli.
4. HTMX akışları aynen korunur (2s polling, OOB swap'ler); sadece fragment markup'ları yeni stile geçer.

## Screens / Views

### 1. Shell (tüm sayfalar)
- **Layout**: `grid-template-columns: 228px 1fr; min-height:100vh`. Sayfa arka planı `#0A0D14`.
- **Sidebar** (`#0D1119`, sağ kenar `1px solid #1C2433`, sticky, 100vh, flex column):
  - Brand bloğu: padding `22px 20px 18px`, alt çizgi `1px #1C2433`. 30×30px logo karesi (radius 8, `linear-gradient(135deg,#3672E8,#5EA0F6)`, içinde beyaz nautilus/çeyrek daire SVG). Başlık: JetBrains Mono 13px/700, letter-spacing `.14em`, "NAUTILUS·LAB". Alt satır: 9.5px uppercase, ls `.16em`, `#5C6778`, "Autonomous Backtest Agent".
  - Nav (padding `12px 10px`, gap 2px): her öğe flex, gap 11, padding `9px 12px`, radius 7, 13px/500. Pasif renk `#97A1B5`, hover bg `#131A28`. **Aktif**: bg `linear-gradient(180deg,rgba(74,140,240,.16),rgba(54,114,232,.08))`, renk `#E9EDF4`, `inset 0 0 0 1px rgba(94,160,246,.3)`. İkonlar 15×15 inline SVG (stroke `currentColor`, width 1.3): Dashboard = 2×2 yuvarlatılmış kare grid; Composer = iki blok + artı; Backtest = play üçgeni. Döngü çalışırken Dashboard öğesinin sağında 7px yeşil (`#2ED47E`) yanıp sönen nokta (`@keyframes blink {50%{opacity:.25}}`, 1.4s).
  - Alt "Engine" kartı: panel bg `#10151F`, border `#1C2433`, radius 8, padding `11px 12px`. Micro-label "ENGINE" (9px, ls .16em, `#5C6778`), durum satırı JetBrains Mono 11px `#97A1B5` + 6px durum noktası (`#2ED47E` çalışırken / `#5C6778` boşta), alt satır 10px `#5C6778` "Claude Sonnet 4.6 · Nautilus".
- **Topbar**: 58px, bg `rgba(13,17,25,.85)` + `backdrop-filter: blur(8px)`, alt çizgi `#1C2433`, sticky, z-10, padding `0 24px`. Solda sayfa başlığı (JetBrains Mono 12px/600 uppercase ls .14em `#97A1B5`). Sağda stat grubu (gap 26): her stat sağa hizalı iki satır — label 9px uppercase ls .14em `#5C6778`, değer JetBrains Mono 12px `#E9EDF4`. Instrument stat'ının solunda 86×26 sparkline SVG (çizgi `#5EA0F6` 1.4px, alan dolgusu `rgba(94,160,246,.10)`). "Last" değeri yeşil `#2ED47E` ve 600.
- **Content**: padding 24px.

### 2. Dashboard
Dikey stack, gap 16.
- **Control strip** (panel, padding `16px 18px`, flex, gap 14): sol blok — panel başlığı "AUTONOMOUS LOOP" (11px uppercase ls .15em `#97A1B5` 600) + açıklama satırı (JetBrains Mono 11.5px `#5C6778`). Sağda: MODE select, **Start** (primary buton), **Stop** (danger buton), durum rozeti.
  - Primary buton: padding `9px 16px`, radius 7, border `1px #3672E8`, bg `linear-gradient(180deg,#4A8CF0,#3672E8)`, beyaz 12.5px/600, play ikonu 10px. Hover `filter:brightness(1.1)`. Disabled `opacity:.35`.
  - Danger buton: bg `rgba(242,92,92,.08)`, border `rgba(242,92,92,.35)`, renk `#F79090`, hover bg `rgba(242,92,92,.16)`, stop-kare ikonu.
  - Durum rozeti: padding `7px 12px`, radius 7. Idle: border `#232C3E`, metin `#5C6778` "IDLE". Çalışırken: border `rgba(46,212,126,.3)`, bg `rgba(46,212,126,.05)`, yanıp sönen 7px yeşil nokta + "RUNNING · {status}" (`#2ED47E`) + `iter #N` (`#5C6778`). Metinler JetBrains Mono 10.5px uppercase ls .1em.
- **KPI şeridi**: `grid-template-columns:repeat(4,1fr); gap:12px`. Kart: panel stili, padding `15px 17px`; label 10px uppercase ls .16em `#5C6778`; değer JetBrains Mono 23px/600 tabular-nums (PnL yeşil/kırmızı, Iterations `#5EA0F6`); alt satır 11px `#5C6778`. Kartlar: Best PnL, Best Sharpe, Avg Win Rate, Iterations.
- **Iterations + Best** (`grid-template-columns:3fr 2fr; gap:16px`):
  - Iterations paneli: başlık satırı + sağda "latest first · live" (mono 10.5px `#5C6778`). Kolon başlıkları grid `62px 1.15fr 1.5fr 96px 62px 60px` (Time/Strategy/Params/PnL/Sharpe/WR), 9.5px uppercase `#5C6778`. Satırlar aynı grid, padding `10px 16px`, alt çizgi `#171E2B`, hover `#131A28`, yeni satır animasyonu `fade+translateY(-4px) .3s`. Time mono 11px `#5C6778`; Strategy mono 11.5px `#5EA0F6` ellipsis; Params mono 10.5px `#5C6778` ellipsis; PnL mono 12px/600 sağa (`#2ED47E`/`#F25C5C`); Sharpe/WR mono 11.5px `#97A1B5` sağa. Liste max-height 378px scroll.
  - Best So Far paneli: başlıkta "TOP PNL" rozeti (mono 9.5px, yeşil border/bg %35/%7). Gövde: strateji adı (mono 14px/600 `#5EA0F6`), param satırı (mono 10.5px `#5C6778`), 4'lü mini-KPI grid'i (iç kart bg `#0A0E16`, border `#171E2B`, radius 7, padding `9px 10px`; label 8.5px; değer mono 13px/600 — PnL renkli, Max DD `#F79090`), "CLAUDE RATIONALE" micro-başlık + açıklama kutusu (12px `#97A1B5`, bg `#0A0E16`, border `#171E2B`, radius 7, padding `11px 13px`).
- **Best Equity Curve paneli**: başlık + sağda "Nautilus BacktestEngine · realized PnL". Grafik satırı: solda 3 Y ekseni etiketi (mono 10px `#5C6778`, `$1005k` formatı, min-width 52px, sağa hizalı, dikey space-between) + SVG (`viewBox 0 0 860 220`, `preserveAspectRatio:none`, height 220). 3 yatay gridline `#171E2B`. Alan: dikey gradient `#5EA0F6` %22→0. Çizgi: `#5EA0F6` 2px, `vector-effect:non-scaling-stroke`. Altında **Drawdown** alt grafiği: height 54, kırmızı alan `rgba(242,92,92,.22)` (0'dan aşağı), solda `0%` / `-X.X%` etiketleri, altında "DRAWDOWN" micro-label.

### 3. Strategy Composer
`grid-template-columns:3fr 2fr; gap:16px`. Sağ kolon `position:sticky; top:82px`.
- **AI Strategy Designer** (en üst panel): bg `linear-gradient(180deg,rgba(54,114,232,.08),rgba(54,114,232,.02))` + panel bg; border 2.6s'de `#1C2433↔#3672E8` arasında nabız atar. Solda 36px yıldız-ikon karesi (bg `rgba(94,160,246,.1)`, border `rgba(94,160,246,.25)`, radius 9). Başlık 12.5px/600; açıklama 12px `#97A1B5`. Sağda primary buton "Claude'a önerttir" → loading'de 12px spinner (border-top beyaz, `spin .8s`) + "Claude düşünüyor…".
- **01 · Strategy Metadata**: Name (2fr) + Trade size BTC (1fr, mono) grid'i, altta Description textarea (2 satır). Input stili: bg `#0A0E16`, border `1px #232C3E`, radius 6, padding `9px 12px`, 13px; focus'ta border `#3672E8`. Field label: 10px uppercase ls .1em `#5C6778` 600, gap 6.
- **02 · Add Signal Block**: başlık sağında canlı mantık ipucu "entry OR-combined · exit OR-combined" (mono 10.5px). Block type (2fr select) + Role (1fr select). Altında parametre grid'i `repeat(auto-fit,minmax(150px,1fr))` — blok tipine göre int/float number input (mono) veya enum select. Blok yardım metni: 12px `#5C6778`, sol kenar `2px solid #232C3E`, padding-left 11. "＋ Add block" secondary buton (bg `#151C2B`, border `#2A3550`, hover `#1A2334`; ＋ işareti `#5EA0F6`).
- **03 · Current Blocks**: satır = index (mono 11px `#5C6778`) + blok adı (13px/500) ve `· type` (mono 10.5px) + param özeti (mono 10.5px `#5C6778`) + rol rozeti (mono 9.5px uppercase; entry: yeşil %35 border / %6 bg; exit: `#F79090`, kırmızı) + çöp ikonu butonu (13px SVG, hover'da kırmızı). Boş durum: ortalanmış `#5C6778` 12.5px metin.
- **03.5 · Advanced Options**: başlık satırı tıklanabilir (hover bg), sağda `▼/▲`. Açılınca bölümler: *Signal logic* (Entry/Exit logic selectleri), *Order & execution* (Order type, Limit offset bps; "Attach bracket (SL/TP)" checkbox; SL type/value + TP type/value 4'lü grid), *Position side & sizing* ("Allow SHORT" checkbox; Sizing mode + Equity % + ATR risk % 3'lü grid). Checkbox 15px, satır metni 12.5px `#97A1B5`.
- **04 · Save to catalog**: açıklama (dosya yolu mono) + primary "Save Strategy" (disket SVG). Validasyon: blok yoksa / entry yoksa hata toast'ı.
- **05 · Saved Strategies**: satır = ad (mono 12.5px/600 `#5EA0F6`) + `id=… · açıklama` (mono 10.5px `#5C6778` ellipsis) + "N blocks" + "0.1 BTC" + tarih + çöp butonu.
- **06 · Custom Blocks (BETA)**: başlıkta amber BETA rozeti (`#E8B44C`, border `rgba(232,180,76,.35)`, bg %7). Açılır. İçerik: açıklama paragrafı, Label input, doğal dil textarea, primary "Claude'a yazdır" (loading spinner'lı). Üretim sonrası kod kartı: bg `#0A0E16`, border `rgba(46,212,126,.3)`; üst satırda "✓ AST VALID" rozeti + `slug.py` (mono) + yeşil "Kaydet ve Kullan" butonu; altında `<pre>` Python kodu (mono 11.5px, lh 1.6, `#B7C2D4`). Kaydedilen bloklar listesi + Sil.
- **Sağ kolon — Strategy Preview**: strateji adı (mono 13px/600, ortalı) → "ENTRY SIGNALS · OR" micro-label → entry chip'leri (yeşil kenarlı kartlar: ad 11.5px/600 `#2ED47E`, altında param mono 9.5px) → aşağı ok SVG (`#3A465C`) → order düğümü (border `#2A3550`, bg `#151C2B`, radius 8: "MARKET order + bracket SL/TP" mono 11px `#5EA0F6`, altında sizing özeti mono 9.5px) → ok → "EXIT SIGNALS · OR" + exit chip'leri (kırmızı `#F79090`). Boş durumlar: kesik çizgili (`1px dashed #232C3E`) placeholder kutusu. Panel draft'lar değiştikçe canlı güncellenir.
- **Sağ kolon — Block Reference**: başlık sağında seçili tip (mono `#5EA0F6`). Blok etiketi (13px/600), yardım metni (12px `#97A1B5`), "PARAMETERS" listesi: her satır param adı (mono 11.5px `#5EA0F6`) + aralık `min – max (default d)` veya enum seçenekleri (mono 10.5px `#5C6778`), alt çizgi `#171E2B`.

### 4. Backtest
`grid-template-columns:3fr 2fr; gap:16px`, sağ kolon sticky.
- **01 · Select Strategy**: Strategy select (mono, katalogdan `name · N blocks · id=…`), Data select (BTC / US Index), primary "Run Backtest" (loading'de spinner + "running BacktestEngine…"). Data=Index seçilirse ikinci satır açılır: Ticker, Granularity, Start, End (`2fr 1fr 1fr 1fr` grid).
- **Result paneli** (fade-in): başlık "RESULT · {name}". 5'li KPI grid'i (iç kart stili; Realized PnL renkli, Max drawdown `#F79090`, değer mono 17px/600). **Equity curve**: 760×210 SVG, yeşil tema (`#2ED47E` çizgi, %20→0 gradient alan), 3 gridline, solda Y etiketleri. **Drawdown**: 760×50 kırmızı alan, başlıkta `min -X.X%`. **Monthly returns** ısı haritası: `44px + repeat(12,1fr)` grid, gap 3; üst satır ay harfleri (J F M A M J J A S O N D, mono 9px); yıl satırları 2019–2026 (2026 Temmuz'a kadar, kalan hücreler `#0D1119`); hücre 22px, radius 3, bg `rgba(46,212,126,α)` pozitif / `rgba(242,92,92,α)` negatif (α = min(|v|/7,1)×0.5+0.08); |v|>2.2 ise hücrede `+N` değeri (mono 8.5px `#E9EDF4`); `title` tooltip "Mar 2024 · +4.12%".
- **Sağ kolon — Order Flow Pipeline**: dikey stepper; her adım 9px halka (2px renkli border) + 1px `#232C3E` bağlantı çizgisi + başlık (mono 12px/600) + alt metin (11px `#5C6778`). Adımlar ve renkleri: `ComposedStrategy.on_bar(bar)` `#5EA0F6` → `order_factory.market(...)` `#5EA0F6` → `submit_order(order)` `#97A1B5` → `RiskEngine` `#97A1B5` → `SimulatedExchange · YAHOO` `#97A1B5` → `ExecutionEngine` `#2ED47E`.
- **Sağ kolon — Run History**: satır = saat (mono 10.5px `#5C6778`) + strateji adı (mono 11.5px `#5EA0F6`, ellipsis) + PnL (mono 11.5px/600 renkli). Son 6 koşu.

## Interactions & Behavior
- **Nav**: tıklamada sayfa geçişi (mevcut uygulamada gerçek route'lar). Hover bg `#131A28`, geçişler ~150ms.
- **Loop Start/Stop**: Start → buton disabled/%35 opacity, durum rozeti RUNNING'e döner, Dashboard nav'ında yeşil nokta belirir; iterasyonlar üstten `nlFade` (opacity 0→1 + translateY(-4px), .3s ease) ile eklenir; durum metni "proposing params… / running BacktestEngine… / extracting metrics…" arasında döner. Gerçekte: HTMX 2s polling (`/fragments/loop_status`, `/fragments/iterations`, `/fragments/best`) + 4s equity fetch.
- **AI önerisi / Custom block üretimi**: butonda inline spinner + "Claude düşünüyor…"; yanıt gelince form alanları ve draft listesi dolar (OOB swap) + başarı toast'ı.
- **Toast**: sağ üstte fixed (top 70, right 22), panel bg, radius 9, `0 8px 28px rgba(0,0,0,.45)` gölge; başarı `✓` yeşil border `rgba(46,212,126,.4)`, hata `✗` kırmızı `rgba(242,92,92,.4)`; ~3.2s sonra kaybolur. Hata örnekleri: "En az bir sinyal bloğu gerekli.", "slow > fast olmalı.", "atr_stop bloğu yalnızca exit rolünde kullanılabilir."
- **Accordion'lar** (03.5, 06): başlık tıklanınca açılır, `▼/▲` göstergesi.
- **Backtest Run**: 1–2s loading, sonuç paneli fade-in, Run History'ye satır eklenir.
- **Focus**: tüm input/select/textarea focus'ta border `#3672E8`.
- **Hover**: tablo/liste satırları `#131A28`; primary butonlar `brightness(1.1)`; çöp ikonları kırmızıya döner.

## State Management
Mevcut backend zaten kaynak: `state.py` (iterasyon geçmişi), `loop_runner.py` (döngü), catalog JSON. UI state'i:
- `page` (route), `loop.running/status/iterCount`, `iterations[]`, `best`
- Composer: `name, tradeSize, description, blockType, role, params{}, drafts[], advancedOptions{}, catalog[]`
- Custom blocks: `label, description, generated{name,code}, customBlocks[]`
- Backtest: `specId, dataKind, running, result{kpis, equityCurve, ddCurve, monthlyReturns}, runs[]`
- `toast {text, kind}` (3.2s auto-dismiss)

## Design Tokens
Renkler (`app.css :root` için önerilen isimler):
```css
--bg-950:#0A0D14;   /* sayfa */
--bg-900:#0D1119;   /* sidebar/topbar */
--panel:#10151F;    /* panel yüzeyi */
--panel-2:#151C2B;  /* vurgulu iç yüzey / secondary btn */
--inset:#0A0E16;    /* input & iç kart zemini */
--border:#1C2433;  --border-soft:#171E2B;  --border-input:#232C3E;  --border-btn2:#2A3550;
--accent:#5EA0F6;  --accent-strong:#3672E8;  --accent-grad-top:#4A8CF0;
--pnl-up:#2ED47E;  --pnl-down:#F25C5C;  --pnl-down-soft:#F79090;  --amber:#E8B44C;
--text:#E9EDF4;  --text-2:#97A1B5;  --muted:#5C6778;  --code:#B7C2D4;
```
Tipografi: `--sans:'IBM Plex Sans'` (400/500/600/700), `--mono:'JetBrains Mono'` (400/500/600/700). Ölçek: micro-label 9–10px uppercase ls .14–.16em; gövde 12–13px; mono veri 10.5–12px; KPI 17px (backtest) / 23px (dashboard); tüm sayısal değerler `font-variant-numeric: tabular-nums`.
Boşluk: kart grid gap 12, ana grid gap 16, panel padding 16–18, panel header `12px 16px`, satır padding `10–12px 16px`, form gap 12–13.
Radius: panel 10, iç kart/kod kartı 7–8, input/buton 6–7, rozet 4, ısı hücresi 3.
Gölge: toast `0 8px 28px rgba(0,0,0,.45)`; aktif nav `inset 0 0 0 1px rgba(94,160,246,.3)`.
Animasyonlar: `blink` (opacity .25 @50%, 1.4s), `fade` (opacity+4px yukarı, .3s ease), `spin` (.8s linear), AI panel border nabzı (2.6s ease-in-out).

## Assets
- Google Fonts: IBM Plex Sans, JetBrains Mono (link `base.html`'de).
- Tüm ikonlar inline SVG'dir (nav ikonları, play/stop, disket, çöp, yıldız, oklar) — prototip dosyasından kopyalanabilir; harici ikon seti yok.
- Logo: 30×30 gradient kare içinde çeyrek-daire "nautilus" işareti (prototipteki SVG).

## Files
- `Nautilus Lab.dc.html` — üç ekranın tamamını içeren etkileşimli hifi prototip. Template bölümündeki inline stiller ölçülerin birincil kaynağıdır; `renderVals()` içindeki mantık davranış referansıdır (validasyon mesajları, durum metinleri, grafik path üretimi).
