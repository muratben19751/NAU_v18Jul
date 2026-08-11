# NautilusTrader Otonom Backtest Ajanı

Bybit klines (spot/linear/inverse), US-index tick'leri ve harici salt-okunur Nautilus katalogları (NAU_ev — 591 US equity) üstünde çalışan görsel strateji besteleyici + otonom backtest ajanı. **Claude** hem hazır blokları birleştirerek tam strateji önerir hem de doğal dille tarif ettiğiniz yeni sinyal bloklarını (Custom Blocks) Python koduna çevirip runtime'da sisteme dahil eder — Nautilus katmanına dokunmadan. Tüm backtest'ler öldürülebilir alt-süreçlerde koşar (sunucu asla donmaz); robustness/WFO süiti çekirdeklere paralel dağıtılır (~8.7×).

## Kurulum

Python **3.12** gerekir (`requires-python = ">=3.12,<3.13"`). Depo boş olmayan
bir dizine de kurulabilir (`git init` + `git fetch` + `git checkout -t` akışı —
`git clone` boş olmayan dizini reddeder).

```bash
# Windows (PowerShell) — depo kökünde:
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"        # bağımlılıklar pyproject.toml'da (tek desteklenen nautilus_trader sürümü: 1.230.0)

# macOS/Linux:
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

### LLM erişimi (iki seçenek)

`agent.py` LLM backend'ini `NAUTILUS_LLM_BACKEND` env var'ı ile seçer (`auto` | `api` | `claude-cli`, varsayılan `auto`):

1. **Claude aboneliği (API key gerekmez):** Makinede [Claude Code](https://claude.com/claude-code) kuruluysa ve abonelikle giriş yapıldıysa (`claude` komutu PATH'te), LLM çağrıları `claude -p` headless modu üzerinden aboneliğinden yapılır. `auto` modda `ANTHROPIC_API_KEY` yoksa otomatik bu yol seçilir. CLI farklı bir yoldaysa `NAUTILUS_CLAUDE_CLI` ile tam yolu ver.
2. **API key:** `ANTHROPIC_API_KEY` env var'ı (veya `~/.nautilus_proxy_key` dosyası) ayarlıysa anthropic SDK ile **doğrudan resmi API'ye** (`https://api.anthropic.com`) çağrı yapılır. Araya bir proxy/gateway koymak istersen `ANTHROPIC_BASE_URL`'i AÇIKÇA ayarla; ayarlıysa hem `agent.py` hem Strategy Studio'nun AI katmanı aynı uca konuşur, ve o uç yanıt vermezse hata jenerik bir bağlantı hatası değil "LLM proxy yanıt vermiyor: `<url>`" der.

İkisi de yoksa agent LLM adımları fallback'e (rastgele öneri) düşer.

## Ortam değişkenleri

| Değişken | Varsayılan | Ne yapar |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | LLM için API anahtarı (yoksa `claude-cli` aboneliği denenir) |
| `ANTHROPIC_BASE_URL` | resmi API | Anthropic ucu — yalnız açıkça ayarlanınca proxy/gateway kullanılır (agent.py + Studio AI aynı ucu okur) |
| `NAUTILUS_LLM_BACKEND` | `auto` | `auto` \| `api` \| `claude-cli` — LLM backend seçimi |
| `NAUTILUS_CLAUDE_CLI` | `claude` | Claude Code CLI tam yolu (PATH'te değilse) |
| `NAUTILUS_PARALLEL` | `1` | `0` = robustness/WFO süreç havuzunu kapat (sıralı yol) |
| `NAUTILUS_PARALLEL_WORKERS` | `cpu//2-2` | Havuz işçi sayısı (clamp [1, 28]) |
| `NAUTILUS_DEBUG_LOG` | kapalı | `1` = Nautilus iç loglarını aç (emir redleri, sessiz strateji hataları) |
| `NAUTILUS_EXTERNAL_CATALOGS` | NAU_ev yolu | Harici salt-okunur ParquetDataCatalog kökleri (`os.pathsep` ayraçlı) |
| `NAUTILUS_INDEX_ROOT` | — | US-index tick CSV kökü (Polygon-tarzı) |

## Çalıştırma

```bash
# Prod / kesintisiz üretim (önerilen): reload YOK
uvicorn server:app --host 127.0.0.1 --port 8000

# Geliştirme (auto-reload) — wiki/skill/test dizinlerini hariç tut
uvicorn server:app --host 127.0.0.1 --port 8000 --reload \
    --reload-exclude "$PWD/nautilus_wiki" --reload-exclude "$PWD/.claude" \
    --reload-exclude "$PWD/tests"
```

> **Not:** `--reload` izlenen ağaçtaki her `*.py` değişiminde sunucuyu yeniden
> başlatır. Strateji üretimi (`/backtest` → doğal dil) ~15-20 sn süren bir
> worker thread'de çalışıp ilerleme durumunu **bellekte** tutar; bu sırada
> izlenen bir `.py` değişirse (kod düzenlemesi / otomatik biçimlendirme) sunucu
> yeniden başlar ve üretim paneli kaybolur. `--reload-exclude`'a **mutlak dizin
> yolu** verin (`$PWD/…`) — uvicorn göreli adı veya glob'u eşleştirmez, yalnızca
> var olan bir dizini recursive dışlar. Uzun üretimler için `--reload`'sız
> çalıştırın.

- **`/`** (dashboard) — Otonom legacy döngüyü başlat/durdur; iterasyonlar canlı akar.
- **`/agent`** — Otonom backtest ajanı (canlı Gantt zaman çizelgesiyle).
- **`/strategy`** — Görsel strateji composer + Custom Blocks.
- **`/backtest`** — Stratejiyi doğal dille tarif et → Claude yeni sinyal blok(ları) yazar → seçilen zaman dilimlerinin **hepsinde** backtest (2+ TF → karşılaştırma tablosu, 1 TF → tam sonuç + equity). Sembol yaz-bul (datalist typeahead). Kayıtlı stratejileri `/strategy` composer'da kur.
- **`/data`** — Instrument catalog (Bybit + US-index + harici NAU_ev kataloğu).

## Sanity check

```bash
python -m pytest tests -q      # tam birim test paketi
python .claude/skills/run-nautilus-web-app/driver.py --port 8199   # uçtan uca: sunucu + gerçek backtest
```

## Mimari

| Modül | Sorumluluk |
|---|---|
| `data.py` | yfinance BTC-USD max, parquet cache |
| `strategies.py` | MA crossover + RSI mean-reversion (otonom loop için legacy Strategy'ler) |
| `composer.py` | `BLOCK_REGISTRY`, `ComposedStrategy` (Nautilus `Strategy` subclass), spec I/O |
| `custom_block_store.py` | Custom bloklar için disk I/O (`~/.cache/nautilus_web_app/custom_blocks/`) |
| `backtest.py` | `BacktestEngine` sarmalayıcı, CASH ↔ MARGIN venue anahtarı, metrik çıkarımı |
| `agent.py` | Claude Fable 5 — parametre önerisi, tam strateji önerisi, **custom block kod üretimi + AST validation** |
| `loop_runner.py` | Otonom döngü arka plan thread'i |
| `state.py` | Thread-safe iterasyon geçmişi |
| `server.py` + `web/routes/` | FastAPI + Jinja2 + HTMX |

## Strategy Composer

**`/strategy`** sayfası şu adımlarla çalışır:

1. **02 · Add Signal Block** — Katalogtan bir blok tipi seç (`ma_cross`, `rsi_threshold`, `bollinger_break`, ...), rol (entry/exit) ve parametreleri gir → **＋ Add block** ile drafts'a ekle.
2. **03.5 · Advanced Options** (opsiyonel) — Entry/Exit logic (OR/AND), order type (market/limit), bracket SL/TP (percent veya ATR), allow_short, sizing modu (fixed / percent_equity / atr_target).
3. **04 · Save to catalog** — Spec `~/.cache/nautilus_web_app/strategy_catalog.json` dosyasına yazılır ve `/backtest` sayfasında kullanılabilir hale gelir.

**AI Strategy Designer** butonu Claude'un mevcut katalog + backtest geçmişine bakarak yeni bir strateji önermesini sağlar; blocks + drafts + advanced options tek seferde OOB-swap ile doldurulur.

## Custom Blocks (Beta)

**`/strategy` → 06 · Custom Blocks (Beta)** paneli built-in 13 blok tipini (`ma_cross`, `rsi_threshold`, `price_breakout`, `momentum`, `volume_spike`, `ema_cross`, `bollinger_break`, `macd_cross`, `atr_stop`, `adx_threshold`, `stoch_rsi_cross`, `wave_trend_cross`, `donchian_channel`) yeterli bulmadığınızda yeni bir tip yaratmanızı sağlar:

1. Bir **label** (kısa isim — bloğun dropdown'da görüneceği ad) ve doğal dille bir **açıklama** yaz (örn. "close 100-günlük SMA'yı yukarı keserse LONG").
2. **🧠 Claude'a yazdır** — Claude Fable 5 JSON şemasına uygun bir blok üretir: `{name, meta: {label, params, help}, code}`. Kod tek bir `evaluate(state, block, closes, indicators, portfolio)` fonksiyonudur.
3. **AST whitelist** kod üzerinde katı denetim yapar:
   - Yasak: `import`, `try/except`, `with`, `lambda`, `global`, `raise`, dunder isim/attribute, `eval`/`exec`/`open`/`__import__`.
   - İzinli: math + statistics + whitelisted builtins (`abs, min, max, sum, len, round, sorted, range, ...`) + bilinen attribute'lar (`.params, .role, .value, .upper, .lower, .initialized, .get, .append, .pop, ...`).
   - Kısıtlı `__builtins__` ile smoke-exec yapılır; hata olursa Claude'a **bir kez** düzeltme fırsatı verilir.
4. **💾 Kaydet ve Kullan** — Blok `~/.cache/nautilus_web_app/custom_blocks/{slug}.py` olarak disk'e yazılır ve `BLOCK_REGISTRY`'ye eklenir. Slug etiketten türetilir (`_slugify`), Claude'un öneri adı yok sayılır — bloğunuz her yerde birebir yazdığınız etiketle görünür.
5. Sunucu yeniden başlatıldığında `_load_custom_blocks()` disk'ten tüm custom blokları yeniden yükler. Bozuk dosyalar skip edilir, kalan katalog etkilenmez.

### `evaluate` sözleşmesi

```python
def evaluate(state, block, closes, indicators, portfolio):
    # state       — mutable dict, bar'lar arası kalıcı, block-idx başına scoped
    # block       — .params (kullanıcının UI'da girdiği değerler), .role, .type
    # closes      — list[float], oldest-first (kapanışlar)
    # indicators  — {"highs": [...], "lows": [...], "volumes": [...]}  (hepsi closes ile hizalı)
    # portfolio   — .is_net_long(id) / .is_net_short(id) / .is_net_flat(id)
    return "long" | "short" | "exit" | None
```

Custom bloklar **tam OHLCV** görür: `closes` + `indicators["highs"]` + `indicators["lows"]` + `indicators["volumes"]` (dördü de hizalı float liste; open verilmez — "önceki kapanış" için `closes[i-1]`). High/low mevcut olduğu için gerçek OHLC indikatörleri hesaplanabilir: **ATR, ADX/DMI, Stochastic, Donchian/Keltner, WaveTrend, SuperTrend** + hacim mantığı (volume spike, OBV). Built-in katalog RSI/EMA/MACD/Bollinger/ATR-stop/volume_spike'ı zaten kapsar — custom blok yalnız bunların dışı için. Çoklu-indikatör konfluans (RSI+ADX+ATR gibi) **serbesttir** — tümü AND'lenebilir; tek maliyet sinyal sıklığı, gevşek eşiklerle telafi edilir (az-trade bloklar sıralamada zaten elenir). Bir sadeleştirme yapılırsa nedeni `meta.help`'e yazılır ve agent ekranında + /sessions replay'inde görünür.

## Advanced Options — Nautilus özellikleri

Composer aşağıdaki Nautilus özelliklerini opsiyonel olarak açar (Nautilus'un kendisi değiştirilmez):

- **AND/OR entry/exit logic** — birden fazla bloğu birleştirmek için
- **Order type** — market ya da limit (`limit_offset_bps` ile)
- **Bracket** — atomik SL/TP (`order_factory.bracket` → `submit_order_list`), SL/TP percent ya da ATR
- **Allow short** — SELL girişleri, backend otomatik olarak `AccountType.MARGIN`'a geçer (netting)
- **Sizing modu** — fixed / percent_equity / atr_target (ATR-target risk % başına)
- **Multi-timeframe trend filtresi** — ana TF'de işlem + üst TF'de EMA trend onayı (ör. 30dk işlem + 1d trend); `trend_filter` / `trend_interval` / `trend_ema_period` spec alanları. `ComposedStrategy` ikincil bar feed'ini subscribe eder; look-ahead güvenli (üst-TF barı yalnız kapandığında değerlendirilir). Trend TF ana TF'den yüksek olmalı; değilse motor filtreyi atlar.

Bkz. `composer.py::ComposedStrategy` ve `agent.py::_STRATEGY_OPTION_DEFAULTS`.

## Otonom Loop (Legacy)

Ana sayfadaki (`/`) Başlat/Durdur, `loop_runner.py`'yi kontrol eder — Claude Fable 5 `ma_crossover` / `rsi_mean_reversion` için parametre önerir, `backtest.py::run_backtest` çalıştırır, `state.py` içindeki iterasyon geçmişini günceller, UI HTMX ile canlı akar. Bu path composer'dan bağımsızdır ve sadece iki hazır strateji üzerinde çalışır.

## Veri kaynakları

- **Bybit v5 klines** — `data.py::load_bybit_bars`; per (kategori, sembol, interval) parquet cache (`~/.cache/nautilus_web_app/bybit/`), art-arda çağrılarda ileri-doğru genişler.
- **US-index tick CSV'leri** — `NAUTILUS_INDEX_ROOT` altındaki Polygon-tarzı günlük gzip'ler; tick→OHLCV resample.
- **Harici Nautilus katalogları** — `NAUTILUS_EXTERNAL_CATALOGS` (varsayılan: NAU_ev, 591 US equity); salt-okunur, yerinde okunur, asla kopyalanmaz/yazılmaz. Bu projenin kendi ingest kökü `~/.cache/nautilus_web_app/equity_catalog` var olduğunda listeye otomatik eklenir.
- **Kendi equity ingest'i (flat-file)** — `python ingest_equities.py --tickers AA,XYZ --years 2003-2026`: `E:\MarketData` Massive/Polygon flat-file arşivinden (~12.400 ticker) 1-dakikalık bar + RTH TF'ler (5m/15m/1h/4h/1d) üretir, `equity_catalog`'a yazar (UNADJUSTED — manifest bayrağıyla işaretli). Başka external katalogda olan ticker atlanır (`--force` zorlar).
- **Flat-file aynası (S3)** — `$env:MASSIVE_S3_KEY=...; $env:MASSIVE_S3_SECRET=...; python download_flatfiles.py --dataset us_indices/minute_aggs_v1`: Massive'in S3 ucundan (`https://files.massive.com`, bucket `flatfiles`) günlük `.csv.gz` ağacını `E:\MarketData\massive-flatfiles` altına yerel key yoluyla birebir indirir; `.part` + `os.replace` ile yarım dosya bırakmaz, yeniden koşumda boyutu tutan dosyayı atlar (`--dry-run` yalnız boyut bildirir). **Dikkat: listeleme ile indirme ayrı yetkiler** — 2026-08-03 ölçümü: aynı kimlik bilgileriyle `us_indices/` 200, `us_stocks_sip/` (ve crypto/forex/futures/options) **403 `NOT_AUTHORIZED`**; 403 ağ hatası gibi yeniden denenmez, koşum açık mesajla durur.
- **Günlük "tüm US hisseleri" (REST grouped)** — `$env:MASSIVE_API_KEY=...; python download_grouped_daily.py --date 2026-08-05` (argümansız: dün; `--start/--end` aralık, `--days N` son N hafta içi gün): S3'te 403 dönen `us_stocks_sip` gününün aynı içeriğini **tek çağrıda** `/v2/aggs/grouped` ucundan çeker (~12.400 ticker) ve flat-file aynasıyla aynı yol/şemaya yazar — `us_stocks_sip/day_aggs_adjusted_v1/YYYY/MM/YYYY-MM-DD.csv.gz`, `--unadjusted` ile `day_aggs_v1`. `.part` + `os.replace`, var olan gün atlanır (`--overwrite` zorlar), tatil/hafta sonu `resultsCount=0` → hata değil, atlanır. **Tek gün için adjusted ≡ unadjusted** (ölçüm 2026-08-05: 12.392 ticker'ın 0'ında fiyat farkı; `adjusted` geçmişi bugüne göre düzeltir). Hacim gün içinde oturmaz: aynı günün iki çağrısı arasında 721 ticker'ın `volume`'ü değişti (geç raporlanan trade'ler). **Bu uçta plan kısıtı yok** (ölçüm 2026-08-06: geçmiş 2003-09-10'a kadar açık — 09-09 boş döner, verinin tabanı; 10 ardışık çağrıda 429 yok): `--all --rpm 0 --workers 8` tüm geçmişi (2003-09-10 → dün, 5.737 işlem günü, 49,1 M satır, 0,97 GB) 15 dk'da indirir. **`adjusted=true` yalnız split'i düzeltir, temettüyü düzeltmez** (ölçüm 2026-08-06: NVDA 10:1 öncesi adjusted = unadjusted/10; AAPL'ın üç ex-temettü gününde fark 0,0000) — yani seri fiyat getirisidir, toplam getiri değil. `--rpm` verilirse tavan koşum başınadır, işçi başına değil.
- **Kendi equity indiricisi (REST)** — `$env:MASSIVE_API_KEY=...; python download_massive.py --tickers HOOD,RIVN --years 2025-2026`: yerel arşiv gerekmez, veriyi Massive (Polygon) `/v2/aggs` ucundan çeker; aynı katalog, aynı right-label sözleşmesi, aynı TF/manifest fazları — ama **ADJUSTED** (`--unadjusted` kapatır). Anahtar yalnız ortam değişkeninden okunur. Plan penceresi dışı yıllar koşumu düşürmez, "plan dışı" diye loglanıp atlanır; `--rpm` istek tavanını yönetir (ücretsiz plan: 5/dk, ~2 yıl geçmiş → `--rpm 5`; ücretli anahtarda `--rpm 0`). **Yeni ölçüm (2026-08-06, mevcut anahtar): bu iki kısıt artık yok** — dakika-bar geçmişi de **2003-09-10**'a kadar açık (AAPL 2003-09-10: 414 bar) ve 10 ardışık çağrıda 429 görülmedi (~90/dk); 2026-08-03'teki "5/dk, ~2 yıl" ölçümü o günkü plan içindi, `--rpm 0` artık doğru varsayılan. Çok günlük pencerede sayfa başı 50.000 bar tavanı `next_url` ile aşılıyor (ölçüm: AAPL 2026-01→07 tek istekte 50.000 + devam sayfası).

## Güvenlik notları

> **Localhost-only varsayımı:** Uygulamada auth / CSRF / rate-limit / CSP **bilinçli olarak yok** (tek kullanıcı, 127.0.0.1). Sunucuyu localhost dışına açmadan önce bu dördü **zorunlu** — bkz. backlog notu.

- Custom block kodu **asla `sys.path`'i kirletmez** — `importlib.util.spec_from_file_location` ile isolated modül yükleme.
- `exec` çağrısı kısıtlı `__builtins__` ile yapılır; `open`, `eval`, `exec`, `__import__` erişilemez.
- Her `evaluate` çağrısı `_eval_block` içinde try/except ile sarılı; başarısız blok o bar için `None` döner ve tek sefer log basılır.
- Tek kullanıcı varsayımı — store paylaşımlı, multi-tenant isolation yok.
