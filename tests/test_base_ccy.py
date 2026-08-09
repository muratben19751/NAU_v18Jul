"""data._base_ccy (DeepR 2026-08-09 [DÜŞÜK]).

_base_ccy's only prior "coverage" was tests/test_tf_sweep.py stubbing it
out entirely via monkeypatch -- the real suffix-stripping logic was never
actually exercised. _instrument_meta's bybit branch also hand-rolled the
identical loop inline instead of calling this; that duplication is now
removed (see data.py) and covered here via a real (non-mocked) call.

Wiki References
---------------
See: [[nau_deepr_ucuncu_tur_2026_08_09]]
"""

from __future__ import annotations

import data


class TestBaseCcy:
    def test_strips_recognized_quote_suffixes(self):
        assert data._base_ccy("BTCUSDT") == "BTC"
        assert data._base_ccy("ETHUSDC") == "ETH"
        assert data._base_ccy("SOLUSD") == "SOL"

    def test_preserves_the_original_casing_of_the_base(self):
        # The suffix MATCH is case-insensitive (`s = symbol.upper()`), but
        # the returned slice comes from the original `symbol`, not the
        # upper-cased copy -- a naive rewrite could easily force-uppercase
        # the result instead.
        assert data._base_ccy("ethusdt") == "eth"

    def test_falls_back_to_the_first_three_characters_when_no_suffix_matches(self):
        assert data._base_ccy("APTEUR") == "APT"

    def test_empty_result_after_stripping_falls_back_to_the_default(self):
        # "USDT"[:-4] == "" -- falsy, so the default kicks in rather than
        # returning an empty base currency.
        assert data._base_ccy("USDT") == "BTC"
        assert data._base_ccy("USDT", default="XXX") == "XXX"

    def test_empty_symbol_returns_the_default(self):
        assert data._base_ccy("") == "BTC"


class TestInstrumentMetaDelegatesToBaseCcy:
    """_instrument_meta's bybit branch used to hand-roll the same
    suffix-stripping loop as _base_ccy inline; it now calls _base_ccy
    directly (data.py). Exercised for real, not mocked -- _make_bybit_instrument
    is a plain Nautilus factory call, no I/O."""

    def test_eth_pair_gets_eth_as_base_currency(self):
        meta = data._instrument_meta("bybit", symbol="ETHUSDT")
        assert meta["base_currency"] == "ETH"

    def test_btc_pair_gets_btc_as_base_currency(self):
        meta = data._instrument_meta("bybit", symbol="BTCUSDT")
        assert meta["base_currency"] == "BTC"

    def test_inline_suffix_loop_is_gone_from_instrument_meta(self):
        import inspect

        src = inspect.getsource(data._instrument_meta)
        assert "_base_ccy(symbol)" in src
        assert 'for suffix in ("USDT", "USDC", "USD")' not in src
