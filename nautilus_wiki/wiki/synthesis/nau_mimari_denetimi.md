---
title: nautilus_web_app Mimari Denetimi (2026-07)
type: synthesis
summary: nautilus_web_app'in mimari denetimi — 45 ham bulgu, çekişmeli-doğrulama sonrası 41 doğrulanmış yapısal defekt. Baskın sorun: niyet düzeyinde kalan katmanlama + 4 tanrı-modül + fonksiyon-içi import döngüleri. Kritik yok.
key_concepts:
  - crash_only_design
  - backtesting_guide
sources:
  - https://github.com/muratben19751/NAU_v18Jul
related:
  - wiki/synthesis/webapp_module_map.md
  - wiki/synthesis/nau_performans_denetimi.md
last_updated: 2026-07-21
---

# nautilus_web_app Mimari Denetimi — 2026-07-21

`nautilus_web_app` repo kodunun (~22.5k satır) **mimari/yapısal** denetimi —
[[webapp_module_map]]'in yapı-odaklı tamamlayıcısıdır (o *ne nereye bağlanır*'ı verir,
bu *sınırlar nerede sızıyor ve neden*'i verir); [[nau_performans_denetimi]]'nin
(*ne yavaş*) mimari kardeşidir. 7 mimari boyut paralel lensle tarandı, ardından her
bulgu **çekişmeli doğrulayıcı** ("kod karşısında bunu çürüt", CONFIRMED/PLAUSIBLE/
REFUTED) ile denetlendi: **45 ham bulgu → 41 doğrulandı** (58 ajan, 0 hata).
Yalnız repo kodu incelendi; NautilusTrader kütüphanesine dokunulmadı.

## Genel değerlendirme

Kod çalışıyor ama sağlığı **niyet düzeyinde kalan katmanlama** tarafından belirleniyor
(Web → Domain → Engine → Data → Runtime): **her sınır sızıyor**. Katmanlama yalnız
bağımlılık enjekte edilen yerlerde tutuyor (`run_many` callable, `IterationResult` DTO);
bir modül-global'i veya underscore-private import ile erişilen her yerde çöküyor.
**Kritik bulgu yok** — hepsi yapısal/bakım hatası veya latent tehlike; sevk edilen yolda
canlı veri bozulması/güvenlik açığı yok.

## En yüksek 3 risk

| # | Bulgu | Modül | Efor |
|---|-------|-------|------|
| **H1** | Engine katmanı web'e *yukarı* uzanıyor — `sandbox.py` child'ı `web.routes.agent_backtest._IPC_Q` global'ini mutasyona uğratıp private `_run_full_robustness`'ı çağırıyor; ~250 satırlık engine orkestrasyonu bir HTTP route dosyasında | `sandbox.py` → `web/routes/agent_backtest.py` | M ⭐ |
| **H2** | WFO batch timeout'u (`WFO_BATCH_TIMEOUT_S=600`) 900s dış kill altında kümülatifi sınırlamıyor — WFO `run_many`'i GA-jenerasyonu başına çağırır (`n_gen=5` → ~3600s > 900s); yavaş-ama-sağlıklı suite hard-kill'de **tüm tamamlanmış iş çöpe** | `backtest_robustness.py` + `sandbox.py` | S ⭐ |
| **H7** | Web route'ları güvenlik-kritik codegen doğrulamasını `agent.py` private'larından import ediyor; **zaten canlı kırık**: `from agent import _has_builtin` (`strategy.py:440`) runtime `ImportError` atar (codegate'te isim `has_builtin`) | `web/routes/strategy.py`, `lab.py`, `agent.py`, `codegate.py` | S–M ⭐ |
| H9 | Global tek-instance `AppState` — tüm sunucu için tek `iterations`/`best`/`running`; session izolasyonu yok, ikinci `/loop/start` sessizce no-op | `state.py` | M |

## Diğer HIGH bulgular

- **H3** — "Backtest çalıştır" servis dikişi yok; Bybit/External/Index dispatch'i 5+ route
  worker'ında elle kopyalanmış ve **çoktan sapmış** (`/run` recipe `initial_capital`+
  `commission_bps` içerir, `/sweep` içermez) → tanrı-modül büyümesinin ana sürücüsü.
- **H4** — `backtest.py` (1978 LOC) tanrı-modül + `data.py` ile karşılıklı **lazy-import
  döngüsü** (aralarında sıfır modül-seviyesi import; hoist edilse `ImportError`). Data ile
  Domain ayrılabilir katmanlar değil.
- **H5** — `server↔routes` döngüsü **43 fonksiyon-içi `from server import`** ile örtülmüş;
  router'lar bağımsız import/mount/test edilemez.
- **H6** — `data.py` (1996 LOC): 4 kaynak + HTTP + NFS/awk subprocess + katalog write/wipe
  + `/data` UI satır kurma tek modülde; on-disk cache şeması ~8 route'a private helper'la sızmış.
- **H8** — Sıralı `optimize_window` ile paralel `_run_walk_forward_parallel` elle kopyalanmış
  GA döngüleri; bit-identical kalmaları sadece "parity contract" yorumuna bağlı. Saparsa
  `NAUTILUS_PARALLEL`'e göre **aynı strateji farklı kazanan** verir (sessiz, tekrarlanamaz).
- **H10** — Built-in composer block eklemek 4-yerde shotgun edit (open/closed değil); oysa
  `register_custom_block` doğrulama yapar — built-in'ler denetimsiz literal.

## 6 çapraz-kesen tema

1. **Tanrı-modüller** kod tabanının yarısını 4 dosyada topluyor: `agent_backtest.py` (2923),
   `composer.py` (2246), `backtest.py` (2286 web/1978 domain), `data.py` (1996). H3/H4/H6/H10'un
   kök nedeni.
2. **Fonksiyon-içi import'larla maskelenmiş döngüsel bağımlılıklar** (`server↔routes`,
   `backtest↔data`, `sandbox→web.routes`) — gerçek bağımlılık grafiğini araçlardan gizler,
   katman ihlalini runtime `ImportError`'a çevirir (biri, H7, zaten canlı).
3. **Paylaşımlı mutable modül-global'i varsayılan bağlama mekanizması** (`_STATE`,
   `_active_model`, `_IPC_Q`, `BLOCK_REGISTRY`, `ProgressStore` dict'leri); kilitleme tutarsız,
   session izolasyonu yok. Doğru desen (`run_many`/`progress_fn` enjeksiyonu) biliniyor ama
   düzensiz uygulanıyor.
4. **Private-sembol bağlama de-facto public API** — web `agent._*`/`data._*`'a, Motor
   `backtest._*`'a uzanıyor; en tehlikelisi LLM-kod güven sınırında (H7).
5. **Codegen/robustness güvenliği & tekrarlanabilirlik yapıyla değil convention'la** — 3 ayrı
   exec ortamı yorum-parity'siyle (biri `strategy.py`'de `RuntimeError`'ı `safe_builtins`'ten
   atlar), WFO parity (H8) ve timeout ordering (H2) sadece yorumda, `regression_baseline.json`
   (496 girdi) sıfır test tarafından tüketiliyor (ölü anchor).
6. **Sessiz degradasyon > gürültülü hata** — startup veri hatasını boş DataFrame'e (server
   "healthy" ama işlevsiz), proposer LLM/parse hatasını rastgele stratejiye, route worker'lar
   snapshot/log hatasını bare `pass`'e yutuyor; operatör "healthy" ile "degraded"ı ayırt edemiyor.

## Önceliklendirilmiş remediation

1. **Robustness servisini çıkar + progress kanalını açıkça geçir (H1)** — Efor M. En yüksek
   şiddet; Engine→Web inversiyonunu ve `_IPC_Q` global-mutasyonunu kaldırır, headless/CLI/test açar.
   Mevcut `progress_fn=_progress` desenini yansıt.
2. **WFO kümülatif-timeout invariantını düzelt (H2)** — Efor S. Suite-seviyesi monotonik
   deadline → `timeout_s=min(WFO_BATCH_TIMEOUT_S, remaining)`, ya da startup'ta
   `WFO_BATCH_TIMEOUT_S × (n_gen+1) < ROBUSTNESS_TIMEOUT_S` assert'i. Ucuz, 15 dk suite kaybını önler.
3. **Codegen `ImportError`'ı düzelt, doğrulamayı `codegate`'e yönlendir (H7)** — Efor S–M.
   Zaten kırık + güvenlik-ilgili; `has_builtin`/`validate_generated_code`/`safe_builtins`
   doğrudan `codegate`'ten import, private re-export alias'larını kaldır.
4. **`load_bars_and_recipe` servis dikişi (H3) + `backtest.py` bölme (H4)** — Efor L. En büyük
   yapısal kazanç; dispatch kopyalarının 4'ünü siler, `backtest↔data` döngüsünü kırar
   (factory'ler `instruments.py`'ye → `data.py` tek-yönlü import). İkisi birlikte yapılmalı.
5. **Tipli `Settings` + version-pin assert'ini import zamanına al (M13/M14/M15)** — Efor S.
   Dağınık env okumalarını, `STARTING_CASH` sapmasını, worker'ların drift'li Nautilus wheel
   çalıştırma boşluğunu tek geçişte kapatır.

Kalan orta/düşük bulgular (spec-builder kopyaları, model-fallback geri-dönüşsüzlüğü,
`ProcessPoolExecutor._processes` private'ına giriş, tipsiz recipe/unit kontratı, robustness
sonucunun tek poll sonrası atılması, vb.) ilgili modül refactor edilirken katlanmalı.

## Doğrulamanın kattığı değer

Çekişmeli katman "makul ama yanlış" bulguları eledi — ör. `data.py load_index_bars`'ın
index-bar hacmini sıfırlaması **kasıtlı davranış** olarak REFUTED; birkaç bulgu PLAUSIBLE'a
düşürüldü (model-fallback sınıflandırıcısı sanıldığından daha muhafazakâr; custom-block repair
loop'unun "kümülatif prompt" endişesi abartılı). ~%91 doğrulama oranı.

## Metodoloji

`Workflow` 4-fazlı: Map (5 küme paralel) → Review (7 boyut: katmanlama, eşzamanlılık/izolasyon,
veri-bütünlüğü, agent/LLM/codegen, state-yaşamdöngüsü, hata/config/gözlemlenebilirlik,
genişletilebilirlik/test) → Verify (her bulgu çekişmeli) → Synthesize. Aynı çok-ajanlı
çekişmeli desen [[nau_performans_denetimi]] ve [[webapp_module_map]]'in "Sağlamlaştırma"
turlarında da kullanıldı — tekrarlanabilir kalite kalıbı.

> Bu bir *denetim anlık görüntüsüdür* (2026-07-21). Refactor'lar uygulandıkça bulgular kapanır;
> güncel modül-eşlemesi için [[webapp_module_map]] esastır.

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[webapp_module_map]]
<!-- BACKLINKS:END -->
