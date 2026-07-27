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
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


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
