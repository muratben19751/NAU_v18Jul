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
2. **API key:** `ANTHROPIC_API_KEY` env var'ı (veya `~/.nautilus_proxy_key` dosyası) ayarlıysa anthropic SDK ile doğrudan (ya da `ANTHROPIC_BASE_URL` proxy'si üzerinden) çağrı yapılır.

İkisi de yoksa agent LLM adımları fallback'e (rastgele öneri) düşer.

## Ortam değişkenleri

| Değişken | Varsayılan | Ne yapar |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | LLM için API anahtarı (yoksa `claude-cli` aboneliği denenir) |
| `NAUTILUS_LLM_BACKEND` | `auto` | `auto` \| `api` \| `claude-cli` — LLM backend seçimi |
| `NAUTILUS_CLAUDE_CLI` | `claude` | Claude Code CLI tam yolu (PATH'te değilse) |
| `NAUTILUS_PARALLEL` | `1` | `0` = robustness/WFO süreç havuzunu kapat (sıralı yol) |
| `NAUTILUS_PARALLEL_WORKERS` | `cpu//2-2` | Havuz işçi sayısı (clamp [1, 28]) |
| `NAUTILUS_DEBUG_LOG` | kapalı | `1` = Nautilus iç loglarını aç (emir redleri, sessiz strateji hataları) |
| `NAUTILUS_EXTERNAL_CATALOGS` | NAU_ev yolu | Harici salt-okunur ParquetDataCatalog kökleri (`os.pathsep` ayraçlı) |
| `NAUTILUS_INDEX_ROOT` | — | US-index tick CSV kökü (Polygon-tarzı) |

## Çalıştırma

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

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
- **Harici Nautilus katalogları** — `NAUTILUS_EXTERNAL_CATALOGS` (varsayılan: NAU_ev, 591 US equity); salt-okunur, yerinde okunur, asla kopyalanmaz/yazılmaz.

## Güvenlik notları

> **Localhost-only varsayımı:** Uygulamada auth / CSRF / rate-limit / CSP **bilinçli olarak yok** (tek kullanıcı, 127.0.0.1). Sunucuyu localhost dışına açmadan önce bu dördü **zorunlu** — bkz. backlog notu.

- Custom block kodu **asla `sys.path`'i kirletmez** — `importlib.util.spec_from_file_location` ile isolated modül yükleme.
- `exec` çağrısı kısıtlı `__builtins__` ile yapılır; `open`, `eval`, `exec`, `__import__` erişilemez.
- Her `evaluate` çağrısı `_eval_block` içinde try/except ile sarılı; başarısız blok o bar için `None` döner ve tek sefer log basılır.
- Tek kullanıcı varsayımı — store paylaşımlı, multi-tenant isolation yok.
