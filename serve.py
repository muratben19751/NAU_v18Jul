"""Nautilus Lab web arayüzünü başlat (PM2 / servis girişi).

    python serve.py                  # 127.0.0.1:8111
    python serve.py --port 9000

PM2 altında bu dosya `interpreter: python` ile çalışır ve
`nautilus.muratben.com` Cloudflare tüneli buraya bağlanır.

`server.py` doğrudan `uvicorn server:app` ile de çalışır; bu launcher yalnızca
PM2'nin beklediği "tek .py giriş noktası" desenini karşılar (bkz. quant/serve.py).

Uygulama YALNIZCA 127.0.0.1'e bağlanır — dışarıya açılma tünel üzerinden olur,
böylece LAN'daki başka bir cihaz doğrudan erişemez.

Wiki References
---------------
Bkz: [[webapp_module_map]], [[nautilus_kernel]]

`server.py`'nin süreç sarmalayıcısı: uygulamayı kurmaz, yalnızca başlatır.
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# "Gerçekten dağıtıldık" işareti, süreç yöneticisinden BAĞIMSIZ olarak.
#
# DÜZELTME (2026-08-17): bu satır önce yanlış bir gerekçeyle eklendi. "pm2 bu
# kurulumda `PM2_HOME`'u çocuk sürece geçirmiyor, dolayısıyla token dosyası hiç
# okunmuyor ve kapı açık" diye yazmıştım; İKİSİ DE YANLIŞTI. Kanıt olarak
# kullandığım iki gözlemin ikisi de ölçüm hatasıydı:
#
#   · `pm2 env <id>` süreçten değil YAPILANDIRMADAN okur; runtime ortamını
#     göstermez. Doğrudan ölçüldü: pm2 altında koşan bir Python süreci
#     `PM2_HOME = 'C:\\Users\\MYDESK\\.pm2'` görüyor. İşaret hep oradaydı.
#   · `urllib.urlopen` yönlendirmeyi takip eder; `303 → /login → 200` zinciri
#     tek bir `200` gibi göründü. Kapı zaten AÇIK DEĞİL, KAPALIYDI.
#
# Yani ortada bir açık yoktu. Kalan gerçek kusur şuydu: aynı "dağıtıldık mı"
# kuralı ÜÇ yerde ayrı ayrı yazılmıştı (dosya yedeği, açılış uyarısı, 503) ve
# üçü de tek bir dış değişkene bağlıydı — bu depoda tekrarlayan çoklu-kopya
# deseni. Kural `server._is_deployed()`'te tekleşti; bu satır ise işareti süreç
# yöneticisinin davranışından bağımsız kılıyor, çünkü `serve.py` dağıtımın
# kendisi. `PM2_HOME` yedek olarak duruyor ve çalışıyor.
#
# Geliştirme yolu (`uvicorn server:app`) bu dosyadan geçmez, dolayısıyla yerel
# koşum eskisi gibi kapısız kalır. `server` import'undan ÖNCE konması şart:
# `_ACCESS_TOKEN` import anında okunur.
os.environ.setdefault("NAU_DEPLOYED", "1")


def main() -> None:
    ap = argparse.ArgumentParser(description="Nautilus Lab web arayüzü")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8111)
    args = ap.parse_args()

    import uvicorn

    # reload=False bilinçli: strateji ÜRETİMİ bellekte durum tutan bir worker
    # thread'de koşar, reload onu öldürür (bkz. server.py modül docstring'i).
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=False,
    )


if __name__ == "__main__":
    main()
