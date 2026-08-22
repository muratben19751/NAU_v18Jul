"""nau-web'i ISIMLI BORU üzerinden servis et — Kaspersky TCP filtresini atlar.

Bu makinede Kaspersky çekirdek filtresi python.exe'nin TCP yanıt gövdelerini
yutuyor (node serbest, python değil; ölçüldü 2026-08-22). Named pipe ağ
yığınından geçmediği için python'un boruya yazması filtrelenmez. `node_proxy.js`
cloudflared'i TCP :8111'de karşılar (node TCP serbest) ve istekleri bu borudan
geçirir.

ASGI app'i (server:app) her boru bağlantısında bir iş parçacığında koşturur,
yanıtı tamponlayıp boruya yazar.

Wiki References
---------------
Bkz: [[webapp_module_map]]. Bu makinede Kaspersky çekirdek filtresi python.exe'nin
TCP yanıt GÖVDESİNİ yutuyordu (node serbest, python client-send serbest, yalnız
python server-yanıtı engelli); bu launcher o filtreyi named pipe ile atlar
(`node_proxy.js` TCP ucu). `serve.py` (doğrudan uvicorn) geliştirme/temiz makine
yolu olarak durur; bu dosya Kaspersky'li üretim makinesinin girişidir.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import traceback

try:  # Türkçe log satırları pm2'nin cp125x konsolunu patlatmasın
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ.setdefault(
    "NAU_DEPLOYED", "1"
)  # auth kapısı açık kalsın (serve.py ile aynı)

import h11  # noqa: E402
import win32file  # noqa: E402
import win32pipe  # noqa: E402

PIPE = chr(92) * 2 + "." + chr(92) + "pipe" + chr(92) + "nauweb"

from server import app  # noqa: E402  (startup: Bybit fetch, ~10-20s)


def _run_asgi(scope: dict, body: bytes) -> tuple[int, list, bytes]:
    """ASGI app'i bir kez koştur, (status, headers, body) döndür."""
    result = {"status": 500, "headers": [], "body": b""}

    async def _go():
        sent = {"started": False}
        chunks: list[bytes] = []
        req_done = {"v": False}

        async def receive():
            if not req_done["v"]:
                req_done["v"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                result["status"] = msg["status"]
                result["headers"] = msg.get("headers", [])
                sent["started"] = True
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))

        await app(scope, receive, send)
        result["body"] = b"".join(chunks)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()
    return result["status"], result["headers"], result["body"]


def _read_request(h) -> tuple:
    conn = h11.Connection(h11.SERVER)
    req = None
    body = b""
    while True:
        ev = conn.next_event()
        if ev is h11.NEED_DATA:
            hr, chunk = win32file.ReadFile(h, 65536)
            conn.receive_data(chunk if chunk else b"")
            if not chunk:
                return None, b"", conn
            continue
        if isinstance(ev, h11.Request):
            req = ev
        elif isinstance(ev, h11.Data):
            body += ev.data
        elif isinstance(ev, h11.EndOfMessage):
            return req, body, conn
        elif isinstance(ev, h11.ConnectionClosed) or ev is h11.PAUSED:
            return req, body, conn


def _handle(h):
    try:
        req, body, conn = _read_request(h)
        if req is None:
            return
        target = req.target.decode("latin1")
        path, _, query = target.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": req.method.decode("ascii"),
            "path": path,
            "raw_path": path.encode("latin1"),
            "query_string": query.encode("latin1"),
            "root_path": "",
            "scheme": "http",
            "headers": [(k.lower(), v) for k, v in req.headers],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 8111),
        }
        status, headers, rbody = _run_asgi(scope, body)
        out = h11.Connection(h11.SERVER)
        # h11 istemci-yönlü döngüyü beklemesin diye elle üret:
        hdrs = [(k, v) for k, v in headers]
        # content-length garanti + connection: close
        has_cl = any(k.lower() == b"content-length" for k, v in hdrs)
        if not has_cl:
            hdrs.append((b"content-length", str(len(rbody)).encode()))
        hdrs = [(k, v) for k, v in hdrs if k.lower() != b"connection"]
        hdrs.append((b"connection", b"close"))
        # elle HTTP/1.1 yanit serialize et (h11 request beklerdi)
        line = b"HTTP/1.1 %d X\r\n" % status
        head = b"".join(b"%s: %s\r\n" % (k, v) for k, v in hdrs)
        win32file.WriteFile(h, line + head + b"\r\n" + rbody)
        win32file.FlushFileBuffers(h)
    except Exception:
        traceback.print_exc()
    finally:
        try:
            win32pipe.DisconnectNamedPipe(h)
            win32file.CloseHandle(h)
        except Exception:
            pass


def _run_lifespan():
    """ASGI lifespan startup'ını koştur — uvicorn bunu yapardı.

    server.py'nin `lifespan()`'i market context'i ve topbar verisini kuruyor
    (`set_market_context`, `_context["market"]`). Boru sunucusu ASGI app'i elle
    sürdüğü için lifespan olayını da elle göndermeli; yoksa ana sayfalar
    başlatılmamış durumla eksik/hatalı render eder. Görev kalıcı bir loop'ta
    'started' halde asılı kalır; kurduğu modül-global durum tüm istek
    loop'larınca görülür.
    """
    loop = asyncio.new_event_loop()
    done = threading.Event()
    err = {}

    async def _life():
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
        q: asyncio.Queue = asyncio.Queue()
        await q.put({"type": "lifespan.startup"})

        async def receive():
            return await q.get()

        async def send(msg):
            if msg["type"] == "lifespan.startup.complete":
                done.set()
            elif msg["type"] == "lifespan.startup.failed":
                err["msg"] = msg.get("message", "")
                done.set()

        await app(scope, receive, send)

    def _runner():
        asyncio.set_event_loop(loop)
        loop.create_task(_life())
        loop.run_forever()

    threading.Thread(target=_runner, daemon=True).start()
    if not done.wait(90):
        print("lifespan startup 90s'de tamamlanmadı (yine de devam)", flush=True)
    elif err:
        print(f"lifespan startup HATASI: {err['msg']}", flush=True)
    else:
        print("lifespan startup tamam (market context kuruldu)", flush=True)


def main():
    # node_proxy artık ayrı bir pm2 süreci (nau-proxy); pm2 her ikisini de
    # bağımsız yönetir, restart'ta orphan node kalmaz. Bu süreç yalnız boruyu
    # servis eder — TCP portu dinlemez.
    _run_lifespan()
    print(f"pipe_serve hazir: {PIPE!r}", flush=True)
    while True:
        h = win32pipe.CreateNamedPipe(
            PIPE,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            0,
            None,
        )
        win32pipe.ConnectNamedPipe(h, None)
        threading.Thread(target=_handle, args=(h,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
