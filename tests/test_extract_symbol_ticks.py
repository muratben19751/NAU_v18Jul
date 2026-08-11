"""extract_symbol_ticks — sentetik bir gün dosyasıyla uçtan uca.

Gerçek arşive dokunmaz: tmp_path'e küçük bir `.csv.gz` yazılıp modülün kök
sabitleri oraya yönlendirilir. İddialar sözleşmeseldir — doğru sembol doğru
yola, tam satır sayısıyla; var olan gün yeniden taranmaz; yazım patlarsa
`.part` kalmaz.
"""

from __future__ import annotations

import csv
import gzip

import pytest

import extract_symbol_ticks as ex

_HEADER = ["ticker", "price", "size", "sip_timestamp"]
_ROWS = [
    ["AAPL", "304.1", "100", "1785830400046604073"],
    ["ZZZZ", "1.0", "5", "1785830400046604074"],
    ["SPY", "775.8", "200", "1785830400046604075"],
    ["AAPL", "304.2", "50", "1785830400046604076"],
]


@pytest.fixture
def day_file(tmp_path, monkeypatch):
    src = tmp_path / "src" / "trades_v1" / "2026" / "08"
    src.mkdir(parents=True)
    f = src / "2026-08-04.csv.gz"
    with gzip.open(f, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        w.writerows(_ROWS)
    monkeypatch.setattr(ex, "SRC_ROOT", tmp_path / "src")
    monkeypatch.setattr(ex, "OUT_ROOT", tmp_path / "out")
    return f


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_each_symbol_lands_in_its_own_tree_with_all_its_rows(day_file, tmp_path):
    total = ex.run("trades_v1", ("AAPL", "SPY"), workers=1)

    assert total == {"AAPL": 2, "SPY": 1}
    aapl = _read(ex.out_path("AAPL", "trades_v1", "2026-08-04"))
    assert [r["price"] for r in aapl] == ["304.1", "304.2"]
    assert {r["ticker"] for r in aapl} == {"AAPL"}  # ZZZZ sızmadı
    assert (tmp_path / "out" / "SPY" / "trades_v1" / "2026" / "08").exists()


def test_second_run_skips_without_reopening_the_source(day_file, monkeypatch):
    ex.run("trades_v1", ("AAPL", "SPY"), workers=1)

    def _boom(*a, **k):
        raise AssertionError("kaynak dosya yeniden açıldı")

    monkeypatch.setattr(ex.gzip, "open", _boom)
    assert ex.run("trades_v1", ("AAPL", "SPY"), workers=1) == {"AAPL": 0, "SPY": 0}


def test_failed_write_leaves_no_part(day_file, monkeypatch):
    import pyarrow as pa

    def _boom(*a, **k):
        raise OSError("disk doldu")

    monkeypatch.setattr(pa, "CompressedOutputStream", _boom)
    with pytest.raises(OSError, match="disk doldu"):
        ex.extract_day(str(day_file), "trades_v1", ("AAPL",), False)

    out_dir = ex.out_path("AAPL", "trades_v1", "2026-08-04").parent
    assert not list(out_dir.glob("*.part"))


def test_column_empty_early_then_list_valued_does_not_crash(tmp_path, monkeypatch):
    """quotes'un `indicators` sütunu: ilk satırlarda boş, sonra "12,2".

    pyarrow tipi blok blok tahmin ettiği için bu sütun once sayi sanılıyor ve
    ilerideki liste değerinde koşum çöküyordu (gerçek koşumda 131. günde).
    Blok boyutu küçültülerek aynı desen sentetik olarak üretilir.
    """
    src = tmp_path / "src" / "quotes_v1" / "2025" / "08"
    src.mkdir(parents=True)
    f = src / "2025-08-15.csv.gz"
    with gzip.open(f, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "bid_price", "indicators", "sip_timestamp"])
        for i in range(400):  # ilk bloklar: indicators boş
            w.writerow(["AAPL", "304.1", "", str(1785830400046604073 + i)])
        w.writerow(["AAPL", "304.2", "12,2", "1785830400046700000"])
    monkeypatch.setattr(ex, "SRC_ROOT", tmp_path / "src")
    monkeypatch.setattr(ex, "OUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(ex, "_BLOCK_SIZE", 1024)  # dosya birden çok bloğa yayılsın

    assert ex.run("quotes_v1", ("AAPL",), workers=1) == {"AAPL": 401}
    rows = _read(ex.out_path("AAPL", "quotes_v1", "2025-08-15"))
    assert rows[-1]["indicators"] == "12,2"


def test_one_bad_day_does_not_kill_the_run(tmp_path, monkeypatch, capsys):
    """Uzun koşumda tek bozuk gün: diğerleri yazılır, sonda hata bildirilir."""
    src = tmp_path / "src" / "trades_v1" / "2026" / "08"
    src.mkdir(parents=True)
    for day in ("2026-08-03", "2026-08-04"):
        with gzip.open(src / f"{day}.csv.gz", "wt", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(_HEADER)
            w.writerows(_ROWS)
    monkeypatch.setattr(ex, "SRC_ROOT", tmp_path / "src")
    monkeypatch.setattr(ex, "OUT_ROOT", tmp_path / "out")

    real = ex.extract_day

    def _flaky(src_path, *a, **k):
        if "2026-08-03" in src_path:
            raise ValueError("bozuk gün")
        return real(src_path, *a, **k)

    monkeypatch.setattr(ex, "extract_day", _flaky)
    with pytest.raises(ex.ExtractError, match="1 gün işlenemedi"):
        ex.run("trades_v1", ("AAPL",), workers=1)

    assert ex.out_path("AAPL", "trades_v1", "2026-08-04").exists()  # sağlam gün yazıldı
    assert "HATA 2026-08-03" in capsys.readouterr().out


def test_missing_range_is_an_actionable_error(day_file):
    with pytest.raises(ex.ExtractError, match="gün dosyası yok"):
        ex.run("trades_v1", ("AAPL",), start="2030-01-01", workers=1)
