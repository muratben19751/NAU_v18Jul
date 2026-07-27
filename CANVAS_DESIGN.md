# GÖREV: Strategy Studio — Canvas Görünümü (Konsept C)

> Node-tabanlı, zoom/pan + sürükle-bırak destekli tam ekran builder modu.
> Sol palet | orta canvas | sağ inspector. Mevcut form görünümü AYNEN kalır;
> canvas aynı stratejinin alternatif bir görselleştiricisidir.

## Genel Kurallar (her phase için geçerli)

- **Nautilus core'una ve site-packages'a asla dokunma.** Tüm değişiklikler bu
  repo içinde kalır. `nautilus_trader==1.230.0` pin'i ve `constants.NAUTILUS_REQUIRED`
  fail-fast kontrolü değişmez.
- **`StrategyDefinition` şeması tek doğruluk kaynağıdır ve DEĞİŞMEZ.**
  Canvas'a özgü hiçbir alan (node pozisyonu, zoom seviyesi vb.) şemaya eklenmez.
  Layout deterministik auto-layout ile her render'da yeniden hesaplanır.
- **Mevcut endpoint'ler yeniden kullanılır, yenisi ancak salt-okunur eklenir.**
  Tüm mutasyonlar `studio_app/docs/studio.md`'deki mevcut endpoint haritasından
  geçer: `POST …/blocks/{block}/rules`, `PATCH …/rules/{rule_id}`,
  `DELETE …/rules/{rule_id}`, `PATCH …/risk`, `PATCH …/blocks/{block}`,
  `PATCH …/allocation`, `POST …/save`, `POST …/discard`.
- **Mevcut studio dosyalarına minimum dokunuş:** `web/routes/studio.py`'ye
  yalnızca yeni GET route'ları eklenir; mevcut fonksiyon gövdeleri ve
  `web/templates/studio/*.html` partial'ları değiştirilmez. Var olan public
  fonksiyon imzaları kırılmaz; yeni parametreler keyword + default ile eklenir.
- **Build step yok.** Repo deseni korunur: Jinja template + `web/static/*.js`
  (vanilla) + gerekiyorsa CDN (unpkg/jsdelivr, base.html'deki mevcut desen).
  Bundler/npm eklenmez.
- Her phase bağımsız bir commit; **Verification** komutları geçmeden sonrakine
  GEÇME. Riskli değişikliklerde önce `git checkout -b canvas-phase-N`.
- Ruff hook'u otomatik çalışır (`.claude/settings.json`); manuel adım yok.

## Teknoloji Kararı

Serbest node editörü (React Flow / Drawflow) KULLANILMAZ. Gerekçe:
`StrategyDefinition` serbest bir graf değil, **kısıtlı bir ağaçtır**
(regime? → entry/exit RuleGroup → Rule'lar → risk → allocation). Kullanıcının
keyfi kablo çekmesi geçersiz stratejiler üretir; kenarlar şemadan türetilir,
kullanıcı çizmez. Bu yüzden:

- **Render:** Vanilla JS + SVG (`web/static/canvas.js`, yeni dosya).
  Auto-layout: katmanlı (layered) yerleşim — kolonlar soldan sağa:
  `instruments → [regime] → rules → group(AND/OR) → entry|exit → risk → [allocation]`.
- **Zoom/pan:** SVG `viewBox` manipülasyonu — wheel zoom (imleç merkezli),
  drag pan, toolbar (+/−/fit), pinch (touch). Harici lib gerekmez; ~150 satır.
- **Sürükle-bırak:** HTML5 drag — paletten sürüklenen indikatör, entry/exit/
  regime "drop lane"lerine bırakılır → mevcut `POST …/blocks/{block}/rules`
  çağrılır (HTMX `htmx.ajax` ile) → canvas yeniden render.
- **Inspector:** Sağ panel, seçili node'un türüne göre mevcut partial'ları
  HTMX ile yükler (`_rule.html`, `_risk_block.html`, `_param_card.html` …).
  Yani inspector = mevcut form parçalarının dar sütunda yeniden kullanımı.
  PATCH akışları olduğu gibi çalışır; OOB swap'ler canvas'ı da günceller.

## Veri Akışı

```
GET /studio/{id}/canvas          → page (base.html extend, 3 sütun layout)
GET /studio/{id}/canvas/graph    → JSON  {nodes:[], edges:[], meta:{}}   (YENİ, salt-okunur)
     └─ sunucu tarafında StrategyDefinition → graph dönüşümü (aşağıda)
mutasyonlar → MEVCUT endpoint'ler → HX-Trigger "studio:changed"
     └─ canvas.js dinler → /canvas/graph'ı yeniden çeker → re-render
```

### Graph dönüşümü (sunucu, yeni modül: `strategy_studio/graph.py`)

`to_graph(defn: StrategyDefinition) -> dict` — saf fonksiyon, yan etkisiz.

Node türleri ve kaynakları:

| node.kind      | Kaynak                                   | Inspector partial      |
|----------------|------------------------------------------|------------------------|
| `instrument`   | `defn.instruments` (yalnız `active`)     | `_instruments.html`    |
| `regime`       | `defn.regime` (varsa) + else dalı        | `_regime_block.html`   |
| `rule`         | `iter_rules()` çıktısı (block etiketli)  | `_rule.html`           |
| `group`        | entry/exit RuleGroup (match: all/any)    | `_rule_group.html`     |
| `filter`       | RuleGroup.filters ("skip if" — kesikli kenar) | `_rule.html`      |
| `risk`         | `defn.risk`                              | `_risk_block.html`     |
| `allocation`   | `defn.allocation` (varsa)                | `_allocation_block.html` |

Kenarlar türetilir: instrument→(regime|rules), rule→group, group→entry/exit,
entry/exit→risk, risk→allocation. `optimize` aralığı olan paramlı node'lar
`badge:"opt"` alır (Optimization paneliyle görsel köprü).

## Phase 1 — Salt-okunur canvas + zoom/pan

**Dosyalar:** `strategy_studio/graph.py` (yeni), `web/routes/studio.py`
(+2 GET route), `web/templates/studio/canvas.html` (yeni),
`web/static/canvas.js` (yeni), `web/static/canvas.css` (yeni).

- `to_graph()` + `/canvas/graph` JSON endpoint'i.
- `canvas.html`: header'a mevcut studio sayfasından "Canvas ⇄ Form" geçiş
  linki (mevcut `page.html`'e TEK satır link eklenebilir — tek istisna).
- SVG render + katmanlı auto-layout + wheel/drag/toolbar zoom-pan + fit-view.
- Node'lara tıklama henüz yok; hover'da başlık tooltip.

**Verification**
```bash
python -m pytest tests/ -q                      # mevcut süit yeşil kalmalı
python -c "from strategy_studio.graph import to_graph; from strategy_studio.store import *; import json"  # import temiz
# tests/test_graph.py (yeni): örnek StrategyDefinition → to_graph():
#  - her rule için tam 1 node, kenar sayısı deterministik
#  - regime'siz stratejide regime node'u yok
#  - filters kesikli kenar (edge.kind == "filter")
python -m pytest tests/test_graph.py -q
curl -s localhost:8000/studio/<id>/canvas/graph | python -m json.tool | head
```

## Phase 2 — Seçim + inspector (mevcut PATCH akışları)

- Node tıklama → sağ panel HTMX ile ilgili partial'ı yükler
  (`GET /studio/{id}/canvas/inspector/{node_id}` — YENİ, salt-okunur;
  içerde mevcut partial render fonksiyonlarını çağırır).
- `HX-Trigger: studio:changed` → canvas graph'ı yeniden çeker; seçim korunur.
- Seçili node vurgusu (2px accent border), Esc ile seçim iptali.

**Verification**
```bash
python -m pytest tests/ -q
# tests/test_canvas_inspector.py: inspector endpoint'i rule/risk/regime için
# 200 + doğru partial içeriği döner; olmayan node_id → 404
node .claude/studio_e2e.mjs   # mevcut e2e bozulmamalı (varsa canvas senaryosu ekle)
```

## Phase 3 — Palet + sürükle-bırak

- Sol palet: `INDICATOR_REGISTRY`'den kategori-gruplu (registry'deki
  `category` alanı), arama kutulu liste. Sunucudan render (Jinja), JS'e
  registry kopyalanmaz.
- Drag → entry/exit/regime lane'ine drop → `POST …/blocks/{block}/rules`
  (registry default'larıyla — mevcut davranış). Drop sonrası yeni node
  otomatik seçilir, inspector açılır.
- Node'da sil butonu → mevcut `DELETE …/rules/{rule_id}`
  ("entry ≥1 kural" sunucu kuralı zaten korur; 409'u toast ile göster).
- Save/Discard butonları header'da → mevcut `POST …/save`, `…/discard`.

**Verification**
```bash
python -m pytest tests/ -q
# e2e: palette'ten drop → rule sayısı +1; delete → −1; entry'de son kural
# silinemez (mevcut sunucu kuralı canvas'tan da doğrulanır)
node .claude/studio_e2e.mjs
```

## Phase 4 — Cila

- Minimap (sağ alt, viewBox oranıyla), klavye: +/−/0(fit)/ok tuşlarıyla pan.
- AI ghost önerileri: mevcut `_ghost.html` verisini canvas'ta yarı saydam
  node olarak göster; accept/dismiss mevcut endpoint'lere gider.
- Backtest/optimize sonuç rozetleri: `_footer_metrics.html` verisini
  header'a kompakt taşı (yeniden kullanım, kopya değil).
- README'ye "Canvas view" bölümü + ekran akışı.

**Verification**
```bash
python -m pytest tests/ -q && node .claude/studio_e2e.mjs
ruff check . && ruff format --check .
```

## Bilinçli Kapsam Dışı

- Serbest kenar çizimi (şema ağacı buna izin vermez — istenirse ayrı RFC).
- Node pozisyonlarının kalıcılığı (auto-layout deterministik; state yok).
- Form görünümünün kaldırılması (iki görünüm süresiz yan yana yaşar).
