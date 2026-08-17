"""Kurulmamış bir kaynak ve koşmayan bir runner kendini SÖYLEMELİ.

DeepR 2026-08-11 [ORTA] — iki ayrı bulgu, tek sınıf: uygulama bir şeyin
çalışmadığını BİLİYOR ama ekranda "sorun yok" görünüyor.

1. **Index (US-Index) yolu varsayılan kurulumda ölü ve uyarısız.**
   `data.INDEX_ROOT` varsayılanı bir macOS yolu
   (`/Users/i034216/Z/us_indices/values_v1`); Windows/Linux kutuda asla var
   olmaz. `NAUTILUS_INDEX_ROOT` ayarlanmadıkça Index kaynağıyla ilgili HER
   adım kırılıyordu — /data'daki "Discover tickers" 404, /backtest/tickers'ta
   "ticker discovery failed: FileNotFoundError: …" `<option>`'ı, Index
   backtest'inde "No bars for {ticker}" — ve hiçbiri "kök yok" demiyordu.
   Dikkat çekici olan, AYNI dosyanın `EXTERNAL_CATALOGS` için modül
   yüklenirken açık bir uyarı loglaması (L31): Index için eşdeğeri yoktu,
   yani UI'daki üç kaynaktan biri sessizce ölü duruyordu ve operatör bunu
   ancak deneyerek anlıyordu.

   Düzeltme tek bir cümleyi (``data.index_root_warning()``) tüm yüzeylerde
   paylaştırıyor: modül log'u, /data paneli, ticker picker'ı, discover 404'ü
   ve backtest hatası aynı teşhisi verir — kullanıcı üç ayrı yerde birbirini
   tutmayan üç şey okumaz.

2. **STUDIO_RUNNER ayarlı değilken deployment 'running' işaretleniyordu.**
   `_runner_pickup`, `RUNNER is None` iken hiçbir TradingNode kurmadan satırı
   RUNNING yapıyor; panel yeşil rozeti ve Pause/Stop düğmelerini gösteriyordu.
   Kullanıcı hiç çalışmayan bir "paper deployment"ı canlı sanıyordu. Aynı
   dürüstlük sorunu backtest motoru için zaten bilinçli olarak çözülmüş
   (`engine_is_stub` + "⚠ SİMÜLE — gerçek backtest değil" rozeti); üçüncü
   motor anahtarı bu düzeltmenin dışında kalmıştı.

   Davranış KORUNUYOR (simüle pickup panelin yoklamasını anlamlı tutuyor),
   değişen şey ekranın ne söylediği.

Şablon iddiaları RENDER EDİLEREK doğrulanır — dosyada string aramak, o
parçanın gerçekten sayfaya girdiğini kanıtlamaz. Gerçek kataloğa,
`~/.cache` bloklarına ve `studio.db`'ye yazılmaz.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════════════════
# Bulgu 1 · Index kökü yoksa bunu söyle
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def missing_index_root(monkeypatch, tmp_path):
    """INDEX_ROOT var olmayan bir yola bakıyor, env var AYARLI DEĞİL.

    Taze bir Windows/Linux kurulumunun tam hâli: operatör hiçbir şey
    ayarlamamış ve yerleşik varsayılan başka bir makinenin yolu.
    """
    import data

    monkeypatch.setattr(data, "INDEX_ROOT", tmp_path / "yok")
    monkeypatch.delenv(data._INDEX_ROOT_ENV, raising=False)
    return data


@pytest.fixture
def present_index_root(monkeypatch, tmp_path):
    """Kök gerçekten var — hiçbir uyarı çıkmamalı (yanlış alarm da bir hata)."""
    import data

    root = tmp_path / "var"
    root.mkdir()
    monkeypatch.setattr(data, "INDEX_ROOT", root)
    monkeypatch.setenv(data._INDEX_ROOT_ENV, str(root))
    return data


def test_a_reachable_root_produces_no_warning(present_index_root):
    """Kök erişilebilirken uyarı YOK: gürültü, sessizlik kadar zararlı."""
    data = present_index_root
    assert data.index_root_warning() is None
    assert data.index_root_status()["exists"] is True


def test_an_unset_env_var_says_the_source_is_not_installed(missing_index_root):
    """Kullanıcı ne yapacağını öğrenmeli: hangi env var, hangi alternatif."""
    data = missing_index_root
    warn = data.index_root_warning()

    assert warn is not None
    assert "NAUTILUS_INDEX_ROOT" in warn
    # "Başka bir makinenin yolu" bilgisi olmadan kullanıcı yolu düzeltmeye
    # değil, var etmeye çalışır.
    assert "another machine" in warn
    # Kaynak ölüyken uygulamanın tamamı ölü değil — çıkış yolu gösterilmeli.
    assert "Bybit" in warn and "External" in warn


def test_a_configured_but_missing_path_gets_a_different_diagnosis(
    monkeypatch, tmp_path
):
    """Env var AYARLI ama yol yoksa teşhis "kurulmamış" değil, "yol yanlış".

    İki durum farklı eylem gerektiriyor: birinde kaynağı kurmak, diğerinde
    yazılan yolu düzeltmek (ya da NFS mount'unu geri getirmek). Tek bir
    genel cümle operatörü yanlış işe yollardı.
    """
    import data

    bad = tmp_path / "mount-dustu"
    monkeypatch.setattr(data, "INDEX_ROOT", bad)
    monkeypatch.setenv(data._INDEX_ROOT_ENV, str(bad))

    warn = data.index_root_warning()
    assert warn is not None
    assert str(bad) in warn
    assert "does not exist" in warn
    # Bu dal "kurulmamış" dilini KULLANMAMALI.
    assert "another machine" not in warn


def test_status_reports_whether_the_default_is_in_play(
    missing_index_root, present_index_root
):
    """`configured` bayrağı env var'ın varlığını izler, yolun varlığını değil."""
    # present_index_root son uygulanan fixture: env var ayarlı.
    assert present_index_root.index_root_status()["configured"] is True


def test_status_is_recomputed_every_call_not_cached(monkeypatch, tmp_path):
    """Kök bir NFS mount; sunucu ömrü içinde bağlanıp kopabilir.

    Önbelleklenmiş bir "yok" cevabı, mount geri geldikten sonra da kaynağı
    ölü göstermeye devam ederdi — tam da kapatılan bulgunun tersi yönde bir
    yalan.
    """
    import data

    root = tmp_path / "nfs"
    monkeypatch.setattr(data, "INDEX_ROOT", root)
    monkeypatch.delenv(data._INDEX_ROOT_ENV, raising=False)

    assert data.index_root_warning() is not None
    root.mkdir()
    assert data.index_root_warning() is None


def test_module_load_warns_just_like_external_catalogs_do():
    """Modül yüklenirken uyarı basılmalı — bulgunun karşılaştırma noktası buydu.

    Süreç içi ölçüm işe yaramaz (pytest `data`'yı çoktan import etti), o
    yüzden temiz bir alt süreçte, `NAUTILUS_INDEX_ROOT` var olmayan bir yola
    bakarken import edilir.
    """
    import os

    env = dict(os.environ)
    env["NAUTILUS_INDEX_ROOT"] = str(ROOT / "_kesinlikle_yok_bir_kok")
    code = (
        "import logging, io, sys;"
        "buf = io.StringIO();"
        "h = logging.StreamHandler(buf);"
        "logging.getLogger().addHandler(h);"
        "logging.getLogger().setLevel(logging.WARNING);"
        "import data;"
        "print('NAUTILUS_INDEX_ROOT' in buf.getvalue())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("True"), (
        f"modül yüklenirken INDEX_ROOT uyarısı basılmadı: {proc.stdout!r}"
    )


def test_data_page_shows_the_reason_instead_of_a_dead_discover_button(
    missing_index_root,
):
    """Panel "registry yok, Discover'a bas" demek yerine sebebi söyler.

    Eski hâlde tek ipucu "Ticker registry not built yet. Click Discover
    tickers" idi; o düğme bu durumda 404 döner, yani talimat kullanıcıyı
    çıkmaza yolluyordu.
    """
    from fastapi.testclient import TestClient

    from server import app

    body = TestClient(app).get("/data").text

    assert "source unavailable" in body
    assert "NAUTILUS_INDEX_ROOT" in body
    # Çıkmaza yollayan talimat artık gösterilmiyor ve düğme tıklanamaz.
    assert "Ticker registry" not in body
    discover = body[body.find("/data/index/discover") :][:600]
    assert "disabled" in discover


def test_discover_endpoint_refuses_with_the_same_sentence(missing_index_root):
    """Düğme disabled ama uç doğrudan çağrılabilir; teşhis orada da aynı olmalı."""
    from fastapi.testclient import TestClient

    from server import app

    r = TestClient(app).post("/data/index/discover", data={"force": False})
    assert r.status_code == 404
    assert "NAUTILUS_INDEX_ROOT" in r.json()["detail"]


def test_ticker_picker_explains_itself_instead_of_echoing_an_exception(
    missing_index_root,
):
    """Eskiden "ticker discovery failed: FileNotFoundError: …" yazıyordu.

    Bu bir teşhis değil, yankı: kullanıcıya ne yapacağını söylemiyor ve
    /data panelindeki (o zaman da olmayan) mesajla ilgisiz.
    """
    from fastapi.testclient import TestClient

    from server import app

    r = TestClient(app).get("/backtest/tickers")
    assert r.status_code == 200
    assert "NAUTILUS_INDEX_ROOT" in r.text
    assert "FileNotFoundError" not in r.text


def test_every_surface_quotes_the_one_helper(missing_index_root):
    """Tek cümle, üç yüzey: metin ıraksarsa kullanıcı üç farklı şey okur."""
    from fastapi.testclient import TestClient
    from markupsafe import escape

    from server import app

    warn = missing_index_root.index_root_warning()
    client = TestClient(app)

    # Cümlede kesme işareti var ("machine's"); HTML'e markupsafe'in kaçışıyla
    # girer (`&#39;`), o yüzden kıyas da aynı kaçışla yapılır.
    escaped = str(escape(warn))
    assert escaped in client.get("/data").text
    assert escaped in client.get("/backtest/tickers").text
    # 404 gövdesi FastAPI'nin JSON zarfı (`{"detail": …}`) — kaçışsız hâli
    # `detail` alanında aranır, gövde metninde değil (Windows yolundaki ters
    # bölü işaretleri JSON'da iki kez kaçar).
    r404 = client.post("/data/index/discover", data={"force": False})
    assert r404.json()["detail"] == warn


# ══════════════════════════════════════════════════════════════════════════
# Bulgu 2 · Runner yokken RUNNING satırı kendini SİMÜLE ilan etsin
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def studio_client(tmp_path, monkeypatch):
    """İzole bir studio.db + tohum strateji. Gerçek `studio.db`'ye dokunulmaz."""
    from fastapi.testclient import TestClient

    from scripts.seed_studio import build_fixture
    from server import app
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    store = StrategyStore(tmp_path / "t.db")
    fx = build_fixture()
    for ic in fx.instruments:
        ic.active = True
    store.save(fx)
    monkeypatch.setattr(main, "store", store)
    return TestClient(app), store, main


def _stub_runner(monkeypatch, main, *, stub: bool):
    """RUNNER'ı testin istediği hâle getir (gerçek PaperRunner asla kurulmaz)."""
    if stub:
        monkeypatch.setattr(main, "RUNNER", None)
    else:
        monkeypatch.setattr(
            main,
            "RUNNER",
            type(
                "FakeRunner",
                (),
                {"launch": lambda self, *a: None, "active_ids": lambda self: set()},
            )(),
        )


def test_deploy_modal_warns_before_the_user_commits(studio_client, monkeypatch):
    """Uyarının yeri BURASI: Capital/kill-switch doldurmadan ÖNCE.

    Kullanıcı "Paper · Nautilus SIM" ile "hiçbir şey koşmuyor"u ayırt
    edemiyordu; modal ikisini de "paper" diye sunuyordu.
    """
    client, _store, main = studio_client
    _stub_runner(monkeypatch, main, stub=True)

    body = client.get("/studio/wt-funding-v3/deploy/modal").text

    assert "sim-badge" in body
    assert "STUDIO_RUNNER" in body
    assert "no order" in body


def test_deploy_modal_stays_quiet_when_a_runner_is_attached(studio_client, monkeypatch):
    """Runner varken uyarı çıkmamalı — yoksa rozet anlamını yitirir."""
    client, _store, main = studio_client
    _stub_runner(monkeypatch, main, stub=False)

    body = client.get("/studio/wt-funding-v3/deploy/modal").text

    assert "sim-badge" not in body


def test_a_stub_running_row_is_labelled_sim(studio_client, monkeypatch):
    """Yeşil RUNNING rozeti + Pause/Stop, arkada hiçbir node yokken yalandı."""
    client, store, main = studio_client
    _stub_runner(monkeypatch, main, stub=True)
    store.create_deployment("dep-abc123", "wt-funding-v3", 1, "paper", "{}")
    store.set_deployment_status("dep-abc123", "running")

    body = client.get("/studio/wt-funding-v3/deployments/panel").text

    assert "SIM" in body
    assert "STUDIO_RUNNER" in body
    assert "nothing is trading" in body


def test_a_real_runner_leaves_the_row_unqualified(studio_client, monkeypatch):
    """Gerçek runner varken RUNNING gerçekten RUNNING demek."""
    client, store, main = studio_client
    _stub_runner(monkeypatch, main, stub=False)
    store.create_deployment("dep-real01", "wt-funding-v3", 1, "paper", "{}")
    store.set_deployment_status("dep-real01", "running")

    body = client.get("/studio/wt-funding-v3/deployments/panel").text

    assert "· SIM" not in body
    assert "nothing is trading" not in body


def test_pickup_without_a_runner_logs_why_nothing_traded(
    studio_client, monkeypatch, caplog
):
    """Rozet operatör BAKARKEN doğruyu söyler; log sonradan sorana söyler.

    "Bu deployment neden hiç emir göndermemiş?" sorusunun cevabı kayıtta
    olmalı — ekran görüntüsü kalmaz, log kalır.
    """
    import logging

    _client, store, main = studio_client
    _stub_runner(monkeypatch, main, stub=True)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    store.create_deployment("dep-log001", "wt-funding-v3", 1, "paper", "{}")

    with caplog.at_level(logging.WARNING):
        main._runner_pickup("dep-log001", "{}")

    assert store.get_deployment("dep-log001")["status"] == "running"
    assert any("STUDIO_RUNNER unset" in r.getMessage() for r in caplog.records), (
        "runner'sız pickup sessizce geçti"
    )


def test_the_deploy_confirmation_does_not_promise_a_pickup(studio_client, monkeypatch):
    """ "pending runner pickup" runner yokken de yazıyordu.

    Beklenen bir pickup yok, simüle edilen bir tanesi var — onay satırının
    kendisi de bunu söylemeli, kullanıcı paneli aşağı kaydırmak zorunda
    kalmasın.
    """
    client, _store, main = studio_client
    _stub_runner(monkeypatch, main, stub=True)
    monkeypatch.setattr(main, "_runner_pickup", lambda *a, **k: None)
    # Tohum strateji `to_nautilus` tarafından reddediliyor (funding feed'i yok);
    # burada denenen şey artefaktın kendisi değil, ONAY SATIRININ metni.
    monkeypatch.setattr(main, "prepare_deployment", lambda *a, **k: "{}")

    r = client.post(
        "/studio/wt-funding-v3/deploy",
        data={
            "environment": "paper",
            "instruments": "active",
            "capital": "10000",
            "kill_switch": "3",
        },
    )

    assert r.status_code == 200
    assert "simulated pickup only" in r.text
    assert "pending runner pickup" not in r.text


# ── kill switch paneli: duran makine NEDEN durduğunu söylemeli ───────────────


def test_a_paused_row_shows_why_it_was_paused(studio_client, monkeypatch):
    """Kill switch arka planda duraklatıyor; sebep satırda görünmezse operatör
    "neden durdu?" sorusuyla baş başa kalıyor.

    Şablon sebebi yalnız `failed` satırlarda basıyordu — kill switch ise
    `paused` yazıyor, yani gerekçe tam da yeni ihtiyaç duyulduğu yerde
    ekrana hiç gelmiyordu.
    """
    client, store, main = studio_client
    _stub_runner(monkeypatch, main, stub=False)
    store.create_deployment("dep-ks0001", "wt-funding-v3", 1, "paper", "{}")
    store.set_deployment_status(
        "dep-ks0001", "paused", "kill switch fired: -3.20% on the day, limit -3.00%."
    )

    body = client.get("/studio/wt-funding-v3/deployments/panel").text

    assert "kill switch fired" in body
    assert "-3.20% on the day" in body


def test_the_panel_keeps_polling_while_a_deployment_runs(studio_client, monkeypatch):
    """Panel yalnız `pending` varken yenileniyordu.

    Kill switch RUNNING bir satırı arka plan thread'inden PAUSED'a çeviriyor;
    yenilenmeyen panel operatöre, o sayfayı elle tazeleyene kadar yeşil rozeti
    göstermeye devam ederdi — ekranın doğruyu söylemesi gereken tek an.
    """
    client, store, main = studio_client
    _stub_runner(monkeypatch, main, stub=False)
    store.create_deployment("dep-ks0002", "wt-funding-v3", 1, "paper", "{}")
    store.set_deployment_status("dep-ks0002", "running")

    body = client.get("/studio/wt-funding-v3/deployments/panel").text
    assert "hx-trigger" in body

    store.set_deployment_status("dep-ks0002", "stopped")
    done = client.get("/studio/wt-funding-v3/deployments/panel").text
    assert "hx-trigger" not in done, "biten deployment'ı 2 saniyede bir sormaya devam"
