"""repair_massive_intraday.py — DeepR 2026-08-09 [YÜKSEK]: repair_day() used
to only patch derived-TF Parquet files, never the underlying 1-MINUTE
catalog. ingest_equities.build_tf_bars() regenerates every TF from scratch
from the 1-MINUTE catalog on each run (rmtree + resample), so a TF-only
repair was silently undone by the next routine ingest. This file locks down
that repair_day() now ALSO patches the 1-MINUTE source for the target day.

Note: repair_massive_intraday.py is the user's own standalone script
(untracked, out of the NAU app's git scope) — this test file is likewise
kept untracked/out of the commit, per explicit scope confirmation.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import repair_massive_intraday as rmi

_SCHEMA = pa.schema(
    [
        ("open", pa.binary(8)),
        ("high", pa.binary(8)),
        ("low", pa.binary(8)),
        ("close", pa.binary(8)),
        ("volume", pa.binary(8)),
        ("ts_event", pa.int64()),
        ("ts_init", pa.int64()),
    ]
)


def _decode_fixed(b: bytes) -> float:
    return int.from_bytes(b, "little", signed=True) / 1_000_000_000


def _seed_day(path, day: date, *, close_value: float) -> None:
    """Write one bad RTH session (390 1-minute bars, all `close_value`) as
    the 'existing catalog' content repair_day() must overwrite."""
    idx = pd.date_range(
        f"{day}T13:31:00Z", periods=390, freq="1min"
    )  # 09:31-16:00 America/New_York in UTC (DST-naive fixture, fine for this test)
    # .as_unit("ns"): pd.date_range defaults to non-ns resolution in this
    # pandas version; ts_event must be ns to match _replace_day()'s
    # pd.Timestamp(...).value boundaries (always ns) — same bug class as the
    # production fix in repair_massive_intraday.py.
    ts = idx.as_unit("ns").view("int64")
    n = len(idx)
    table = pa.Table.from_arrays(
        [
            pa.array(rmi._fixed([close_value] * n, 2), type=pa.binary(8))
            for _ in range(5)  # open/high/low/close/volume all same stub value
        ]
        + [pa.array(ts, type=pa.int64()), pa.array(ts, type=pa.int64())],
        schema=_SCHEMA,
    )
    path.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path / "data.parquet", compression="zstd")


@pytest.fixture
def catalog(tmp_path):
    root = tmp_path / "data" / "bar"
    day = date(2024, 3, 4)
    _seed_day(root / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL", day, close_value=1.0)
    for _tf, (_rule, spec) in rmi.TFS.items():
        _seed_day(root / f"QQQ.NASDAQ-{spec}-LAST-EXTERNAL", day, close_value=1.0)
    return tmp_path, day


def _fake_minute_rows(day: date, *, close_value: float) -> list[dict]:
    """390 corrected RTH 1-minute bars for `day` in Massive's raw shape.

    `t` is the bar's WINDOW START (repair_day() shifts it +1min to get the
    close/label time before RTH-filtering) — 09:30..15:59 America/New_York,
    localized properly (not a hardcoded UTC offset) so this is correct
    regardless of DST.
    """
    start = pd.Timestamp(f"{day} 09:30:00", tz="America/New_York")
    idx = pd.date_range(start, periods=390, freq="1min", tz="America/New_York")
    return [
        {
            "o": close_value,
            "h": close_value,
            "l": close_value,
            "c": close_value,
            "v": 100.0,
            "t": int(ts.tz_convert("UTC").value // 1_000_000),  # epoch millis
        }
        for ts in idx
    ]


def _patch_fetch(monkeypatch, day: date, *, close_value: float) -> None:
    # api_key() is evaluated as a call-site argument to fetch_minute_aggs
    # inside repair_day() — it raises before the (mocked) fetch function
    # itself would ever run, so it needs patching too.
    monkeypatch.setattr(rmi, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        rmi,
        "fetch_minute_aggs",
        lambda *a, **k: (_fake_minute_rows(day, close_value=close_value), "ok"),
    )


def test_repair_day_also_patches_the_1_minute_source(catalog, monkeypatch):
    tmp_path, day = catalog
    _patch_fetch(monkeypatch, day, close_value=42.0)

    rmi.repair_day("QQQ", day, tmp_path)

    minute_dir = tmp_path / "data" / "bar" / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    written = next(minute_dir.glob("*.parquet"))
    table = pq.read_table(written)
    closes = [_decode_fixed(b.as_py()) for b in table["close"]]
    assert closes, "1-MINUTE file has no rows after repair"
    assert all(c == pytest.approx(42.0) for c in closes), (
        "1-MINUTE source still holds the stale close value — the repair "
        "only patched derived TFs, exactly the regression this test guards"
    )


def test_repair_day_returns_before_after_counts_for_1_minute_too(catalog, monkeypatch):
    tmp_path, day = catalog
    _patch_fetch(monkeypatch, day, close_value=42.0)

    result = rmi.repair_day("QQQ", day, tmp_path)

    assert "1-MINUTE" in result
    before, after = result["1-MINUTE"]
    assert before == 390
    assert after == 390  # same day fully replaced, not appended


def test_subsequent_tf_rebuild_from_repaired_minute_data_no_longer_undoes_the_fix(
    catalog, monkeypatch
):
    """The exact regression scenario from the DeepR finding: repair, then
    simulate build_tf_bars() regenerating 5-MINUTE purely from the 1-MINUTE
    catalog, and confirm the corrected value survives."""
    tmp_path, day = catalog
    _patch_fetch(monkeypatch, day, close_value=42.0)
    rmi.repair_day("QQQ", day, tmp_path)

    minute_dir = tmp_path / "data" / "bar" / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    table = pq.read_table(next(minute_dir.glob("*.parquet")))
    closes = [_decode_fixed(b.as_py()) for b in table["close"]]

    # This is what build_tf_bars() would derive a 5-MINUTE bar from — if the
    # 1-MINUTE repair hadn't happened, this would still read 1.0 (the stale
    # seed value), not 42.0.
    assert all(c == pytest.approx(42.0) for c in closes)


# ---------------------------------------------------------------------------
# DeepR 2026-08-09 [ORTA] follow-ups
# ---------------------------------------------------------------------------


class TestFixedValidation:
    def test_finite_values_round_trip(self):
        encoded = rmi._fixed([1.5, -2.25, 0.0], 2)
        assert [_decode_fixed(b) for b in encoded] == [1.5, -2.25, 0.0]

    def test_nan_raises_a_clear_error_identifying_the_row(self):
        with pytest.raises(ValueError, match="non-finite.*row 1"):
            rmi._fixed([1.0, float("nan"), 3.0], 2)

    def test_infinity_raises_a_clear_error(self):
        with pytest.raises(ValueError, match="non-finite.*row 0"):
            rmi._fixed([float("inf")], 2)

    def test_a_value_too_large_for_signed_int64_is_rejected(self):
        # 1e10 at 1e9 scale -> 1e19, over the signed-8-byte ceiling (~9.2e18).
        with pytest.raises(ValueError, match="does not fit"):
            rmi._fixed([1e10], 2)


class TestFixedHonoursPrecision:
    """DeepR 2026-08-11 [ORTA]: `precision` gövdede hiç okunmuyordu.

    Ölü parametre değil, YANLIŞ parametreydi: onarılan gün katalogdaki
    komşularından farklı bir sözleşmeyle kodlanıyordu — oysa modülün tüm
    varlık sebebi "onarım da ingest ile AYNI kuralları kullanmalı".
    Doğruluk ölçütü Nautilus'un kendi `Price`/`Quantity` raw değeri.
    """

    @staticmethod
    def _raw(encoded: bytes) -> int:
        return int.from_bytes(encoded, "little", signed=True)

    @pytest.mark.parametrize(
        "value", [10.126, 10.125, 10.124, 0.005, 175.4, 1.005, 1.5, -2.25, 0.0]
    )
    def test_price_encoding_matches_nautilus_price(self, value):
        from nautilus_trader.model.objects import Price

        assert self._raw(rmi._fixed([value], 2)[0]) == Price(value, 2).raw

    @pytest.mark.parametrize("value", [175.4, 175.5, 0.4, 12345.0])
    def test_volume_encoding_matches_nautilus_quantity(self, value):
        """Hacim `precision=0` ile çağrılıyor: 175.4 → 175, 175.4 DEĞİL."""
        from nautilus_trader.model.objects import Quantity

        assert self._raw(rmi._fixed([value], 0)[0]) == Quantity(value, 0).raw

    def test_precision_actually_changes_the_encoding(self):
        """Aynı değer farklı precision'la farklı kodlanmalı — parametrenin
        okunduğunun en kısa kanıtı."""
        assert rmi._fixed([1.234], 2) != rmi._fixed([1.234], 3)

    def test_out_of_range_precision_is_rejected_loudly(self):
        with pytest.raises(ValueError, match="precision"):
            rmi._fixed([1.0], 10)
        with pytest.raises(ValueError, match="precision"):
            rmi._fixed([1.0], -1)


class TestBackupStaysFresh:
    def test_second_repair_refreshes_the_backup_instead_of_keeping_the_original(
        self, catalog, monkeypatch
    ):
        """DeepR 2026-08-09 [ORTA]: .bak used to be written only once — a
        second repair silently left it pointing at the state before the
        FIRST repair, so 'undo the last repair' actually undid both."""
        tmp_path, day = catalog
        minute_dir = tmp_path / "data" / "bar" / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL"

        _patch_fetch(monkeypatch, day, close_value=42.0)
        rmi.repair_day("QQQ", day, tmp_path)  # first repair: 1.0 (seed) -> 42.0

        _patch_fetch(monkeypatch, day, close_value=99.0)
        rmi.repair_day("QQQ", day, tmp_path)  # second repair: 42.0 -> 99.0

        backup = next(minute_dir.glob("*.parquet.bak"))
        closes = [_decode_fixed(b.as_py()) for b in pq.read_table(backup)["close"]]
        assert all(c == pytest.approx(42.0) for c in closes), (
            ".bak should hold the state right before the LAST repair (42.0), "
            "not the original pre-first-repair seed (1.0)"
        )


class TestHalfDaySupport:
    def test_default_390_rejects_a_short_session(self, catalog, monkeypatch):
        tmp_path, day = catalog
        monkeypatch.setattr(rmi, "api_key", lambda: "test-key")
        monkeypatch.setattr(
            rmi,
            "fetch_minute_aggs",
            lambda *a, **k: (_fake_minute_rows(day, close_value=1.0)[:210], "ok"),
        )

        with pytest.raises(RuntimeError, match="only 210.*expected 390"):
            rmi.repair_day("QQQ", day, tmp_path)

    def test_explicit_expected_minutes_accepts_a_known_half_day(
        self, catalog, monkeypatch
    ):
        tmp_path, day = catalog
        monkeypatch.setattr(rmi, "api_key", lambda: "test-key")
        monkeypatch.setattr(
            rmi,
            "fetch_minute_aggs",
            lambda *a, **k: (_fake_minute_rows(day, close_value=7.0)[:210], "ok"),
        )

        result = rmi.repair_day("QQQ", day, tmp_path, expected_minutes=210)

        assert result["1-MINUTE"] == (390, 210)


# ---------------------------------------------------------------------------
# DeepR 2026-08-09 [DÜŞÜK] follow-up
# ---------------------------------------------------------------------------


class TestFindParquet:
    """next() on an empty glob raised a bare StopIteration -- no path, no
    hint about what to do. _find_parquet() turns that into an actionable
    FileNotFoundError."""

    def test_finds_the_single_parquet_file(self, tmp_path):
        bar_dir = tmp_path / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL"
        bar_dir.mkdir()
        (bar_dir / "data.parquet").write_bytes(b"stub")

        assert rmi._find_parquet(bar_dir) == bar_dir / "data.parquet"

    def test_missing_directory_raises_file_not_found_not_stop_iteration(self, tmp_path):
        bar_dir = tmp_path / "never-ingested.NASDAQ-1-MINUTE-LAST-EXTERNAL"

        with pytest.raises(FileNotFoundError, match=r"never-ingested.*ingested"):
            rmi._find_parquet(bar_dir)

    def test_empty_directory_raises_file_not_found_not_stop_iteration(self, tmp_path):
        bar_dir = tmp_path / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL"
        bar_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="ingested"):
            rmi._find_parquet(bar_dir)

    def test_repair_day_on_a_never_ingested_ticker_raises_a_clear_error(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: repair_day() itself, not just the helper in isolation."""
        day = date(2024, 3, 4)
        _patch_fetch(monkeypatch, day, close_value=42.0)

        with pytest.raises(FileNotFoundError, match="ingested"):
            rmi.repair_day("NEVERSEEN", day, tmp_path)


# ---------------------------------------------------------------------------
# DeepR 2026-08-10 [KRİTİK]: 1-DAY barında UTC/ET gün sınırı kayması
# ---------------------------------------------------------------------------
# Katalogda bar damgası KAPANIŞTIR (right-label), dolayısıyla bir seansın
# 1-DAY barı "seans + 1 gün 00:00 New York" ile damgalanır — DST'ye göre 04:00
# ya da 05:00 UTC, yani onarılan günün UTC takvim gününün DIŞINDA. Naif bir
# [gün 00:00 UTC, gün+1 00:00 UTC) filtresi bu yüzden yanlış barı seçiyordu:
# KOMŞU (bir önceki) seansın günlük barını siliyor, onarılan günün barını ise
# yerinde bırakıp üstüne bir yenisini ekleyerek İKİZLİYORDU.
#
# Bu testlerin fikstürü kasten yukarıdaki `_seed_day`'i kullanmaz: TF
# dosyalarını ingest'in kendi `resample_tf`'i ile üretir. Damgalama sözleşmesi
# taklit edilirse hata testte görünmez.

_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}

# DST'nin iki yakası: yaz (EDT, UTC-4) ve kış (EST, UTC-5). Her üçlü ardışık
# işlem günüdür (Sal-Çar-Per) — araya hafta sonu/tatil girmesin.
_DST_CASES = [
    pytest.param(date(2024, 6, 11), date(2024, 6, 12), date(2024, 6, 13), id="EDT-yaz"),
    pytest.param(date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18), id="EST-kis"),
]


def _session_minutes(day: date, *, close_value: float) -> pd.DataFrame:
    """`day` seansının 390 RTH dakikalık barı, katalogdaki right-label
    sözleşmesiyle: ilk bar 09:31, son bar 16:00 New York (yerel saatte
    kurulur, sabit bir UTC ofseti varsayılmaz — DST'nin iki yakasında da
    doğru)."""
    idx = pd.date_range(f"{day} 09:31", periods=390, freq="1min", tz=rmi.TZ)
    return pd.DataFrame(
        {
            "open": close_value,
            "high": close_value,
            "low": close_value,
            "close": close_value,
            "volume": 100.0,
        },
        index=idx,
    )


def _write_bars(path, frame: pd.DataFrame) -> None:
    path.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    # .as_unit("ns"): date_range/resample bu pandas sürümünde ns olmayan
    # çözünürlük döndürebiliyor; ts_event ns olmalı (bkz. `_seed_day`).
    frame["ts"] = frame.index.tz_convert("UTC").as_unit("ns").view("int64")
    pq.write_table(
        rmi._bar_table(frame, _SCHEMA), path / "data.parquet", compression="zstd"
    )


def _build_catalog(tmp_path, sessions: dict[date, float]):
    """Çok seanslı gerçekçi katalog: 1-MINUTE'ten türeyen her TF, ingest'in
    kullandığı `resample_tf` ile üretilir."""
    root = tmp_path / "data" / "bar"
    minutes = pd.concat(
        [_session_minutes(d, close_value=v) for d, v in sorted(sessions.items())]
    )
    _write_bars(root / "QQQ.NASDAQ-1-MINUTE-LAST-EXTERNAL", minutes)
    for tf, (_rule, spec) in rmi.TFS.items():
        agg = rmi.resample_tf(minutes, tf, _AGG).dropna(subset=["close"])
        _write_bars(root / f"QQQ.NASDAQ-{spec}-LAST-EXTERNAL", agg)
    return tmp_path


def _daily_closes_by_session(tmp_path) -> dict[date, list[float]]:
    """1-DAY dosyası → {seans tarihi: [close, ...]}.

    Damga → seans eşlemesi ingest_equities._daily_session_dates ile aynı:
    right-label damgadan 1 ns geri gitmek kovanın son anını, yani seansın
    KENDİ gününü verir (DST'den bağımsız). Liste tutuluyor ki ikizlenme
    (aynı seans için >1 bar) sessizce yutulmasın.
    """
    path = rmi._find_parquet(
        tmp_path / "data" / "bar" / "QQQ.NASDAQ-1-DAY-LAST-EXTERNAL"
    )
    table = pq.read_table(path)
    stamps = pd.to_datetime(
        table["ts_event"].to_pylist(), unit="ns", utc=True
    ).tz_convert(rmi.TZ) - pd.Timedelta(1, "ns")
    out: dict[date, list[float]] = {}
    for stamp, raw in zip(stamps, table["close"].to_pylist()):
        out.setdefault(stamp.date(), []).append(_decode_fixed(raw))
    return out


@pytest.mark.parametrize("prev_day, day, next_day", _DST_CASES)
def test_repair_keeps_neighbour_daily_bars_intact(
    prev_day, day, next_day, tmp_path, monkeypatch
):
    """Komşu seansların günlük barlarına DOKUNULMAMALI. Hatalı UTC penceresi
    bir önceki seansın 1-DAY barını siliyordu (o seans katalogdan kayboluyor,
    kapsam boşluğu olarak görünüyordu)."""
    _build_catalog(tmp_path, {prev_day: 1.0, day: 2.0, next_day: 3.0})
    _patch_fetch(monkeypatch, day, close_value=42.0)

    rmi.repair_day("QQQ", day, tmp_path)

    daily = _daily_closes_by_session(tmp_path)
    assert set(daily) == {prev_day, day, next_day}, (
        f"onarım seans kümesini değiştirdi: {sorted(daily)} — komşu günün "
        "günlük barı silinmiş olmalı"
    )
    assert daily[prev_day] == [pytest.approx(1.0)]
    assert daily[next_day] == [pytest.approx(3.0)]


@pytest.mark.parametrize("prev_day, day, next_day", _DST_CASES)
def test_repair_does_not_twin_the_repaired_daily_bar(
    prev_day, day, next_day, tmp_path, monkeypatch
):
    """Onarılan gün için TEK bir 1-DAY barı kalmalı ve o bar onarılmış
    değeri taşımalı. Hatalı pencere eski barı silmediği için onarılan gün iki
    bara çıkıyor, biri eski (bozuk) değeri taşımaya devam ediyordu."""
    _build_catalog(tmp_path, {prev_day: 1.0, day: 2.0, next_day: 3.0})
    _patch_fetch(monkeypatch, day, close_value=42.0)

    rmi.repair_day("QQQ", day, tmp_path)

    daily = _daily_closes_by_session(tmp_path)
    assert daily[day] == [pytest.approx(42.0)], (
        f"{day} için {len(daily.get(day, []))} günlük bar var "
        f"({daily.get(day)}) — tam olarak 1 tane, onarılmış değerle beklenir"
    )


@pytest.mark.parametrize(
    "day", [date(2024, 6, 12), date(2024, 1, 17)], ids=["EDT-yaz", "EST-kis"]
)
def test_day_window_matches_the_ingest_stamping_convention(day):
    """Sözleşme kilidi. Silme penceresi, ingest'in ÜRETTİĞİ 1-DAY damgasını
    içermeli; komşu seansların damgalarını dışarıda bırakmalı. İki taraf
    ıraksarsa (biri UTC takvimine, diğeri seansa göre damgalarsa) onarılan gün
    katalogdaki komşularından farklı bir sözleşmeyle yazılır — bu test tam o
    ıraksamayı yakalar."""
    start, end = rmi.session_label_bounds_ns(day)

    def daily_stamp(d: date) -> int:
        frame = _session_minutes(d, close_value=1.0)
        agg = rmi.resample_tf(frame, "D", _AGG).dropna(subset=["close"])
        return int(agg.index[0].tz_convert("UTC").as_unit("ns").value)

    assert start <= daily_stamp(day) < end
    assert daily_stamp(day - timedelta(days=1)) < start
    assert daily_stamp(day + timedelta(days=1)) >= end
