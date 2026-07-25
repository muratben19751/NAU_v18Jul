---
title: Strategy Studio
status: standalone package — pending merge into nautilus_web_app
updated: 2026-07-25
tags: [ui, strategy, backtest, optimize, ai-loop, deploy]
related: ["[[backtest]]", "[[optimize]]", "[[llm-loop]]", "[[live]]", "[[universe]]"]
---

# Strategy Studio

## TL;DR

Görsel strateji kurucu: kural ağacı (regime → entry → exit → risk → allocation)
tek bir `StrategyDefinition` JSON dokümanında yaşar; UI onu render eder, LLM
onu düzenler, compiler onu nötr `CompiledStrategy`'ye çevirir. Draft → Save
(append-only versiyon) → Backtest → Optimize → AI loop → gated Deploy akışının
tamamı çalışır durumda; motorlar (Nautilus runner, walk-forward optimizer,
LLM client) stub'larla ayakta ve 5 INTEGRATION POINT'ten gerçekleriyle
değiştirilir. 82 test + Ruff temiz.

## Mental Model

- **Tek doğruluk kaynağı:** `StrategyDefinition`. Başka hiçbir yerde strateji
  durumu tutulmaz. UI = bu dokümanın görüntüsü, AI diff'i = bu dokümana yama.
- **Tek mutasyon yolu:** İnsan edit'i de AI edit'i de `mutations.py`'den
  geçer. AI, senin yazamayacağın hiçbir şeyi yazamaz (aynı bounds, aynı
  doğrulama).
- **Draft ≠ Version:** Her edit `strategy_drafts`'a gider; Save,
  `strategy_versions`'a append-only terfi ettirir (`parent_version` zinciri).
  Deploy **her zaman kayıtlı versiyonu** derler, asla draft'ı değil.
- **Guardrail'ler sunucuda:** compile hatası, min-trade altı, OOS objective
  kötüleşmesi → AI önerisi auto modda bile reddedilir. Deploy gate'i
  (OOS DSR ≥ 0.8) ve sweep limiti (`OPTIMIZER_MAX_RUNS`) de sunucu tarafında.
- **Bloklar genişler, makine değişmez:** `sub_entry`/`sub_exit` (regime ELSE
  substrategy'si) sadece iki yeni blok adı — aynı endpoint'ler, aynı sweep,
  aynı AI yolu.

## Key Files

| Dosya | Rol |
|---|---|
| `app/studio/schema.py` | StrategyDefinition + Param/OptimizeRange/RegimeBranch/SubStrategy/AllocationBlock |
| `app/studio/mutations.py` | Tüm edit operasyonları (insan + AI ortak yol) |
| `app/studio/compiler.py` | `compile_strategy()` → CompiledStrategy; CompileError(rule_id) ile UI işaretleme |
| `app/studio/store.py` | SQLite: versions, drafts, runs, optimize_runs, ai_*, deployments |
| `app/studio/registry.py` | 15 indikatörlük kayıt (param sınırları + izinli operatörler) — `impl=None` |
| `app/studio/backtest.py` | `BacktestAdapter` protokolü + deterministik stub |
| `app/studio/optimizer.py` | Grid stub; param adresleme `"<rule_id>.<param>"` / `"risk.<field>"` |
| `app/studio/ai.py` | Suggestion kontratı, prompt, parse (1 retry), guardrail'ler, loop |
| `app/studio/deploy.py` | Gate + artifact; `_stub_runner_pickup` yaşam döngüsünü simüle eder |
| `app/main.py` | Tüm route'lar (merge'de APIRouter'a çevrilecek) |
| `templates/studio/`, `static/studio.{css,js}` | HTMX UI; OOB side-panel senkronu, sekme durumu korunur |
| `docs/studio.md` | Endpoint haritası + AI kontratı (detay burada, tekrar etme) |

## Invariants

1. Entry bloğu ≥ 1 kural; son aktif enstrüman kapatılamaz.
2. Geçersiz edit draft'a **hiç yazılmaz** (422 + banner); optimize aralığı
   değer edit'inde korunur.
3. `optimized_params()` adreslemesi (`"<rule_id>.<param>"`) optimizer
   sonuçlarının Apply'ı için sabittir — gerçek optimizer'a geçerken koru.
4. AI Suggestion tek JSON nesne; parse 1 retry (hata geri beslenir);
   reddedilen rationale'ler sonraki prompt'a girer (tekrar önlenir).
5. Deployment durum makinesi: pending → running → paused ⇄ running → stopped
   (terminal). Geçişler sunucuda doğrulanır.
6. Ranked allocation `top_n ≤ aktif enstrüman sayısı` — compile-time kontrol.

## Gotchas

- **Stub metrikler config hash'inden deterministik** — UI/test için ideal,
  trading kararı için anlamsız. Gerçek adapter takılana dek DSR'lara bakma.
- Fixture'ın tam sweep'i ~1.17M run → optimize butonu bilerek 422 verir;
  önce toggle'larla daralt. Bu bug değil, limit guard'ının kendisi.
- Sandbox'ta arka plan uvicorn bash çağrıları arasında ölüyor; canlı testler
  tek çağrıda yapıldı (repoda sorun yok).
- `python-multipart` şart (Form parametreleri); pip'te `--break-system-packages`.
- Side-panel OOB refresh'i sekmeyi artık koruyor (`applyTab`) — ama pane içi
  scroll pozisyonu korunmuyor.
- Regime `substrategy_id` alanı rezerve (kayıtlı stratejiyi referanslama) —
  şimdilik sadece inline `substrategy` derlenir.

## Integration Points (merge checklist)

1. `registry.py` → indikatör `impl`'lerini kendi feature fonksiyonlarına bağla.
2. `backtest.py` → `NautilusBacktestAdapter` + `to_nautilus(CompiledStrategy)`.
3. `optimizer.py` → kendi walk-forward optimizer'ın (adreslemeyi koruyarak).
4. `ai.py` → mevcut LLM loop'unun istemcisi (`HttpAnthropicClient` yerine).
5. `deploy.py` / `_stub_runner_pickup` → gerçek live/sim TradingNode hand-off;
   pickup'ta satırı `running`'e çek.

Ayrıca: route'ları APIRouter'a taşı, `StrategyStore(DB_PATH)`'i uygulama
SQLite'ına yönlendir (tablolar additive).

## Open Questions

- [ ] Substrategy'nin kendi risk override'ları olmalı mı, yoksa ana RISK
      bloğunu paylaşmak yeterli mi? (Şimdilik paylaşıyor.)
- [ ] Allocation `inverse_volatility` ağırlıklaması gerçek motorda hangi
      vol penceresiyle hesaplanacak?
- [ ] AI loop'ta auto-accept eşiği sadece "OOS iyileşti" mi kalmalı, yoksa
      minimum delta (örn. DSR +0.02) mı istenmeli?
- [ ] Deploy artifact'inin runner tarafında şema versiyonlaması gerekecek mi?
- [ ] QA prompt'u (negatif test turu) gerçek entegrasyon sonrası koşulacak.
