"""`_LAST_RESULT` — oturum başına son backtest sonucu, üç sınırla.

Yuva canlı `IterationResult`'ı tutuyor: işlem listesi + öz sermaye eğrisi.
ÖLÇÜLDÜ (2026-08-17, 120k barlık gerçek koşu, 451 işlem): pickle protokol 5
ile 565.136 B ≈ 0,57 MB. Eski 500'lük tavan bunun ~283 MB'ı demekti ve tam
menzilli 1m koşuları çok daha fazla işlem üretiyor.

Ama asıl kusur tavanın büyüklüğü değildi: tahliye `next(iter(...))` ile İLK
EKLENENİ atıyordu, en az kullanılanı değil — yani operatörün aylardır açık
duran sekmesi, yeni sid'ler geldikçe ÖNCE düşen yuvaydı. Üstelik `sid`
istemcinin gönderdiği çerez değeri: doğrulanmıyor, imzalanmıyor, süresi
dolmuyordu.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

import pytest

from web.routes import backtest as bt


@pytest.fixture(autouse=True)
def clean_slots():
    with bt._LAST_RESULT_LOCK:
        bt._LAST_RESULT.clear()
    yield
    with bt._LAST_RESULT_LOCK:
        bt._LAST_RESULT.clear()


def _put(sid: str, name: str = "s") -> None:
    bt._last_result_set(sid, r=object(), spec_name=name, narrative="", bars_info={})


def test_the_cap_holds():
    for i in range(bt._MAX_RESULT_SESSIONS + 20):
        _put(f"sid{i}")

    assert len(bt._LAST_RESULT) == bt._MAX_RESULT_SESSIONS


def test_the_slot_being_read_is_not_the_one_evicted():
    """LRU'nun bütün mesele olduğu yer. Eski kod ilk ekleneni atıyordu:
    tek operatörlü bir uygulamada bu, TAM DA aktif sekmenin yuvasıdır."""
    _put("operator")
    for i in range(bt._MAX_RESULT_SESSIONS - 1):
        _put(f"filler{i}")

    # Operatör sonucuna bakıyor — yuva tazeleniyor.
    assert bt.last_result_get("operator")["spec_name"] == "s"

    _put("newcomer")  # tavan doldu, biri düşecek

    assert "operator" in bt._LAST_RESULT, "okunan yuva tahliye edildi"
    assert "filler0" not in bt._LAST_RESULT, "en az kullanılan yuva korundu"


def test_a_stale_slot_expires(monkeypatch):
    """`sid` doğrulanmamış bir çerez; tavan tek savunma olamaz. Saatlerdir
    kimsenin bakmadığı bir sonuç zaten ölü."""
    clock = [1_000_000.0]
    monkeypatch.setattr(bt.time, "time", lambda: clock[0])
    _put("old")
    assert bt.last_result_get("old")["spec_name"] == "s"

    clock[0] += bt._RESULT_TTL_S + 1

    assert bt.last_result_get("old")["spec_name"] is None
    assert "old" not in bt._LAST_RESULT


def test_a_slot_inside_the_ttl_survives(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(bt.time, "time", lambda: clock[0])
    _put("fresh")

    clock[0] += bt._RESULT_TTL_S - 1

    assert bt.last_result_get("fresh")["spec_name"] == "s"


def test_the_read_contract_did_not_change():
    """`at` iç bir alan. `web/routes/studio.py` de bu yuvayı okuyor; ona yeni
    bir anahtar sızdırmak sözleşmeyi sessizce değiştirirdi."""
    _put("x")

    slot = bt.last_result_get("x")

    assert set(slot) == set(bt._empty_result_slot())
    assert "at" not in slot


def test_the_cap_is_sized_from_the_measurement():
    """Ölçüm yorumda; sayı ondan türedi. Tavanı büyütmek isteyen önce o
    ölçümü yenilemek zorunda kalsın."""
    assert bt._MAX_RESULT_SESSIONS <= 50, (
        "0,57 MB'lık yuvalarla 50'nin üstü ölçülmemiş bir bellek taahhüdü"
    )
