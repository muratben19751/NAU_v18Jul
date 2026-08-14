"""/studio kabuğu — mod geçişi, düğüm taşıma ve sihirbaz adım kilitleri.

Bu dosyanın kapsadığı mantığın TAMAMI şablonlara gömülü inline JS'te yaşıyor,
yani `TestClient` ile erişilemez (DeepR 2026-08-11 [ORTA]). Sunucu tarafı
testler HTML'in doğru üretildiğini kanıtlıyor; burada kanıtlanan şey tarayıcının
onunla ne YAPTIĞI.

En kritik iddia `swMoveNodes`: `#result`, `#robustness-result` ve `#chat-thread`
SINGLETON düğümler (ilerleme fragment'ları onları `hx-target` olarak sabit
yazıyor), bu yüzden sihirbaz kopya sahiplenemiyor — mod değişince düğümler
yeniden EBEVEYNLENİYOR. Bu, sessizce bozulmaya birebir uygun bir tasarım: bir
düğüm yanlış yere taşınırsa sayfa normal görünür, yalnız canlı ilerleme
güncellemeleri görünmez bir DOM dalına akar. Sunucudan bakınca fark edilemez.

Wiki References: [[webapp_module_map]]
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_LIVE_SINGLETONS = ("result", "robustness-result", "chat-thread")


def _goto_studio(page):
    page.goto(f"{page.base}/studio", wait_until="load")
    page.wait_for_selector("#sw-wizard, .mode-btn", timeout=15000)


def _dismiss_brief(page):
    """AUTO brief çekmecesini kapat.

    Çekmece sayfa yüklenirken AÇIK gelir (`mcInitBrief`) ve MODAL'dır:
    `position: fixed; left: 0; width: 420px; z-index: 41`. Yani AUTO paneli
    aktifken mod düğmelerinin solda kalanlarını fiilen tıklanamaz yapar —
    `pointer events intercepted by .mc-drawer-head`. Kasıtlı bir tasarım
    (scrim + ✕ + Escape ile kapanır), ama tarayıcıda çalışan bir testin bunu
    bilmesi gerekir; ilk yazımda bilmiyordu ve dört test bu yüzden düştü.
    Bulgunun kendisi bu süitin niye var olduğunun kanıtı: hiçbir sunucu-tarafı
    test bu çakışmayı göremez.
    """
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.body.classList.contains('mc-brief-open')", timeout=5000
    )


def _pick_a_strategy(page):
    """Sihirbazın 1. adımındaki kapıyı geç.

    `swGoto` ilk satırı: ``if (n > 1 && !swState.mode) { swGoto(1); return; }``
    — yani bir şablon/kayıtlı strateji seçilmeden 2. adıma geçilemez. Bu
    doğru davranış; aşağıdaki 3. adım kapısı testlerinin ön koşulu.
    """
    page.click(".sw-card[data-sw-template]")
    page.wait_for_function(
        "() => !document.getElementById('sw-next-1').disabled", timeout=5000
    )


def _active_mode(page) -> str:
    return page.eval_on_selector(".mode-btn.active", "b => b.dataset.mode")


def _visible_step(page) -> int:
    return page.evaluate(
        "() => { const s = document.querySelector('.sw-step.active');"
        " return s ? Number(s.id.replace('sw-step-','')) : 0; }"
    )


class TestTheShellBootsWithoutJsErrors:
    def test_studio_loads_clean(self, page, assert_no_page_errors):
        """Çıpa: aşağıdaki her testin ön koşulu. Bir `ReferenceError` sayfayı
        görsel olarak bozmayabilir ama sonraki script bloğunu da öldürür."""
        _goto_studio(page)
        assert_no_page_errors()

    def test_a_fresh_visitor_lands_on_the_simple_wizard(self, page):
        _goto_studio(page)
        assert _active_mode(page) == "simple"
        assert _visible_step(page) == 1

    def test_the_vendored_frontend_bundles_actually_executed(self, page):
        """htmx ve Chart.js CDN'den yerel `web/static/`e alınmıştı; yüklenip
        yüklenmedikleri ancak tarayıcıda anlaşılır. htmx yoksa sayfadaki HİÇBİR
        düğme, poll ya da sekme çalışmaz."""
        _goto_studio(page)
        assert page.evaluate("() => typeof window.htmx") == "object"
        assert page.evaluate("() => typeof window.Chart") == "function"


class TestModeSwitching:
    @pytest.mark.parametrize("mode", ["manuel", "otonom", "simple"])
    def test_clicking_a_mode_button_activates_exactly_one_panel(self, page, mode):
        _goto_studio(page)
        page.click(f'.mode-btn[data-mode="{mode}"]')
        page.wait_for_timeout(150)

        assert _active_mode(page) == mode
        active = page.eval_on_selector_all(
            ".lab-modepanel.active", "els => els.map(e => e.id)"
        )
        assert active == [f"lab-modepanel-{mode}"]

    def test_the_choice_survives_a_reload(self, page):
        """`labMode` seçimi localStorage'a yazıyor; yazmazsa kullanıcı her
        yenilemede SIMPLE'a düşer."""
        _goto_studio(page)
        page.click('.mode-btn[data-mode="otonom"]')
        page.wait_for_timeout(150)

        _goto_studio(page)
        assert _active_mode(page) == "otonom"

    def test_an_unknown_saved_mode_falls_back_instead_of_blanking_the_page(self, page):
        """localStorage kullanıcı tarafından düzenlenebilir bir yüzey; bozuk
        değer hiçbir panelin açık olmadığı boş bir sayfa üretmemeli."""
        _goto_studio(page)
        page.evaluate("() => localStorage.setItem('studioMode', 'garbage')")
        _goto_studio(page)

        assert _active_mode(page) == "simple"
        assert len(page.query_selector_all(".lab-modepanel.active")) == 1


class TestTheLiveSingletonNodesSurviveModeSwitching:
    """Kaybolan bir düğüm sessizdir: sayfa normal görünür, canlı ilerleme
    görünmez bir dala akar."""

    @staticmethod
    def _present(page) -> dict[str, bool]:
        return {
            n: page.evaluate(f"() => !!document.getElementById('{n}')")
            for n in _LIVE_SINGLETONS
        }

    def test_no_node_is_lost_or_duplicated_across_a_full_mode_cycle(self, page):
        _goto_studio(page)
        before = self._present(page)

        for mode in ("manuel", "otonom", "simple", "manuel", "simple"):
            if page.evaluate("() => document.body.classList.contains('mc-brief-open')"):
                _dismiss_brief(page)
            page.click(f'.mode-btn[data-mode="{mode}"]')
            page.wait_for_timeout(120)

        after = self._present(page)
        assert after == before, "mod döngüsü canlı bir düğümü kaybetti"

        for node_id in _LIVE_SINGLETONS:
            n = page.evaluate(f"() => document.querySelectorAll('#{node_id}').length")
            assert n <= 1, f"#{node_id} ikizlendi ({n} kopya) — hx-target belirsizleşir"

    def test_in_simple_mode_the_result_node_lives_inside_the_wizard_slot(self, page):
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(150)

        parent = page.evaluate(
            "() => { const n = document.getElementById('result');"
            " return n && n.parentElement ? n.parentElement.id : null; }"
        )
        # Katalog boşken şablon düğümü doğrudan slot içinde üretir; doluyken
        # `swMoveNodes` taşır. İkisinde de varılacak yer aynı.
        assert parent == "sw-result-slot", f"result düğümü {parent} altında"

    def test_leaving_simple_returns_the_result_node_to_its_pro_home(self, page):
        """Pro evi VARSA düğüm oraya döner.

        Katalog boşken Pro gövdesi yalnız boş-durum panelini basar, yani
        `#bt-tabpanel-result` DOM'da yoktur ve düğüm slotta kalır — bu, o
        durumdaki tek doğru davranış (yok bir eve taşınamaz). Test her iki
        gerçeği de ayırt eder; yoksa boş katalogda yanlış yerden geçerdi.
        """
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(120)
        page.click('.mode-btn[data-mode="manuel"]')
        page.wait_for_timeout(150)
        assert _active_mode(page) == "manuel"

        state = page.evaluate(
            "() => ({ home: !!document.getElementById('bt-tabpanel-result'),"
            " parent: (document.getElementById('result')||{}).parentElement?.id ?? null })"
        )
        if not state["home"]:
            assert state["parent"] == "sw-result-slot"
        else:
            assert state["parent"] == "bt-tabpanel-result", (
                f"result düğümü {state['parent']} altında kaldı"
            )

    def test_the_pro_body_swap_never_leaves_a_duplicate_live_node(self, page):
        """Boş → dolu katalog geçişindeki İKİZLENME.

        Boş-durum paneli kendini yokluyor ve dolduğunda `#backtest-body-inner`
        TAM gövdeyle değişiyor — o gövde KENDİ `#result`/`#chat-thread`/
        `#robustness-result` düğümlerini getiriyor, sihirbazınkiler de yerinde
        duruyor. İki aynı id ile `hx-target="#result"` belirsizleşir: htmx
        ilkini seçer, ilerleme gizli slota akar, kullanıcı hiçbir hata görmeden
        sonucunu kaybeder. Burada swap elle tetiklenir (katalog dolmasını
        beklemeden).
        """
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(120)

        # Pro gövdesinin tam sürümünü zorla: aynı OOB-benzeri swap, poll'u
        # beklemeden.
        page.evaluate(
            """() => {
              const inner = document.getElementById('backtest-body-inner');
              if (!inner) return;
              const extra = document.createElement('div');
              extra.innerHTML = '<div id="bt-tabpanel-result"><div id="result"></div></div>'
                + '<div id="rob-home"><div id="robustness-result"></div></div>'
                + '<div id="bt-tabpanel-plan"><div id="chat-thread"></div></div>';
              inner.appendChild(extra);
              htmx.trigger(inner, 'htmx:afterSwap');
            }"""
        )
        page.wait_for_timeout(200)

        for node_id in _LIVE_SINGLETONS:
            n = page.evaluate(
                f"() => document.querySelectorAll('[id=\"{node_id}\"]').length"
            )
            assert n == 1, f"#{node_id} ikizlendi ({n} kopya) — hx-target belirsizleşir"


class TestTheWizardStepGate:
    """Adım 4-5 tamamlanmış bir backtest'e bağlı. Kapı yalnız JS'te var."""

    def test_continue_to_reliability_starts_locked(self, page):
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(120)

        assert page.eval_on_selector("#sw-next-3", "b => b.disabled") is True
        assert "Run the backtest first" in page.eval_on_selector(
            "#sw-next-3", "b => b.title"
        )

    def test_jumping_past_the_gate_from_the_rail_is_refused_with_a_reason(self, page):
        """Kilitli düğmeyi atlamak mümkün: üstteki adım rayı doğrudan
        `swGoto(4)` çağırıyor. Kapı orada da tutmalı — ve kullanıcıya NEDEN
        tutulduğunu söylemeli, sessizce geri atmamalı."""
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(120)
        _pick_a_strategy(page)
        page.click('.sw-rail-step[data-sw-step="3"]')
        page.wait_for_timeout(120)

        rail4 = page.query_selector('.sw-rail-step[data-sw-step="4"]')
        assert rail4.is_disabled(), "kapı aşıldı — ray adımı 4 tıklanabilir"
        # Ölü bir düğme sebebini SÖYLEMELİ: yanı başındaki #sw-next-3 bir
        # `title` taşıyor, ray taşımıyordu (kullanıcı tıklıyor, hiçbir şey
        # olmuyor, gerekçe hiçbir yerde yok).
        assert "backtest" in (rail4.get_attribute("title") or "").lower(), (
            "kilitli ray adımı gerekçe göstermiyor"
        )
        assert _visible_step(page) == 3

    def test_the_earlier_steps_stay_freely_navigable(self, page):
        """Kapı yalnız 4+ için. 1↔2↔3 serbest kalmalı, yoksa kullanıcı seçimini
        düzeltmek için sayfayı yenilemek zorunda kalır."""
        _goto_studio(page)
        page.click('.mode-btn[data-mode="simple"]')
        page.wait_for_timeout(120)
        _pick_a_strategy(page)

        for step in (2, 3, 1, 3):
            page.click(f'.sw-rail-step[data-sw-step="{step}"]')
            page.wait_for_timeout(100)
            assert _visible_step(page) == step


class TestTheAutoBriefKeepsItsRangeInSyncWithTheSelection:
    """`mcSyncRange`: aralık SEÇİME bağlıdır, bir kez doldurulup bırakılamaz.

    Bu tam olarak `RuntimeError: Insufficient data (63 bars, QQQC.NASDAQ
    1-HOUR)` hatasının kaynağıydı — MAX brief açılırken BTCUSDT için bir kez
    basılmış, sonra sembol değişmişti ve tarihler eski sembolünkinde kalmıştı.
    """

    def test_changing_the_symbol_refills_the_date_range(self, page):
        _goto_studio(page)
        page.click('.mode-btn[data-mode="otonom"]')
        page.wait_for_timeout(200)

        n_opts = page.eval_on_selector("#mc-symbol", "s => s.options.length")
        if n_opts < 2:
            pytest.fail(
                "AUTO brief'inde tek sembol var — bu iddia sınanamadı. "
                "E2E sunucusunun kataloğu tohumlanmalı (sessiz geçiş yerine hata)."
            )

        calls = page.evaluate(
            "() => { window.__syncCalls = 0;"
            " const orig = window.mcSyncRange;"
            " window.mcSyncRange = function () { window.__syncCalls++; return orig.apply(this, arguments); };"
            " return window.__syncCalls; }"
        )
        assert calls == 0

        page.select_option("#mc-symbol", index=1)
        page.wait_for_timeout(250)

        assert page.evaluate("() => window.__syncCalls") >= 1, (
            "sembol değişti ama tarih aralığı tazelenmedi — eski sembolün "
            "penceresiyle koşulur"
        )

    def test_changing_the_category_refills_the_date_range(self, page):
        _goto_studio(page)
        page.click('.mode-btn[data-mode="otonom"]')
        page.wait_for_timeout(200)

        page.evaluate(
            "() => { window.__syncCalls = 0;"
            " const orig = window.mcSyncRange;"
            " window.mcSyncRange = function () { window.__syncCalls++; return orig.apply(this, arguments); }; }"
        )
        current = page.eval_on_selector("#mc-category", "s => s.value")
        page.select_option("#mc-category", "spot" if current != "spot" else "linear")
        page.wait_for_timeout(250)

        assert page.evaluate("() => window.__syncCalls") >= 1
