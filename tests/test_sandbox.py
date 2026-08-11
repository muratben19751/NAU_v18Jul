"""Phase 3a — custom-code sandbox: load-time re-validation + killable subprocess."""

import time

import pytest

import sandbox
from composer import ComposedStrategySpec, SignalBlock


def _spec(block_type, params=None):
    return ComposedStrategySpec(
        id="t",
        name="t",
        description="",
        blocks=[SignalBlock(type=block_type, role="entry", params=params or {})],
    )


# ---------------------------------------------------------------------------
# Load-time AST re-validation (composer._load_module_from_path)
# ---------------------------------------------------------------------------


class TestLoadTimeRevalidation:
    def test_rejects_tampered_on_disk_code(self, tmp_path):
        from composer import _load_module_from_path

        bad = tmp_path / "evil.py"
        bad.write_text(
            "import os\n"
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    return os.getcwd()\n",
            encoding="utf-8",
        )
        with pytest.raises(ImportError):
            _load_module_from_path("evil", bad)

    def test_accepts_clean_block(self, tmp_path):
        from composer import _load_module_from_path

        good = tmp_path / "good.py"
        good.write_text(
            "def evaluate(state, block, closes, indicators, portfolio):\n"
            "    return 'long' if closes[-1] > closes[0] else None\n",
            encoding="utf-8",
        )
        mod = _load_module_from_path("good_block", good)
        assert callable(mod.evaluate)


# ---------------------------------------------------------------------------
# Custom-block detection (routing between fast path and sandbox)
# ---------------------------------------------------------------------------


class TestHasCustomBlock:
    def test_builtin_only_is_false(self):
        assert sandbox.has_custom_block(_spec("ma_cross")) is False

    def test_unknown_block_treated_as_custom(self):
        # Unknown type → not builtin → must be sandboxed (fail safe).
        assert sandbox.has_custom_block(_spec("nonexistent_block_xyz")) is True


class TestRecipeHelpers:
    def test_derive_base(self):
        assert sandbox._derive_base("BTCUSDT") == "BTC"
        assert sandbox._derive_base("ETHUSDT") == "ETH"

    def test_build_instrument_bar_type(self):
        inst, bt = sandbox._build_instrument_bar_type(
            {"symbol": "BTCUSDT", "category": "linear", "interval": "1"}
        )
        assert str(inst.id.venue) == "BYBIT_LINEAR"
        assert str(bt).startswith("BTCUSDT.BYBIT_LINEAR-1-MINUTE")


@pytest.fixture
def fake_external_catalog(tmp_path, monkeypatch):
    """Repo-içi sahte dış katalog: QQQ.NASDAQ Equity + 1-DAY bar dizini.

    DeepR 2026-08-11 [YÜKSEK]: bu sınıf eskiden
    `skipif(not _external_catalog_available())` ile korunuyordu; makinede
    (ve temiz bir CI runner'ında) QQQ.NASDAQ dış kataloğu yoksa Equity
    enstrüman kurulumu, `size_precision == 0` ve
    `QQQ.NASDAQ-1-DAY-LAST-EXTERNAL` bar-type üretimi hiç sınanmıyordu — ve
    skip sessiz olduğu için kimse fark etmiyordu. Ortam bağımlılığı burada
    tamamen kaldırıldı: katalog testin kendisi tarafından kuruluyor, gerçek
    NAU kataloğuna hiç dokunulmuyor.
    """
    from nautilus_trader.model import Currency, InstrumentId, Price, Quantity, Symbol
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    import data

    root = tmp_path / "external_catalog"
    root.mkdir()
    # Bar dizini yalnızca ADIYLA konuşuyor (`_external_bar_dir` isim ayrıştırır);
    # parquet içeriği bu testlerin konusu değil.
    (root / "data" / "bar" / "QQQ.NASDAQ-1-DAY-LAST-EXTERNAL").mkdir(parents=True)

    inst = Equity(
        instrument_id=InstrumentId.from_str("QQQ.NASDAQ"),
        raw_symbol=Symbol("QQQ"),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )
    ParquetDataCatalog(str(root)).write_data([inst])
    monkeypatch.setattr(data, "EXTERNAL_CATALOGS", [root])
    return root


@pytest.mark.usefixtures("fake_external_catalog")
class TestExternalRecipe:
    def test_build_external_instrument_bar_type(self):
        inst, bt = sandbox._build_instrument_bar_type(
            {
                "source": "external",
                "instrument_id": "QQQ.NASDAQ",
                "granularity": "1-DAY",
            }
        )
        assert type(inst).__name__ == "Equity"
        assert int(inst.size_precision) == 0  # integer shares
        assert str(bt) == "QQQ.NASDAQ-1-DAY-LAST-EXTERNAL"

    def test_unknown_external_instrument_raises(self):
        with pytest.raises(RuntimeError, match="not found in catalog"):
            sandbox._build_instrument_bar_type(
                {
                    "source": "external",
                    "instrument_id": "NOPE.NOWHERE",
                    "granularity": "1-DAY",
                }
            )


# ---------------------------------------------------------------------------
# Fast path stays in-process (no subprocess for builtin specs)
# ---------------------------------------------------------------------------


class TestFastPath:
    def test_builtin_runs_in_process(self, monkeypatch):
        import backtest

        sentinel = object()
        calls = {"n": 0}

        def _fake(spec, bars, **kw):
            calls["n"] += 1
            return sentinel

        monkeypatch.setattr(backtest, "run_composed_backtest", _fake)
        out = sandbox.run_backtest_guarded(
            _spec("ma_cross"),
            None,
            {"symbol": "BTCUSDT", "category": "linear", "interval": "1"},
        )
        assert out is sentinel
        assert calls["n"] == 1  # called directly, not in a child


# ---------------------------------------------------------------------------
# Killable subprocess — the security guarantee
# ---------------------------------------------------------------------------


class TestKillMachinery:
    def test_hang_is_terminated_and_parent_survives(self):
        t0 = time.time()
        result, err = sandbox._run_in_child(
            sandbox._hang_target, ("payload",), None, timeout_s=2.0
        )
        elapsed = time.time() - t0
        assert result is None
        assert err is not None and "timed out" in err
        # Must actually kill near the deadline, not hang indefinitely.
        assert elapsed < 10.0
