"""Regression tests for the bug fixes applied in the code review."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# data.py fixes
# ---------------------------------------------------------------------------


class TestRefreshRowIndexNoneGuard:
    """refresh_row('index') must not pass None to load_index_bars."""

    def test_no_start_end_defaults_to_last_year(self):
        from data import refresh_row

        calls = []

        def fake_load(ticker, start, end, granularity="1d", force=False):
            calls.append((start, end))
            return  # no-op

        with (
            patch("data.load_index_bars", fake_load),
            patch(
                "data._index_rows",
                return_value=[
                    {
                        "key": "I:SPX",
                        "source": "index",
                        "instrument_id": "I:SPX.POLYGON",
                        "bar_types": [],
                    }
                ],
            ),
        ):
            try:
                refresh_row("index", ticker="I:SPX", granularity="1d")
            except RuntimeError:
                pass  # "row not built" is fine — we only care that load was called

        assert calls, "load_index_bars was never called"
        start_arg, end_arg = calls[0]
        assert start_arg is not None, "start must not be None"
        assert end_arg is not None, "end must not be None"
        assert isinstance(start_arg, date)
        assert isinstance(end_arg, date)
        assert end_arg >= start_arg


class TestTickerToFilenameReuse:
    """Inline replace chains must equal _ticker_to_filename."""

    def test_colon_replaced(self):
        from data import _ticker_to_filename

        assert _ticker_to_filename("I:SPX") == "I_SPX"

    def test_slash_replaced(self):
        from data import _ticker_to_filename

        assert _ticker_to_filename("BTC/USD") == "BTC_USD"

    def test_both_replaced(self):
        from data import _ticker_to_filename

        assert _ticker_to_filename("I:BTC/USD") == "I_BTC_USD"


class TestAutoWriteBaseCurrency:
    """_auto_write_bybit_catalog must derive base from symbol, not default to BTC."""

    def _run(self, symbol):
        import pandas as pd

        from data import _auto_write_bybit_catalog

        captured = {}

        # price_hint: DeepR 2026-08-11 [YÜKSEK] — the catalog writer now derives
        # the price precision from the frame it is about to write (sub-cent
        # symbols must not land in the catalog as all-zero bars). The fake has
        # to accept it, and capturing it keeps that wiring under test here too.
        def fake_make_instrument(
            symbol="BTCUSDT", base="BTC", quote=None, category="linear", **kw
        ):
            captured["base"] = base
            captured["category"] = category
            captured["price_hint"] = kw.get("price_hint")
            m = MagicMock()
            m.id = MagicMock()
            return m

        df = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        )
        cat_mock = MagicMock()
        cat_mock.delete_data_range = MagicMock(side_effect=Exception("no data"))

        # Functions are imported inside _auto_write_bybit_catalog via
        # "from backtest import ..." so we patch on the backtest module.
        with (
            patch("backtest._make_bybit_instrument", fake_make_instrument),
            patch("backtest._make_bybit_bar_type", MagicMock(return_value=MagicMock())),
            patch("backtest._bars_from_df", MagicMock(return_value=[])),
            patch("data.get_nautilus_catalog", MagicMock(return_value=cat_mock)),
        ):
            _auto_write_bybit_catalog(symbol, "1", df)

        return captured

    def test_eth_base_derived(self):
        captured = self._run("ETHUSDT")
        assert captured.get("base") == "ETH", (
            f"expected ETH, got {captured.get('base')}"
        )

    def test_sol_base_derived(self):
        captured = self._run("SOLUSDT")
        assert captured.get("base") == "SOL"

    def test_btc_base_still_works(self):
        captured = self._run("BTCUSDT")
        assert captured.get("base") == "BTC"

    def test_price_hint_is_derived_from_the_frame(self):
        """Katalog yazıcısı fiyat ölçeğini veriden alıyor mu (cent altı tuzağı)."""
        captured = self._run("ETHUSDT")
        assert captured.get("price_hint") == 1.0  # _run'ın çerçevesindeki tek fiyat


# ---------------------------------------------------------------------------
# web/routes/data.py fixes
# ---------------------------------------------------------------------------


class TestDiscoverIndexTickersKwarg:
    """The route must call discover_index_tickers with force= not force_refresh=."""

    def test_force_kwarg_name(self):
        # Directly check the source doesn't contain the bad kwarg
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1] / "web" / "routes" / "data.py"
        ).read_text(encoding="utf-8")
        assert "force_refresh=" not in src, (
            "force_refresh= kwarg still present in web/routes/data.py"
        )
        assert "force=force" in src or "force=" in src


# ---------------------------------------------------------------------------
# composer.py fixes
# ---------------------------------------------------------------------------


class TestCurrentEquity:
    """Drives the ACTUAL _current_equity method — previously the logic was
    hand-copied and the shipped composer.ComposedStrategy._current_equity was
    never called (false confidence in a money-critical method; suite analysis
    2026-07 quick-win #6)."""

    def _strat(self, portfolio, equity_mode=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            _iid_obj=SimpleNamespace(venue="BYBIT"),
            _equity_mode=equity_mode,
            portfolio=portfolio,
            log=MagicMock(),
        )

    def _call(self, strat):
        from composer import ComposedStrategy

        return ComposedStrategy._current_equity(strat)

    def test_usdt_account_returns_actual_balance(self):
        from nautilus_trader.model import Currency

        bal_mock = MagicMock()
        bal_mock.total.as_double.return_value = 500_000.0

        account_mock = MagicMock()
        account_mock.balances.return_value = {Currency.from_str("USDT"): bal_mock}

        portfolio_mock = MagicMock()
        portfolio_mock.equity.return_value = None  # no v2 native path → scan balances
        portfolio_mock.account.return_value = account_mock

        assert self._call(self._strat(portfolio_mock)) == 500_000.0

    def test_no_account_returns_fallback(self):
        from app_constants import STARTING_CASH

        portfolio_mock = MagicMock()
        portfolio_mock.equity.return_value = None
        portfolio_mock.account.return_value = None

        assert self._call(self._strat(portfolio_mock)) == float(STARTING_CASH)

    def test_fallback_to_constant_is_logged_and_cached(self):
        """DeepR 2026-08-08 [YÜKSEK]: the constant fallback was silent (no log)
        and never cached into _equity_mode, so every remaining candle of a
        possibly years-long run re-attempted both failing paths. Now it must
        warn once and set _equity_mode so later calls short-circuit."""
        from app_constants import STARTING_CASH

        portfolio_mock = MagicMock()
        portfolio_mock.equity.return_value = None
        portfolio_mock.account.return_value = None
        strat = self._strat(portfolio_mock)

        result = self._call(strat)

        assert result == float(STARTING_CASH)
        assert strat._equity_mode == "constant"
        assert strat._equity_constant_value == float(STARTING_CASH)
        assert strat.log.warning.called

    def test_cached_constant_mode_skips_the_failing_paths_entirely(self):
        """Once _equity_mode=='constant', the method must not touch
        portfolio.equity()/account() again — it returns the cached value."""
        portfolio_mock = MagicMock()
        strat = self._strat(portfolio_mock, equity_mode="constant")
        strat._equity_constant_value = 12_345.0

        result = self._call(strat)

        assert result == 12_345.0
        portfolio_mock.equity.assert_not_called()
        portfolio_mock.account.assert_not_called()

    def test_portfolio_equity_exception_is_logged_not_swallowed(self):
        """The portfolio.equity() except used to be a bare `pass` — a broken
        v2 native path failed silently on every candle."""
        portfolio_mock = MagicMock()
        portfolio_mock.equity.side_effect = RuntimeError("boom")
        portfolio_mock.account.return_value = None
        strat = self._strat(portfolio_mock)

        self._call(strat)

        assert strat.log.warning.called
        assert "portfolio.equity()" in strat.log.warning.call_args_list[0].args[0]


class TestOnBarBlockPartition:
    """_entry_blocks and _exit_blocks must be pre-partitioned from spec.blocks."""

    def test_blocks_partitioned_correctly(self):
        from composer import ComposedStrategySpec, SignalBlock

        spec = ComposedStrategySpec(
            id="test",
            name="test",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross", role="entry", params={"fast": 5, "slow": 10}
                ),
                SignalBlock(
                    type="ma_cross", role="entry", params={"fast": 3, "slow": 8}
                ),
                SignalBlock(
                    type="ma_cross", role="exit", params={"fast": 5, "slow": 10}
                ),
            ],
        )
        entry_blocks = [(i, b) for i, b in enumerate(spec.blocks) if b.role == "entry"]
        exit_blocks = [(i, b) for i, b in enumerate(spec.blocks) if b.role == "exit"]

        assert len(entry_blocks) == 2
        assert len(exit_blocks) == 1
        assert all(b.role == "entry" for _, b in entry_blocks)
        assert all(b.role == "exit" for _, b in exit_blocks)


# ---------------------------------------------------------------------------
# backtest.py fixes
# ---------------------------------------------------------------------------


class TestMakeBybitBarTypeIntervals:
    """_make_bybit_bar_type must support all Bybit intervals."""

    def test_all_intervals(self):
        from nautilus_trader.model import InstrumentId

        from backtest import _make_bybit_bar_type

        iid = InstrumentId.from_str("BTCUSDT.BYBIT")
        for interval in ("1", "5", "15", "30", "60", "240", "720", "D"):
            bt = _make_bybit_bar_type(iid, interval)
            assert bt is not None

    def test_unsupported_raises(self):
        from nautilus_trader.model import InstrumentId

        from backtest import _make_bybit_bar_type

        iid = InstrumentId.from_str("BTCUSDT.BYBIT")
        # "45" is deliberately absent: Bybit's kline API has no 45m interval.
        with pytest.raises(ValueError):
            _make_bybit_bar_type(iid, "45")


class TestComposedStrategySpecValidate:
    """ComposedStrategySpec.validate() must catch missing entry block."""

    def test_no_entry_block_is_invalid(self):
        from composer import ComposedStrategySpec, SignalBlock

        spec = ComposedStrategySpec(
            id="x",
            name="x",
            description="",
            blocks=[SignalBlock(type="ma_cross", role="exit", params={})],
        )
        err = spec.validate()
        assert err is not None
        assert "entry" in err.lower()

    def test_valid_spec(self):
        from composer import ComposedStrategySpec, SignalBlock

        spec = ComposedStrategySpec(
            id="x",
            name="x",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross", role="entry", params={"fast": 5, "slow": 10}
                )
            ],
        )
        assert spec.validate() is None

    def _entry_only(self, **kwargs):
        from composer import ComposedStrategySpec, SignalBlock

        return ComposedStrategySpec(
            id="x",
            name="x",
            description="",
            blocks=[
                SignalBlock(
                    type="ma_cross", role="entry", params={"fast": 5, "slow": 10}
                )
            ],
            **kwargs,
        )

    # DeepR 2026-08-09 [ORTA]: atr_period<=0 used to reach Nautilus's
    # AverageTrueRange(period) unvalidated, raising only once a live
    # backtest actually needed it. Required only when ATR is actually
    # consulted (sl_type/tp_type == "atr", or ATR-target sizing) — a
    # percent-SL spec with atr_period=0 is unrelated and must stay valid.
    def test_non_positive_atr_period_is_invalid_when_sl_type_is_atr(self):
        err = self._entry_only(sl_type="atr", atr_period=0).validate()
        assert err is not None
        assert "atr_period" in err

    def test_non_positive_atr_period_is_invalid_when_tp_type_is_atr(self):
        err = self._entry_only(tp_type="atr", atr_period=-1).validate()
        assert err is not None
        assert "atr_period" in err

    def test_non_positive_atr_period_is_invalid_for_atr_target_sizing(self):
        err = self._entry_only(trade_size_mode="atr_target", atr_period=0).validate()
        assert err is not None
        assert "atr_period" in err

    def test_non_positive_atr_period_is_fine_when_atr_is_not_used(self):
        err = self._entry_only(sl_type="percent", atr_period=0).validate()
        assert err is None

    def test_positive_atr_period_is_valid_when_sl_type_is_atr(self):
        err = self._entry_only(sl_type="atr", atr_period=14).validate()
        assert err is None
