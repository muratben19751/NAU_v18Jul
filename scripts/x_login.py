"""Tek seferlik X (Twitter) girişi — oturum çerezini `x_watch` için diske yazar.

Başı açık bir Chromium açar, operatör KENDİ X hesabıyla giriş yapar (2FA, captcha,
"olağandışı etkinlik" adımları dahil — hepsi elle), sonra bu betik oturumu
`DATA_DIR/x_storage_state.json` dosyasına kaydeder. `x_watch.py` her turda o
dosyayı yükleyip aramayı giriş yapmış hâlde yapar.

Parola ne sorulur ne saklanır: tarayıcıya operatör yazar, biz yalnız sonuçtaki
çerezleri alırız. (Tamamen otomatik yeniden giriş isteniyorsa `x_watch.relogin`
+ `NAU_XWATCH_X_USER`/`NAU_XWATCH_X_PASSWORD` yolu var; 2FA açıkken çalışmaz.)

Kullanım::

    pip install playwright && playwright install chromium
    python scripts/x_login.py

Wiki References: [[x_watch_izleyici]], [[webapp_module_map]]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from x_watch import (  # noqa: E402
    _UA,
    STORAGE_STATE_PATH,
    _harden_permissions,
    _sync_playwright,
)

_DONE_URL_HINTS = ("/home", "/search", "/notifications", "/explore")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    sync_playwright = _sync_playwright()
    print(
        "Bir Chromium penceresi açılıyor. KENDİ X hesabınızla giriş yapın.\n"
        "Giriş bitip ana akışı gördüğünüzde bu terminale dönüp Enter'a basın.\n"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(user_agent=_UA, locale="tr-TR")
            page = ctx.new_page()
            page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")

            input("Giriş tamamlandıysa Enter'a basın... ")

            url = page.url
            if not any(h in url for h in _DONE_URL_HINTS):
                # Uyar ama ENGELLEME: operatör sayfada olduğunu biliyor olabilir,
                # ve yanlış pozitif yüzünden çalışan bir oturumu atmak daha kötü.
                print(f"UYARI: adres hâlâ giriş akışına benziyor ({url}).")
                if input("Yine de kaydedilsin mi? [e/H] ").strip().lower() not in {
                    "e",
                    "y",
                }:
                    print("İptal edildi — hiçbir şey yazılmadı.")
                    return 1

            STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(STORAGE_STATE_PATH))
            _harden_permissions(STORAGE_STATE_PATH)
        finally:
            browser.close()

    # Yolun kendisi basılıyor (sır değil), ama içeriği asla — dosya oturum
    # çerezini taşır, yani hesaba erişim demektir.
    print(f"\nOturum kaydedildi: {STORAGE_STATE_PATH}")
    print("Sıradaki adım:  python x_watch.py --once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
