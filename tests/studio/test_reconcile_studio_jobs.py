"""Restart sonrası sahipsiz kalan 'running' satırların uzlaştırılması.

DeepR 2026-08-11 [YÜKSEK]: `_reconcile_deployments()` yalnız `deployments`
tablosunu uzlaştırıyordu; kardeş üç tablo (`studio_runs`, `optimize_runs`,
`ai_loops`) hiç uzlaştırılmıyordu. Üç iş de Starlette `BackgroundTasks` ile
uygulama sürecinde koşuyor, yani pm2 restart'ı / çökme satırı SONSUZA DEK
`running` bırakıyor ve onu bitirecek kimse kalmıyordu. Görünen sonuçları:

* `_footer_metrics.html` `run.status == "running"` iken her 2 saniyede bir
  `/studio/{id}/runs/latest` poll'lar -- bitmeyecek bir koşu için, her açık
  sekmeden.
* `route_loop_start`'ın "a loop is already running" kontrolü o strateji için
  KALICI 422 verir: DB elle düzeltilmeden AI loop bir daha başlatılamaz.
* Optimizer paneli aynı şekilde döner durur.

Bir gün önce aynı sınıftan bir hata AUTO oturumlarında bulunmuştu (sessions.py
`session_end` yoksa koşuyu sonsuza dek "Running" gösteriyordu); aynı ders.

Durum `failed` DEĞİL `interrupted` olur: başarısızlık iş hakkında bir yargıdır,
kesinti ise "sonucunu asla öğrenemeyeceğiz" demektir -- ikisini aynı kefeye
koymak kullanıcıya olmamış bir arıza raporlamaktır.

Testler tmp bir DB kullanır; CANLI `studio.db`'ye asla yazılmaz.

Wiki References
---------------
See: [[strategy_studio]].
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SID = "wt-funding-v3"


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Süreç-içi kayıtlar testler arasında sızmasın."""
    import web.routes.strategy_studio as main

    main._ACTIVE_JOBS.clear()
    main._JOBS_RECONCILED.clear()
    yield
    main._ACTIVE_JOBS.clear()
    main._JOBS_RECONCILED.clear()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from scripts.seed_studio import build_fixture
    from strategy_studio.store import StrategyStore
    from web.routes import strategy_studio as main

    st = StrategyStore(tmp_path / "t.db")
    st.save(build_fixture())
    monkeypatch.setattr(main, "store", st)
    return st


@pytest.fixture()
def client(store):
    from server import app as _host

    c = TestClient(_host)
    c.store = store
    return c


def _orphan_run(store, run_id="orphan-run"):
    """Önceki süreçten kalmış, kimsenin bitiremeyeceği bir backtest satırı."""
    store.create_run(run_id, SID, 1, False, "deadbeef")
    return run_id


class TestStoreLiveJobs:
    def test_it_lists_running_rows_from_all_three_tables(self, store):
        store.create_run("r1", SID, 1, False)
        store.create_opt("o1", SID, 1, False)
        store.create_loop("l1", SID, "{}")

        jobs = {(j["kind"], j["job_id"]) for j in store.live_jobs()}

        assert jobs == {("run", "r1"), ("optimize", "o1"), ("loop", "l1")}

    def test_finished_rows_are_not_live(self, store):
        store.create_run("r1", SID, 1, False)
        store.finish_run("r1", "{}")
        store.create_opt("o1", SID, 1, False)
        store.fail_opt("o1", "boom")
        store.create_loop("l1", SID, "{}")
        store.finish_loop("l1", "done", "bitti")

        assert store.live_jobs() == []

    def test_interrupt_writes_status_reason_and_finished_at(self, store):
        store.create_run("r1", SID, 1, False)

        store.interrupt_job("run", "r1", "süreç öldü")

        row = store.get_run("r1")
        assert row["status"] == "interrupted"
        assert row["error"] == "süreç öldü"
        assert row["finished_at"]

    def test_interrupt_uses_the_note_column_for_loops(self, store):
        store.create_loop("l1", SID, "{}")

        store.interrupt_job("loop", "l1", "süreç öldü")

        row = store.latest_loop(SID)
        assert row["status"] == "interrupted"
        assert row["note"] == "süreç öldü"

    def test_a_row_that_finished_meanwhile_is_not_overwritten(self, store):
        """Yarış: uzlaştırma karar verirken iş gerçekten bitmiş olabilir."""
        store.create_run("r1", SID, 1, False)
        store.finish_run("r1", '{"dsr": 1.0}')

        store.interrupt_job("run", "r1", "süreç öldü")

        assert store.get_run("r1")["status"] == "done"


class TestReconciliation:
    def test_an_orphan_row_becomes_interrupted_not_failed(self, store):
        import web.routes.strategy_studio as main

        _orphan_run(store)

        assert main._reconcile_studio_jobs() == 1
        row = store.get_run("orphan-run")
        assert row["status"] == "interrupted"
        assert row["status"] != "failed"
        assert "yeniden başladı" in row["error"]

    def test_a_job_running_in_this_process_is_left_alone(self, store):
        """Uzlaştırma canlı bir koşuyu öldürmemeli."""
        import web.routes.strategy_studio as main

        store.create_run("mine", SID, 1, False)
        main._claim_job("mine")

        assert main._reconcile_studio_jobs() == 0
        assert store.get_run("mine")["status"] == "running"

    def test_a_released_job_is_reconcilable_again(self, store):
        import web.routes.strategy_studio as main

        store.create_run("mine", SID, 1, False)
        main._claim_job("mine")
        main._release_job("mine")  # görev bitti ama satır 'running' kaldıysa

        assert main._reconcile_studio_jobs() == 1

    def test_all_three_kinds_are_reconciled(self, store):
        import web.routes.strategy_studio as main

        store.create_run("r1", SID, 1, False)
        store.create_opt("o1", SID, 1, False)
        store.create_loop("l1", SID, "{}")

        assert main._reconcile_studio_jobs() == 3
        assert store.get_run("r1")["status"] == "interrupted"
        assert store.latest_opt(SID)["status"] == "interrupted"
        assert store.latest_loop(SID)["status"] == "interrupted"

    def test_it_runs_once_per_database(self, store):
        """Her istekte tüm tabloları taramak yerine süreç başına bir kez."""
        import web.routes.strategy_studio as main

        main._reconcile_studio_jobs_once()
        _orphan_run(store)  # bu satır uzlaştırmadan SONRA doğdu
        main._reconcile_studio_jobs_once()

        assert store.get_run("orphan-run")["status"] == "running"

    def test_a_reconciliation_failure_is_logged_not_fatal(
        self, store, caplog, monkeypatch
    ):
        import web.routes.strategy_studio as main

        def _boom():
            raise RuntimeError("db locked")

        monkeypatch.setattr(main, "_reconcile_studio_jobs", _boom)

        with caplog.at_level("WARNING"):
            main._reconcile_studio_jobs_once()  # patlamamalı

        assert "reconciliation failed" in caplog.text


class TestRoutesStopLying:
    def test_the_footer_poll_stops_after_reconciliation(self, client):
        """Denetlenen semptom: `hx-trigger="every 2s"` sonsuza kadar dönüyordu."""
        _orphan_run(client.store)

        r = client.get(f"/studio/{SID}/runs/latest")

        assert r.status_code == 200
        assert "every 2s" not in r.text
        assert "Interrupted" in r.text
        assert client.store.get_run("orphan-run")["status"] == "interrupted"

    def test_the_side_panel_stops_polling_a_dead_optimizer(self, client):
        client.store.create_opt("orphan-opt", SID, 1, False)

        r = client.get(f"/studio/{SID}/optimize/panel")

        assert "every 2s" not in r.text
        assert "Last run interrupted" in r.text
        assert client.store.latest_opt(SID)["status"] == "interrupted"

    def test_a_new_ai_loop_can_start_after_a_restart(self, client, monkeypatch):
        """KALICI 422'nin düzeldiği yer: DB'ye elle dokunmadan yeni loop."""
        from web.routes import strategy_studio as main

        client.store.create_loop("orphan-loop", SID, "{}")
        # Gerçek loop bir LLM çağrısıdır; burada test edilen kapı, döngü değil.
        monkeypatch.setattr(main, "_execute_loop", lambda *a, **k: None)

        r = client.post(f"/studio/{SID}/ai/loop/start", data={"max_iterations": "1"})

        assert r.status_code == 200
        assert "already running" not in r.text
        assert client.store.latest_loop(SID)["loop_id"] != "orphan-loop"

    def test_a_loop_genuinely_running_in_this_process_still_blocks(
        self, client, monkeypatch
    ):
        """Uzlaştırma, çalışan bir loop'un kapısını AÇMAMALI."""
        from web.routes import strategy_studio as main

        client.store.create_loop("live-loop", SID, "{}")
        main._claim_job("live-loop")
        monkeypatch.setattr(main, "_execute_loop", lambda *a, **k: None)

        r = client.post(f"/studio/{SID}/ai/loop/start", data={"max_iterations": "1"})

        assert r.status_code == 422
        assert "already running" in r.text

    def test_a_backtest_started_now_is_not_marked_interrupted(self, client):
        """Rotada sahiplenme: satır doğduğu an uzlaştırmaya karşı korumalı."""
        r = client.post(f"/studio/{SID}/backtest")

        assert r.status_code == 200
        # TestClient arka plan görevini istek dönüşünde koşturur → 'done'.
        assert client.store.latest_run(SID)["status"] == "done"
