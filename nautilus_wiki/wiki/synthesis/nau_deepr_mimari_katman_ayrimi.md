---
title: Mimari katman ayrımı — motor web'in altında durur
type: synthesis
summary: Robustluk suite'i web/routes/agent_backtest.py içinde durduğu için sandbox child'ı sırf onu çağırmak üzere tüm FastAPI router ağacını import ediyor ve import grafiğinde iki gerçek döngü oluşuyordu; `auto/` paketi bağımlılığı doğru yöne çevirdi ve kural iki AST testiyle bağlandı.
sources: []
related:
  - wiki/synthesis/nau_deepr_dorduncu_tur_2026_08_11.md
  - wiki/synthesis/webapp_module_map.md
last_updated: 2026-08-18
---

# Mimari katman ayrımı — motor web'in altında durur

Robustluk suite'i bir HTTP özelliği değil, **motorun kendisidir**: `sandbox.py`
onu ayrı bir süreçte koşturur, `parallel_exec.py` alt birimlerini bir process
pool'a dağıtır. İkisi de FastAPI servis etmez.

Buna rağmen kod uzun süre `web/routes/agent_backtest.py` içinde durdu. Sonucu
iki katmanlıydı ve ikincisi statikti, yani yorumla tartışılamazdı:

* **Çalışma zamanı** — sandbox child'ı, sırf suite'i çağırabilmek için tüm
  router ağacını (APIRouter, şablon ortamı, oturum log dizini) import etmek
  zorundaydı. HTTP servis etmeyen bir worker, HTTP katmanının tamamını
  yüklüyordu.
* **Import grafiği** — iki gerçek döngü vardı:
  `sandbox → web.routes.agent_backtest → sandbox` ve
  `parallel_exec → sandbox → web.routes.agent_backtest → parallel_exec`.

Bulgu DeepR 2026-08-11 [YÜKSEK] (bkz. [[nau_deepr_dorduncu_tur_2026_08_11]]).

## Düzeltme: bağımlılığın YÖNÜ

`auto/` paketi motoru web'in altına aldı — route (ve sandbox) onu çağırır,
tersi olmaz. Bu paketteki hiçbir modül `web.*` import etmez.

Aktarım sırasında bir ayrıntı kasıtlı olarak korundu: `agent_backtest.py`
taşınan isimleri alt tireli takma adlarla (`_peer_exclusions`, `_wfo_test`,
`_MC_DD_LIMIT`) yeniden dışa veriyor. Bu modülün tarihsel import yüzeyi
(testlerin `ab._peer_exclusions` diye eriştiği yüzey) taşıma yüzünden
değişmesin diye — davranış birebir korundu, yalnız ev sahibi değişti.

İlerleme aktarımı da açık hâle geldi: `sandbox._robustness_child` eskiden
`import web.routes.agent_backtest as ab` yapıp `ab._IPC_Q = q` ile BAŞKA bir
modülün private global'ini set ediyordu. Artık çağıranın verdiği bir
`progress_fn` var — child kuyruğa yazar, in-process bir çağıran koşu durumuna.

## Kural niyet değil, test

İki AST testi sınırı denetliyor ve ikisi de kapsamını KURALDAN türetir, elle
yazılmış bir listeden değil:

| test | ne der |
|---|---|
| `tests/test_auto_layer_is_web_free.py` | `auto/` altındaki hiçbir modül `web.*` import etmez |
| `tests/test_web_layer_has_no_cycle.py` | web katmanı geri yönde bir döngü kurmaz |

Aynı duruşun üçüncü örneği `tests/test_env_registry_is_complete.py`: ortam
değişkeni kataloğu ile kod arasındaki iki yönlü sürüklenmeyi kırmızıya çevirir
(bkz. [[surec_yoneticisi_ortami_dondurur]]).

Ayırt edici soru hep aynı: **bu denetimin kapsamı, denetlediği şeyin
tanımından mı türüyor, yoksa yazıldığı gün doğru olan bir listeden mi?**
Bkz. [[kod_dokuman_koprusu_denetlenmiyor]].

İlgili: [[webapp_module_map]], [[auto_kapi_ve_geri_bildirim]].

<!-- BACKLINKS:BEGIN -->
## Referenced by

- [[nau_deepr_dorduncu_tur_2026_08_11]]
<!-- BACKLINKS:END -->
