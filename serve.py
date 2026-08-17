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

# "Gerçekten dağıtıldık" işareti — ve işaret BU DOSYANIN KENDİSİ.
#
# Eskiden o rolü `PM2_HOME` oynuyordu: erişim kapısının dosya yedeği, açılış
# uyarısı ve (2026-08-17'den beri) token'sız dağıtımı reddeden 503, üçü de ona
# bakıyordu. ÖLÇÜLDÜ 2026-08-17, canlı süreçte: `pm2 env` çıktısında `PM2_HOME`
# YOK — pm2 bu kurulumda onu çocuk sürece geçirmiyor. Sonuç zinciri şuydu:
# `~/.nau_access_token` (6 bayt, mevcut) hiç okunmadı → `_ACCESS_TOKEN` boş →
# `_is_authenticated` herkese True → `GET /` çerezsiz 200 döndü, cloudflared
# 15 saattir açıkken.
#
# Kusur token'da değil, İŞARETTEYDİ: koruma, izlediğiyle aynı arızaya bağlıydı.
# İşaret kaybolunca hem kapı açıldı hem kapıyı bekleyen alarm sustu.
#
# `serve.py` kaybolamaz, çünkü dağıtım O. Geliştirme yolu `uvicorn server:app`
# bu dosyadan geçmez, dolayısıyla yerel koşum eskisi gibi kapısız kalır.
# `server` import'undan ÖNCE konması şart: `_ACCESS_TOKEN` import anında okunur.
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
