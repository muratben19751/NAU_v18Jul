"""`regression_baseline.json` artık gerçekten bir çıpa — çünkü bu test onu OKUYOR.

Bir taban çizgisi dosyası, onu okuyan bir test yoksa çıpa değildir. Eski hâli
tam olarak buydu: dosya vardı, wiki'de "bit-identical parity" iddiası ona
dayanıyordu, ama hiçbir test/CI adımı açmıyordu — ve açamazdı da, çünkü betiğin
penceresi ``datetime.now() - 7 gün`` ile canlı borsadan geliyordu ve sayılar
bir daha üretilemiyordu.

Bu dosya çerçeveyi, spec'leri ve koşucuyu KENDİ TANIMLAMIYOR; üçünü de
``capture_baseline``'dan içeri alıyor. İki kopya kaçınılmaz olarak ayrışır —
bu projede "aynı cümle iki alt sistemde iki ölçü" tekrarlayan bir arıza sınıfı,
ve bu oturumda aynı spec'in iki yürütme yolunda farklı pnl verdiği ölçüldü.

Sayılar kasıtlı değiştiyse: ``python capture_baseline.py`` ve diff'te
"motorun cevabı değişti" yazsın — sessizce kaymasındansa.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import capture_baseline as CB

BASELINE = Path(__file__).resolve().parent.parent / "regression_baseline.json"


@pytest.fixture(scope="module")
def baseline() -> dict:
    if not BASELINE.exists():
        pytest.fail(
            f"{BASELINE.name} yok — `python capture_baseline.py` ile üretilmeli"
        )
    return json.loads(BASELINE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Dosyanın kendisi
# ---------------------------------------------------------------------------


def test_the_file_records_how_it_was_measured(baseline):
    """Köken olmadan çıpa, bir sonraki kırılmada 'ne değişti'yi cevaplayamaz."""
    p = baseline["provenance"]
    assert p["nautilus_version"] and p["python"] and p["platform"]
    # Yürütme yolu kimliğin parçası: aynı spec+barlar iki yolda farklı pnl
    # veriyor (ölçüldü 2026-08-21), yani "hangi yol" kaydedilmezse sayı anlamsız.
    assert p["execution_path"] == CB.EXECUTION_PATH
    assert p["recipe"] == CB.RECIPE
    f = p["frame"]
    assert (f["bars"], f["seed"], f["start"], f["freq"]) == (
        CB.BARS,
        CB.FRAME_SEED,
        CB.FRAME_START,
        CB.FRAME_FREQ,
    )


def test_the_stored_spec_definitions_match_the_code(baseline):
    """Dosya, kodun ürettiğinden BAŞKA spec'ler taşıyorsa çıpa yalan söyler."""
    assert baseline["specs"] == CB.spec_definitions()


def test_the_file_carries_no_volatile_field(baseline):
    """`created_at` gibi alanlar her yeniden üretimde anlamsız diff çıkarır.

    Aynı davranış (from_dict her çağrıda o anın damgasını basıyor) bu oturumda
    bir testi aralıklı kırmıştı; dosyaya sızmasına izin verilmiyor.
    """
    blob = json.dumps(baseline)
    assert "created_at" not in blob


# ---------------------------------------------------------------------------
# Girdi — sayılardan ÖNCE
# ---------------------------------------------------------------------------


def test_the_anchor_frame_has_not_moved(baseline):
    """Girdi değişmediyse çıktının değişmesi motorun haberidir.

    Bu test sayısal karşılaştırmadan ÖNCE gelir ve platformdan bağımsızdır:
    "girdiyi değiştirdim" ile "motorun cevabı değişti" ayrı kalsın diye.
    """
    assert (
        CB.frame_fingerprint(CB.anchor_frame())
        == baseline["provenance"]["frame"]["fingerprint"]
    )


def test_the_frame_is_generated_not_fetched():
    """Ağsız ve önbeleksiz: çıpanın her makinede aynı olmasının şartı."""
    import inspect

    src = inspect.getsource(CB.anchor_frame)
    for forbidden in ("load_bybit_bars", "load_external_bars", "read_parquet", "now("):
        assert forbidden not in src, f"çerçeve dış kaynağa bağlanmış: {forbidden}"


def test_every_spec_definition_is_valid_against_the_catalog():
    """Uydurma parametre adı SESSİZCE varsayılana düşer.

    Bu gerçekten oldu: `price_breakout` için `direction="up"` (doğrusu "high"),
    `bollinger_break` için `std`/`direction` (doğrusu `k`/`side`/`mode`),
    `momentum` için `period`/`threshold` (doğrusu `lookback`/`sign`) yazılmıştı.
    Biri sıfır işlem açtı; ötekiler işlem açtı ama YAZILANI değil VARSAYILANI
    çiviliyordu — sessiz olanı fark etmek çok daha zor.
    """
    CB.validate_spec_defs()  # geçersizse ValueError


# ---------------------------------------------------------------------------
# Sayılar
# ---------------------------------------------------------------------------


def test_no_anchor_row_is_too_thin_to_anchor_anything(baseline):
    """Sıfır ya da tek işlemli bir satır neredeyse hiçbir şeyi çivilemez."""
    thin = {
        k: v.get("n_trades")
        for k, v in baseline["results"].items()
        if not v.get("error") and (v.get("n_trades") or 0) < 10
    }
    assert not thin, f"çok az işlemli çıpa satırları: {thin}"


def test_no_spec_errored(baseline):
    errs = {k: v["error"] for k, v in baseline["results"].items() if v.get("error")}
    assert not errs, errs


@pytest.mark.parametrize("name", sorted(CB._SPEC_DEFS))
def test_the_engine_still_returns_the_same_numbers(baseline, name):
    """Asıl çıpa. Sayı kayarsa bu kırılır ve bu DOĞRU davranıştır.

    Platform kapısı dosyanın KENDİ kaydına bakıyor, sabit bir 'win32'ye değil:
    CI'da hem windows hem ubuntu ayağı var ve motorun OS'lar arası bit-birebir
    aynılığı doğrulanmadı. Çıpanın amacı gerileme yakalamak, platform farkını
    gerileme diye sunmak değil.
    """
    recorded_platform = baseline["provenance"]["platform"]
    if sys.platform != recorded_platform:
        pytest.skip(
            f"çıpa {recorded_platform}'da ölçüldü; bu platformun sayıları henüz "
            "kaydedilmedi (girdi, şema ve determinizm testleri yine koşuyor)"
        )
    want = baseline["results"][name]
    r = CB.run_anchor(CB.anchor_specs()[name], CB.anchor_frame())
    assert r.error is None, r.error
    m = r.metrics or {}
    assert m.get("n_trades") == want["n_trades"]
    assert m.get("pnl") == pytest.approx(want["pnl"], abs=1e-6)
    if want["sharpe"] is not None:
        assert m.get("sharpe") == pytest.approx(want["sharpe"], rel=1e-9)


def test_two_runs_of_the_same_input_agree():
    """Determinizm iddiasının kendisi — platformdan bağımsız.

    Bir tohum/sıralama sızıntısı çıpayı "bazen kırılan" bir teste çevirirdi ve
    o, sayıların kaymasından daha kötü olurdu: kimse ona güvenmez.
    """
    frame = CB.anchor_frame()
    spec = CB.anchor_specs()["ma_cross+atr_stop"]
    a, b = CB.run_anchor(spec, frame), CB.run_anchor(spec, frame)
    assert (a.metrics or {}).get("pnl") == (b.metrics or {}).get("pnl")
    assert (a.metrics or {}).get("sharpe") == (b.metrics or {}).get("sharpe")
    assert (a.metrics or {}).get("n_trades") == (b.metrics or {}).get("n_trades")


def test_losing_rows_do_not_carry_a_positive_sharpe(baseline):
    """Tarihsel kayıttaki desenin geri gelmediğinin bekçisi.

    `regression_baseline_2026-07-23_historical.json`: 466 zararlı koşunun
    461'i (%99) POZİTİF Sharpe taşıyor, +71,9'a kadar. Bugünkü kod bu deseni
    üretmiyor ve bu satırlar sabit bir çerçevede sabit spec'lerle ölçüldüğü
    için burada bekçilik edebilir.

    NOT: bu evrensel bir yasa DEĞİL — `sharpe` bar çözünürlüklü MTM eğrisinden,
    `pnl` gerçekleşen işlemlerden gelir, yani ilkesel olarak ayrışabilirler.
    Çivilenen şey BU çerçevede ölçülen davranış; kasıtlı bir değişiklikte
    çıpayla birlikte bu da güncellenir.
    """
    odd = {
        k: (v["pnl"], v["sharpe"])
        for k, v in baseline["results"].items()
        if not v.get("error")
        and v.get("pnl") is not None
        and v.get("sharpe") is not None
        and v["pnl"] < 0
        and v["sharpe"] > 0
    }
    assert not odd, f"zararda ama pozitif Sharpe: {odd}"
