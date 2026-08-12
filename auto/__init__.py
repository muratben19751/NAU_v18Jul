"""AUTO (otonom araştırma döngüsü) alan katmanı — web'den bağımsız.

Neden ayrı bir paket: robustluk suite'i bir HTTP özelliği değil, motorun
kendisidir. `sandbox.py` onu ayrı bir süreçte (child process) koşturur ve
`parallel_exec.py` alt-birimlerini bir process pool'a dağıtır — ikisi de
FastAPI servis etmez. Kod `web/routes/agent_backtest.py` içinde durduğu
sürece o child, sırf suite'i çağırabilmek için tüm router ağacını (APIRouter,
şablon ortamı, oturum log dizini) import etmek zorundaydı; statik import
grafiğinde de `sandbox -> web.routes.agent_backtest -> sandbox` ve
`parallel_exec -> sandbox -> web.routes.agent_backtest -> parallel_exec`
olmak üzere iki gerçek döngü vardı (DeepR 2026-08-11 [YÜKSEK]).

Bu paket bağımlılığı doğru yöne çevirir: motor altta durur, route (ve
sandbox) onu çağırır. Buradaki hiçbir modül `web.*` import etmez — yeni bir
şey eklerken bu kural korunmalı; `tests/test_auto_layer_is_web_free.py` bunu
AST ile denetliyor.

Wiki References
---------------
See: [[webapp_module_map]], [[auto_kapi_ve_geri_bildirim]],
[[nau_deepr_mimari_katman_ayrimi]]
"""
