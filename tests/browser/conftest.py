"""Gerçek tarayıcı E2E'si için izole sunucu + Playwright sayfası.

DeepR 2026-08-11 [ORTA]: uygulamanın en kırılgan mantığının bir kısmı şablonlara
gömülü **inline JS**'te yaşıyor — SIMPLE sihirbazının adım kilitleri, mod geçişi
(`labMode` + `swMoveNodes` canlı DOM düğümlerini TAŞIYOR), `mcSyncRange`, MAX
düğmesi. Sunucu tarafı hiçbir test bunlara ulaşamaz: `TestClient` HTML'i alır,
JS'i çalıştırmaz. Bu yüzden o kod tamamen korumasızdı ve tur boyunca bulunan
UX hatalarının çoğu (MAX'ın seçili TF'i yok sayması, sabit kategori, sihirbazın
"Improve with AI"ının sonucu göndermemesi) tam olarak orada oturuyordu.

## Duruş: sessiz skip DEĞİL, açık opt-in

Kardeş bulgu [YÜKSEK] şuydu: "kritik E2E testleri ortama bağlı olduğu için
varsayılan koşumda ve CI'da HİÇ çalışmıyor (sessiz skip)". Bir tarayıcı süiti
eklerken aynı tuzağa düşmek kolay. Bu yüzden:

- varsayılan koşumda `browser` işaretiyle **deselect** edilir — özet satırında
  "deselected" olarak görünür, "skipped" olarak değil (biri kasıt, öbürü kaza),
- `-m browser` ile koşulduğunda Playwright ya da tarayıcı ikilisi yoksa
  **HATA** verir, atlamaz: "koştum ve geçti" ile "hiç koşmadı" ayırt edilebilsin,
- CI'da ayrı bir adım koşar ve `N passed` kontrolü yapar (canlı-Bybit e2e
  adımıyla aynı desen): sıfır test toplayan bir koşum yeşil sayılmaz.

## İzolasyon

Sunucu AYRI BİR SÜREÇ olarak, `NAU_DATA_DIR` ve `NAU_STUDIO_DB` geçici bir
dizine yönlendirilmiş hâlde açılır. Bu pazarlık konusu değil: bu süit gerçek
`~/.cache/nautilus_web_app` kataloğuna ve repo kökündeki `studio.db`'ye ASLA
dokunmamalı. Ortam değişkenleriyle taşınabilir olmaları zaten bu turda eklendi
(`app_constants.data_dir`/`studio_db_path`) — burası onun ilk gerçek müşterisi.

Wiki References: [[webapp_module_map]], [[surec_yoneticisi_ortami_dondurur]]
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_BOOT_TIMEOUT_S = 90.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until_up(base: str, proc: subprocess.Popen, deadline: float) -> None:
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else "") or ""
            raise RuntimeError(
                f"E2E sunucusu açılışta öldü (exit {proc.returncode}):\n{out[-4000:]}"
            )
        try:
            with urllib.request.urlopen(f"{base}/", timeout=2) as r:
                if r.status < 500:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    raise RuntimeError(f"E2E sunucusu {_BOOT_TIMEOUT_S:.0f}s içinde açılmadı ({last})")


@pytest.fixture(scope="session")
def live_server(tmp_path_factory) -> str:
    """İzole bir veri kökü üzerinde gerçek uvicorn süreci; base URL döner."""
    data_dir = tmp_path_factory.mktemp("e2e-data")
    env = {
        **os.environ,
        # ── İZOLASYON — gerçek katalog ve studio.db'ye dokunma ──
        "NAU_DATA_DIR": str(data_dir),
        "NAU_STUDIO_DB": str(data_dir / "studio.db"),
        # Erişim kapısı kapalı olsun: bu süit auth'u değil arayüz mantığını sınıyor
        # (kapının kendi testleri var).
        "NAU_ACCESS_TOKEN": "",
        # Dağıtım işaretleri sızmasın: PM2_HOME set ise sunucu ev dizinindeki
        # token dosyasını okur ve her sayfa 303'e döner.
        "PM2_HOME": "",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_until_up(base, proc, time.monotonic() + _BOOT_TIMEOUT_S)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.fixture(scope="session")
def browser():
    """Chromium. Eksikse ATLAMAZ — hata verir (bkz. modül docstring'i)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "playwright kurulu değil. `-m browser` açıkça istendi, yani sessizce "
            "atlamak yanlış olur: `uv sync --extra dev && playwright install chromium`"
        ) from e

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Chromium başlatılamadı — ikili muhtemelen kurulu değil: "
                "`playwright install chromium`"
            ) from e
        try:
            yield b
        finally:
            b.close()


@pytest.fixture()
def page(browser, live_server):
    """Temiz bir bağlam (localStorage boş → mod tercihi testler arası sızmaz).

    Konsol hataları TOPLANIR ve `assert_no_page_errors` ile iddiaya çevrilebilir:
    inline JS'te bir `ReferenceError` sayfayı görsel olarak bozmayabilir ama
    sonraki script bloğunu da öldürür — bu süitin var oluş sebebi tam olarak
    böyle bir hatayı görünür kılmak.
    """
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.errors = []  # type: ignore[attr-defined]
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on(
        "console",
        lambda m: (
            pg.errors.append(f"console.error: {m.text}") if m.type == "error" else None
        ),
    )
    pg.base = live_server  # type: ignore[attr-defined]
    try:
        yield pg
    finally:
        ctx.close()


@pytest.fixture()
def assert_no_page_errors(page):
    def _check():
        # Ağ 4xx/5xx'i konsola "Failed to load resource" olarak düşebilir; bu
        # süit JS hatalarını kovalıyor, eksik favicon'u değil.
        real = [e for e in page.errors if "Failed to load resource" not in e]
        assert not real, "sayfada JS hatası var:\n  " + "\n  ".join(real)

    return _check
