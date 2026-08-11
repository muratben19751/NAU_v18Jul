"""Klasik (NAU-parity olmayan) blok evaluator'larının davranış birim testleri.

DeepR 2026-08-11 [YÜKSEK]: `_eval_ma_cross`, `_eval_rsi_threshold`,
`_eval_price_breakout`, `_eval_momentum`, `_eval_ema_cross`,
`_eval_bollinger_break`, `_eval_macd_cross` ve `_eval_atr_stop` tüm test
ağacında yalnızca `test_composer_public_surface.py`'nin golden-name
frozenset'inde geçiyordu: yani bu fonksiyonların DAVRANIŞI değil, sadece
`composer.<ad>` üzerinden erişilebilir OLMASI test ediliyordu.

Bunlar stratejilerin sinyal üreten çekirdeği. Bir yön/karşılaştırma hatası
(ör. `hi - atr*mult` yerine `hi + atr*mult`) hiçbir testi kırmaz, yalnızca
backtest PnL'ini sessizce bozar. Burada pinlenen üç sözleşme:

  1. **Kenar tetikleme (edge trigger).** Cross tipi bloklar koşul SÜRDÜĞÜ
     sürece değil, koşula GEÇİLDİĞİ barda ateşlenir; ilk barda `prev` cari
     değere kurulduğu için asla ateşlenmez (aksi hâlde her strateji ilk barda
     geçmişe bakarak işlem açardı).
  2. **Warmup.** Yeterli geçmiş / hazır olmayan gösterge → `None`; kısmi
     veriyle hesap yapılmaz.
  3. **Durum taşıma (stateful).** `_eval_atr_stop` trailing tepe/dip'i
     `_prev_state` üzerinde bar dizisi boyunca taşır, pozisyon yönüne göre
     dallanır ve flat olunca sıfırlar. Taşıma, sıfırlanma ve yön burada
     ayrı ayrı çivilenmiştir.

Wiki References: [[strategy_and_actor]], [[webapp_module_map]]
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from block_library_classic import (
    _eval_atr_stop,
    _eval_bollinger_break,
    _eval_ema_cross,
    _eval_ma_cross,
    _eval_macd_cross,
    _eval_momentum,
    _eval_price_breakout,
    _eval_rsi_threshold,
    _lb_atr_stop,
    _lb_ma_cross,
    _lb_macd_cross,
    _lb_momentum,
    _lb_price_breakout,
    _lb_rsi_threshold,
    _validate_atr_stop,
    _validate_cross_fast_slow,
)

# ---------------------------------------------------------------------------
# Duck-type sahteleri: evaluator'lar `strategy`den yalnızca _prev_state /
# _indicators / _volumes / portfolio / _iid okur (modül docstring'i böyle
# söylüyor), bu yüzden gerçek Nautilus Strategy'e ihtiyaç yok.
# ---------------------------------------------------------------------------


class _Ind:
    """Nautilus gösterge sahtesi: `initialized` + `value`."""

    def __init__(self, value: float = 0.0, initialized: bool = True):
        self.value = value
        self.initialized = initialized


class _BB:
    """BollingerBands sahtesi: `initialized` + `upper`/`lower`."""

    def __init__(self, upper: float, lower: float, initialized: bool = True):
        self.upper = upper
        self.lower = lower
        self.initialized = initialized


class _Portfolio:
    """Pozisyon yönü sahtesi — `_eval_atr_stop`'un tek dış bağımlılığı."""

    def __init__(self, net: str = "flat"):
        self.net = net

    def is_net_long(self, _iid) -> bool:
        return self.net == "long"

    def is_net_short(self, _iid) -> bool:
        return self.net == "short"


def _strategy(net: str = "flat", **indicators) -> SimpleNamespace:
    return SimpleNamespace(
        _prev_state={},
        _indicators=dict(indicators),
        _volumes=[],
        portfolio=_Portfolio(net),
        _iid=lambda: "BTCUSDT-PERP.BYBIT",
    )


def _block(btype: str, role: str = "entry", **params) -> SimpleNamespace:
    return SimpleNamespace(type=btype, role=role, params=dict(params))


# ---------------------------------------------------------------------------
# ma_cross — hareketli ortalama kesişimi (durum: son diff)
# ---------------------------------------------------------------------------


class TestEvalMaCross:
    _FAST, _SLOW = 2, 4

    def _blk(self, role="entry", direction="up"):
        return _block(
            "ma_cross", role, fast=self._FAST, slow=self._SLOW, direction=direction
        )

    def test_warmup_returns_none_below_slow_period(self):
        """slow periyodu kadar mum birikmeden hiçbir sinyal üretilmez."""
        s = _strategy()
        assert _eval_ma_cross(s, 0, self._blk(), [10.0, 10.0, 10.0]) is None
        assert s._prev_state == {}, "warmup barında durum yazılmamalı"

    def test_first_bar_never_fires_even_when_already_crossed(self):
        """`prev = _prev_state.get(idx, diff)` garantisi.

        Blok ilk kez çalıştığında fast zaten slow'un ÜSTÜNDE olsa bile
        ateşlenmez — aksi hâlde her strateji geçmişe bakıp ilk barda işlem
        açardı (kenar değil, seviye tetiklemesi).
        """
        s = _strategy()
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 20]) is None
        assert s._prev_state[0] > 0, "diff durumu yine de kaydedilmeli"

    def test_upward_cross_returns_long(self):
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 10])  # diff = 0
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 14]) == "long"

    def test_downward_cross_returns_short_only_for_down_direction(self):
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(direction="down"), [10, 10, 10, 10])
        out = _eval_ma_cross(s, 0, self._blk(direction="down"), [10, 10, 10, 6])
        assert out == "short"

    def test_direction_filter_suppresses_the_opposite_cross(self):
        """Yön 'up' iken aşağı kesişim sinyal ÜRETMEZ (ve tersi)."""
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 10])
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 6]) is None

    def test_exit_role_returns_exit_token(self):
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(role="exit"), [10, 10, 10, 10])
        assert _eval_ma_cross(s, 0, self._blk(role="exit"), [10, 10, 10, 14]) == "exit"

    def test_no_refire_while_the_cross_persists(self):
        """Kesişim sürerken ikinci barda tekrar ateşlenmez."""
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 10])
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 14]) == "long"
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 14, 18]) is None

    def test_state_is_keyed_per_block_index(self):
        """İki ma_cross bloğu aynı stratejide birbirinin durumunu ezmez."""
        s = _strategy()
        _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 10])
        _eval_ma_cross(s, 1, self._blk(), [10, 10, 10, 20])
        assert s._prev_state[0] == 0
        assert s._prev_state[1] > 0
        # 0 numaralı blok hâlâ kendi (0) geçmişinden ateşler
        assert _eval_ma_cross(s, 0, self._blk(), [10, 10, 10, 14]) == "long"


# ---------------------------------------------------------------------------
# rsi_threshold — 0-100 ölçeği (H6) + eşik kesişimi
# ---------------------------------------------------------------------------


class TestEvalRsiThreshold:
    def test_missing_indicator_returns_none(self):
        assert _eval_rsi_threshold(_strategy(), 0, _block("rsi_threshold"), []) is None

    def test_uninitialized_indicator_returns_none(self):
        s = _strategy(rsi=_Ind(0.2, initialized=False))
        s._indicators = {0: {"rsi": _Ind(0.2, initialized=False)}}
        assert _eval_rsi_threshold(s, 0, _block("rsi_threshold"), []) is None
        assert s._prev_state == {}, "hazır olmayan gösterge durum yazmamalı"

    def test_value_is_scaled_to_0_100_before_comparison(self):
        """H6 regresyon çıpası: Nautilus RSI ∈ [0,1), eşikler 0-100 ölçeğinde.

        Ölçekleme kaldırılırsa `prev >= 30` hiçbir zaman doğru olmaz ve blok
        ÖLÜ KOD hâline gelir — tam da H6'nın düzelttiği hata.
        """
        rsi = _Ind(0.35)
        s = _strategy()
        s._indicators = {0: {"rsi": rsi}}
        blk = _block("rsi_threshold", threshold=30.0, cross="below")

        assert _eval_rsi_threshold(s, 0, blk, []) is None  # ilk bar: prev = 35
        assert s._prev_state[0] == pytest.approx(35.0)
        rsi.value = 0.25  # 25 < 30 → aşağı kesişim
        assert _eval_rsi_threshold(s, 0, blk, []) == "long"

    def test_threshold_on_the_raw_0_1_scale_never_fires(self):
        """Eşik yanlışlıkla 0-1 ölçeğinde verilirse sinyal çıkmaz.

        Ölçek uyuşmazlığının hangi yönde olduğunu çiviler: karşılaştırma
        0-100 tarafında yapılır.
        """
        rsi = _Ind(0.35)
        s = _strategy()
        s._indicators = {0: {"rsi": rsi}}
        blk = _block("rsi_threshold", threshold=0.30, cross="below")
        _eval_rsi_threshold(s, 0, blk, [])
        rsi.value = 0.25
        assert _eval_rsi_threshold(s, 0, blk, []) is None

    def test_cross_above_returns_short(self):
        rsi = _Ind(0.65)
        s = _strategy()
        s._indicators = {0: {"rsi": rsi}}
        blk = _block("rsi_threshold", threshold=70.0, cross="above")
        assert _eval_rsi_threshold(s, 0, blk, []) is None
        rsi.value = 0.75
        assert _eval_rsi_threshold(s, 0, blk, []) == "short"

    def test_exit_role_uses_the_configured_cross_direction(self):
        rsi = _Ind(0.65)
        s = _strategy()
        s._indicators = {0: {"rsi": rsi}}
        blk = _block("rsi_threshold", role="exit", threshold=70.0, cross="above")
        _eval_rsi_threshold(s, 0, blk, [])
        rsi.value = 0.75
        assert _eval_rsi_threshold(s, 0, blk, []) == "exit"

    def test_touching_the_threshold_is_not_a_cross(self):
        """Eşiğe DEĞMEK kesişim değildir (`thr > val` katı eşitsizlik)."""
        rsi = _Ind(0.35)
        s = _strategy()
        s._indicators = {0: {"rsi": rsi}}
        blk = _block("rsi_threshold", threshold=30.0, cross="below")
        _eval_rsi_threshold(s, 0, blk, [])
        rsi.value = 0.30  # tam eşik
        assert _eval_rsi_threshold(s, 0, blk, []) is None


# ---------------------------------------------------------------------------
# price_breakout — durumsuz, pencere önceki N mumdan oluşur
# ---------------------------------------------------------------------------


class TestEvalPriceBreakout:
    def test_warmup_needs_lookback_plus_one_bars(self):
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="high")
        assert _eval_price_breakout(s, 0, blk, [1, 2, 3]) is None
        assert _eval_price_breakout(s, 0, blk, [1, 2, 3, 4]) == "long"

    def test_window_excludes_the_current_bar(self):
        """Pencere `closes[-(n+1):-1]` — cari mum kendi kırılımına dahil değil.

        Dahil olsaydı `last > max(window)` asla doğru olamazdı (blok ölü kod).
        """
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="high")
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 13]) == "long"

    def test_equal_to_the_window_high_does_not_fire(self):
        """Kırılım KATI: tepeye eşitlenmek yetmez."""
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="high")
        assert _eval_price_breakout(s, 0, blk, [10, 13, 12, 13]) is None

    def test_low_direction_returns_short(self):
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="low")
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 9]) == "short"

    def test_high_breakout_is_ignored_when_direction_is_low(self):
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="low")
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 13]) is None

    def test_exit_role_returns_exit(self):
        s = _strategy()
        blk = _block("price_breakout", role="exit", lookback=3, direction="high")
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 13]) == "exit"

    def test_block_is_stateless(self):
        """Aynı girdi iki kez → aynı çıktı; `_prev_state`'e hiç dokunulmaz.

        Bu blok kasten seviye tetiklemeli (her kırılım barında ateşler);
        yanlışlıkla kenar tetiklemeye çevrilmesin diye çivilendi.
        """
        s = _strategy()
        blk = _block("price_breakout", lookback=3, direction="high")
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 13]) == "long"
        assert _eval_price_breakout(s, 0, blk, [10, 11, 12, 13]) == "long"
        assert s._prev_state == {}


# ---------------------------------------------------------------------------
# momentum — N barlık değişimin işaret değiştirmesi (durum: son değişim)
# ---------------------------------------------------------------------------


class TestEvalMomentum:
    def test_warmup_needs_lookback_plus_one_bars(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="positive")
        assert _eval_momentum(s, 0, blk, [10, 10]) is None
        assert s._prev_state == {}

    def test_first_bar_never_fires(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="positive")
        assert _eval_momentum(s, 0, blk, [10, 10, 20]) is None
        assert s._prev_state[0] == 10

    def test_sign_flip_to_positive_returns_long(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="positive")
        _eval_momentum(s, 0, blk, [10, 10, 10])  # change = 0
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 12]) == "long"

    def test_sign_flip_to_negative_returns_short(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="negative")
        _eval_momentum(s, 0, blk, [10, 10, 10])
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 8]) == "short"

    def test_opposite_sign_is_filtered_out(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="positive")
        _eval_momentum(s, 0, blk, [10, 10, 10])
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 8]) is None

    def test_exit_role_returns_exit(self):
        s = _strategy()
        blk = _block("momentum", role="exit", lookback=2, sign="positive")
        _eval_momentum(s, 0, blk, [10, 10, 10])
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 12]) == "exit"

    def test_no_refire_while_momentum_stays_positive(self):
        s = _strategy()
        blk = _block("momentum", lookback=2, sign="positive")
        _eval_momentum(s, 0, blk, [10, 10, 10])
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 12]) == "long"
        assert _eval_momentum(s, 0, blk, [10, 10, 10, 12, 14]) is None


# ---------------------------------------------------------------------------
# ema_cross / macd_cross — aynı kenar mantığı, iki ayrı fonksiyon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn,btype", [(_eval_ema_cross, "ema_cross"), (_eval_macd_cross, "macd_cross")]
)
class TestEvalIndicatorPairCross:
    """ema_cross ve macd_cross `fast.value - slow.value` üzerinden AYNI kenar
    sözleşmesini uygular; ikisi ayrı fonksiyon olduğu için biri yamanırken
    diğerinin ayrışmaması adına tek parametrize altında pinlenir."""

    def _setup(self, s, fast_v, slow_v, initialized=True):
        fast, slow = _Ind(fast_v, initialized), _Ind(slow_v, initialized)
        s._indicators = {0: {"fast": fast, "slow": slow}}
        return fast, slow

    def test_missing_indicators_return_none(self, fn, btype):
        assert fn(_strategy(), 0, _block(btype), []) is None

    def test_uninitialized_indicators_return_none(self, fn, btype):
        s = _strategy()
        self._setup(s, 12.0, 10.0, initialized=False)
        assert fn(s, 0, _block(btype), []) is None
        assert s._prev_state == {}, "warmup'ta durum yazılmamalı"

    def test_first_bar_never_fires(self, fn, btype):
        s = _strategy()
        self._setup(s, 12.0, 10.0)
        assert fn(s, 0, _block(btype, direction="up"), []) is None
        assert s._prev_state[0] == pytest.approx(2.0)

    def test_upward_cross_returns_long(self, fn, btype):
        s = _strategy()
        fast, _ = self._setup(s, 10.0, 10.0)
        blk = _block(btype, direction="up")
        assert fn(s, 0, blk, []) is None
        fast.value = 11.0
        assert fn(s, 0, blk, []) == "long"

    def test_downward_cross_returns_short(self, fn, btype):
        s = _strategy()
        fast, _ = self._setup(s, 10.0, 10.0)
        blk = _block(btype, direction="down")
        assert fn(s, 0, blk, []) is None
        fast.value = 9.0
        assert fn(s, 0, blk, []) == "short"

    def test_exit_role_returns_exit(self, fn, btype):
        s = _strategy()
        fast, _ = self._setup(s, 10.0, 10.0)
        blk = _block(btype, role="exit", direction="up")
        assert fn(s, 0, blk, []) is None
        fast.value = 11.0
        assert fn(s, 0, blk, []) == "exit"

    def test_no_refire_while_the_spread_stays_positive(self, fn, btype):
        s = _strategy()
        fast, _ = self._setup(s, 10.0, 10.0)
        blk = _block(btype, direction="up")
        assert fn(s, 0, blk, []) is None  # ilk bar
        assert fn(s, 0, blk, []) is None  # değişmeyen fark: kesişim yok
        fast.value = 11.0
        assert fn(s, 0, blk, []) == "long"
        fast.value = 12.0
        assert fn(s, 0, blk, []) is None


# ---------------------------------------------------------------------------
# bollinger_break — üç mod (legacy / breakout / revert)
# ---------------------------------------------------------------------------


class TestEvalBollingerBreak:
    def _setup(self, initialized=True):
        s = _strategy()
        s._indicators = {
            0: {"bb": _BB(upper=110.0, lower=90.0, initialized=initialized)}
        }
        return s

    def test_missing_or_uninitialized_indicator_returns_none(self):
        assert (
            _eval_bollinger_break(_strategy(), 0, _block("bollinger_break"), []) is None
        )
        s = self._setup(initialized=False)
        assert _eval_bollinger_break(s, 0, _block("bollinger_break"), [120.0]) is None

    def test_no_break_returns_none(self):
        s = self._setup()
        blk = _block("bollinger_break", side="lower")
        assert _eval_bollinger_break(s, 0, blk, [100.0]) is None

    def test_touching_the_band_counts_as_a_break(self):
        """Bantla temas kırılım sayılır (`>=` / `<=`)."""
        s = self._setup()
        assert (
            _eval_bollinger_break(s, 0, _block("bollinger_break", side="lower"), [90.0])
            == "long"
        )
        assert (
            _eval_bollinger_break(
                s, 0, _block("bollinger_break", side="upper"), [110.0]
            )
            == "long"
        )

    @pytest.mark.parametrize("side,last", [("lower", 80.0), ("upper", 120.0)])
    def test_legacy_mode_returns_long_for_both_bands(self, side, last):
        """Geriye dönük uyum: mod verilmezse HER İKİ bant da 'long' üretir.

        Katalogdaki eski spec'ler bu davranışa göre kaydedildi; L14 yeni
        modları eklerken varsayılanı kasten değiştirmedi.
        """
        s = self._setup()
        blk = _block("bollinger_break", side=side)
        assert _eval_bollinger_break(s, 0, blk, [last]) == "long"

    @pytest.mark.parametrize(
        "mode,side,last,expected",
        [
            ("breakout", "upper", 120.0, "long"),
            ("breakout", "lower", 80.0, "short"),
            ("revert", "upper", 120.0, "short"),
            ("revert", "lower", 80.0, "long"),
        ],
    )
    def test_breakout_and_revert_modes_are_mirror_images(
        self, mode, side, last, expected
    ):
        s = self._setup()
        blk = _block("bollinger_break", side=side, mode=mode)
        assert _eval_bollinger_break(s, 0, blk, [last]) == expected

    def test_exit_role_ignores_mode(self):
        """Çıkış rolünde mod önemsiz: kırılım varsa 'exit'."""
        s = self._setup()
        for mode in ("legacy", "breakout", "revert"):
            blk = _block("bollinger_break", role="exit", side="upper", mode=mode)
            assert _eval_bollinger_break(s, 0, blk, [120.0]) == "exit"
        blk = _block("bollinger_break", role="exit", side="upper", mode="revert")
        assert _eval_bollinger_break(s, 0, blk, [100.0]) is None

    def test_empty_closes_uses_zero_and_breaks_the_lower_band(self):
        """`closes` boşken son fiyat 0.0 varsayılır → alt bant kırılır.

        Sürpriz ama GERÇEK davranış; kasten mi böyle kaldığı belli olsun diye
        çivilendi (üst bant tarafında sinyal üretmez).
        """
        s = self._setup()
        assert (
            _eval_bollinger_break(s, 0, _block("bollinger_break", side="lower"), [])
            == "long"
        )
        assert (
            _eval_bollinger_break(s, 0, _block("bollinger_break", side="upper"), [])
            is None
        )


# ---------------------------------------------------------------------------
# atr_stop — TEK stateful blok: trailing tepe/dip `_prev_state`te taşınır
# ---------------------------------------------------------------------------


class TestEvalAtrStopGuards:
    def _long_strategy(self, atr_value=1.0, initialized=True):
        s = _strategy(net="long")
        s._indicators = {0: {"atr": _Ind(atr_value, initialized)}}
        return s

    def test_entry_role_is_refused_outright(self):
        """atr_stop yalnızca çıkış bloğudur; giriş rolünde hiçbir şey yapmaz."""
        s = self._long_strategy()
        blk = _block("atr_stop", role="entry", mult=2.0)
        assert _eval_atr_stop(s, 0, blk, [100.0, 50.0]) is None
        assert s._prev_state == {}, "giriş rolünde durum bile yazılmamalı"

    def test_missing_atr_returns_none(self):
        s = _strategy(net="long")
        assert _eval_atr_stop(s, 0, _block("atr_stop", role="exit"), [100.0]) is None

    def test_warmup_does_not_track_the_trailing_extreme(self):
        """ATR hazır değilken hiçbir tepe kaydedilmez.

        Bu, sıfırlamanın ikinci yüzü: warmup boyunca görülen fiyatlar trailing
        tepeye SAYILMAZ, ilk geçerli bar tepeyi kendi kapanışından kurar.
        Aksi hâlde warmup'taki bir fiyat sıçraması, pozisyon açılır açılmaz
        stop tetiklerdi.
        """
        s = self._long_strategy(initialized=False)
        blk = _block("atr_stop", role="exit", mult=2.0)
        assert _eval_atr_stop(s, 0, blk, [500.0]) is None
        assert s._prev_state == {}

        s._indicators[0]["atr"].initialized = True
        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert s._prev_state["atr_hi_0"] == 100.0

    def test_empty_closes_returns_none(self):
        s = self._long_strategy()
        assert _eval_atr_stop(s, 0, _block("atr_stop", role="exit"), []) is None

    def test_flat_position_resets_both_extremes_to_the_last_close(self):
        s = _strategy(net="flat")
        s._indicators = {0: {"atr": _Ind(1.0)}}
        blk = _block("atr_stop", role="exit", mult=2.0)
        s._prev_state["atr_hi_0"] = 999.0
        s._prev_state["atr_lo_0"] = 1.0
        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert s._prev_state["atr_hi_0"] == 100.0
        assert s._prev_state["atr_lo_0"] == 100.0


class TestEvalAtrStopLongTrailing:
    """Uzun pozisyon: tepe (`atr_hi_i`) taşınır, `last <= hi - atr*mult` çıkar."""

    def _walk(self, closes, atr_value=1.0, mult=2.0, state=None, net="long"):
        s = _strategy(net=net)
        s._indicators = {0: {"atr": _Ind(atr_value)}}
        if state:
            s._prev_state.update(state)
        blk = _block("atr_stop", role="exit", mult=mult)
        results = [_eval_atr_stop(s, 0, blk, [c]) for c in closes]
        return s, results

    def test_the_peak_is_carried_across_bars(self):
        """Çıkış, GEÇMİŞ tepeye göre ölçülür — cari bara göre değil.

        105'e çıkıp 102'ye dönen fiyat, tepe taşınmasaydı (hi=102) 100'ün
        altına inmedikçe çıkmazdı; taşındığı için 103 eşiğinde çıkar.
        """
        s, out = self._walk([100.0, 105.0, 104.0, 102.0])
        assert out == [None, None, None, "exit"]
        assert s._prev_state["atr_hi_0"] == 105.0

    def test_a_monotonically_rising_price_never_stops_out(self):
        """Yön çıpası: stop tepenin ALTINDA (`hi - atr*mult`).

        İşaret ters çevrilseydi (`hi + atr*mult`) yükselen her fiyat anında
        çıkış üretir, trend stratejileri hiç kazanamazdı.
        """
        _, out = self._walk([100.0, 101.0, 110.0, 130.0, 200.0])
        assert out == [None, None, None, None, None]

    def test_the_stop_distance_scales_with_atr_and_mult(self):
        """atr*mult tam olarak eşiği belirler: 100 tepede, atr=2, mult=3 → 94."""
        _, out = self._walk([100.0, 94.5], atr_value=2.0, mult=3.0)
        assert out == [None, None]
        _, out = self._walk([100.0, 94.0], atr_value=2.0, mult=3.0)
        assert out == [None, "exit"]

    def test_default_multiplier_is_three(self):
        s = _strategy(net="long")
        s._indicators = {0: {"atr": _Ind(1.0)}}
        blk = _block("atr_stop", role="exit")  # mult verilmedi
        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert _eval_atr_stop(s, 0, blk, [97.5]) is None  # 3.0 → eşik 97.0
        assert _eval_atr_stop(s, 0, blk, [97.0]) == "exit"

    def test_going_flat_resets_the_peak_for_the_next_position(self):
        """SIFIRLAMA NOKTASI: yeni pozisyon eski tepeyi miras almaz.

        Bu testin koruduğu hata sınıfı: kapanan bir işlemin 105'lik tepesi
        kalırsa, 101'den açılan YENİ uzun pozisyon daha ilk barında stop olur.
        """
        s = _strategy(net="long")
        s._indicators = {0: {"atr": _Ind(1.0)}}
        blk = _block("atr_stop", role="exit", mult=2.0)

        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert _eval_atr_stop(s, 0, blk, [105.0]) is None
        assert _eval_atr_stop(s, 0, blk, [102.0]) == "exit"

        s.portfolio.net = "flat"  # pozisyon kapandı
        assert _eval_atr_stop(s, 0, blk, [102.0]) is None
        assert s._prev_state["atr_hi_0"] == 102.0

        s.portfolio.net = "long"  # yeni pozisyon
        assert _eval_atr_stop(s, 0, blk, [101.0]) is None, (
            "yeni pozisyon önceki işlemin tepesini miras aldı"
        )

    def test_state_is_keyed_per_block_index(self):
        """İki atr_stop bloğu ayrı anahtarlarda (`atr_hi_0` / `atr_hi_1`) izlenir."""
        s = _strategy(net="long")
        s._indicators = {0: {"atr": _Ind(1.0)}, 1: {"atr": _Ind(1.0)}}
        blk = _block("atr_stop", role="exit", mult=2.0)
        _eval_atr_stop(s, 0, blk, [100.0])
        _eval_atr_stop(s, 1, blk, [200.0])
        assert s._prev_state["atr_hi_0"] == 100.0
        assert s._prev_state["atr_hi_1"] == 200.0


class TestEvalAtrStopShortTrailing:
    """Kısa pozisyon: dip (`atr_lo_i`) taşınır, `last >= lo + atr*mult` çıkar."""

    def _short(self):
        s = _strategy(net="short")
        s._indicators = {0: {"atr": _Ind(1.0)}}
        return s, _block("atr_stop", role="exit", mult=2.0)

    def test_the_trough_is_carried_across_bars(self):
        s, blk = self._short()
        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert _eval_atr_stop(s, 0, blk, [95.0]) is None
        assert s._prev_state["atr_lo_0"] == 95.0
        assert _eval_atr_stop(s, 0, blk, [96.0]) is None  # eşik 97.0
        assert _eval_atr_stop(s, 0, blk, [97.0]) == "exit"

    def test_a_monotonically_falling_price_never_stops_out(self):
        """Yön çıpası (kısa taraf): stop dibin ÜSTÜNDE (`lo + atr*mult`)."""
        s, blk = self._short()
        for close in (100.0, 99.0, 90.0, 70.0, 10.0):
            assert _eval_atr_stop(s, 0, blk, [close]) is None

    def test_long_and_short_extremes_do_not_cross_talk(self):
        """Uzun tarafın tepesi kısa tarafın dibini kirletmez (ayrı anahtarlar)."""
        s, blk = self._short()
        s._prev_state["atr_hi_0"] = 1.0  # uzun taraftan kalma saçma değer
        assert _eval_atr_stop(s, 0, blk, [100.0]) is None
        assert s._prev_state["atr_lo_0"] == 100.0
        assert s._prev_state["atr_hi_0"] == 1.0, "kısa dal tepe anahtarına yazdı"


# ---------------------------------------------------------------------------
# max_lookback + validate — warmup bütçesi ve parametre tutarlılığı
# ---------------------------------------------------------------------------


class TestLookbackBudget:
    """`max_lookback` stratejinin kaç bar warmup bekleyeceğini belirler.

    Eksik hesaplanırsa evaluator'lar kısmi pencereyle çalışır (yukarıdaki
    warmup testleri o durumda `None` döndüğü için sinyaller sessizce kaybolur).
    """

    def test_defaults_match_the_evaluators_own_requirements(self):
        assert _lb_ma_cross({}) == 30
        assert _lb_macd_cross({}) == 26
        assert _lb_price_breakout({}) == 20
        # +1: evaluator `closes[-n-1]`'e, yani N+1'inci bara bakıyor
        assert _lb_momentum({}) == 11
        assert _lb_rsi_threshold({}) == 15
        assert _lb_atr_stop({}) == 15

    def test_explicit_params_win(self):
        assert _lb_ma_cross({"slow": 100}) == 100
        assert _lb_momentum({"lookback": 5}) == 6
        assert _lb_atr_stop({"period": 21}) == 22


class TestBuiltinValidators:
    def test_slow_must_exceed_fast(self):
        assert _validate_cross_fast_slow(_block("ma_cross", fast=10, slow=30)) is None
        err = _validate_cross_fast_slow(_block("ma_cross", fast=30, slow=10))
        assert err and "slow must be > fast" in err

    def test_equal_periods_are_rejected(self):
        """fast == slow → diff sabit 0, blok hiç ateşlenmez; erken reddedilir."""
        assert (
            _validate_cross_fast_slow(_block("ma_cross", fast=10, slow=10)) is not None
        )

    def test_atr_stop_is_exit_only(self):
        assert _validate_atr_stop(_block("atr_stop", role="exit")) is None
        assert _validate_atr_stop(_block("atr_stop", role="entry")) is not None


# ---------------------------------------------------------------------------
# Kayıt defteri bağlantısı — bu testler ÜRETİMDE koşan kodu koruyor mu?
# ---------------------------------------------------------------------------


def test_block_registry_wires_these_exact_functions():
    """BLOCK_REGISTRY girdileri yukarıda test edilen fonksiyonların TA KENDİSİ.

    Bu olmadan testler bir kopyayı/ölü kodu koruyor olabilirdi: bir gün
    composer kendi kopyasını kullanmaya başlarsa bu assert kırılır.
    """
    from composer import BLOCK_REGISTRY

    expected = {
        "ma_cross": _eval_ma_cross,
        "rsi_threshold": _eval_rsi_threshold,
        "price_breakout": _eval_price_breakout,
        "momentum": _eval_momentum,
        "ema_cross": _eval_ema_cross,
        "bollinger_break": _eval_bollinger_break,
        "macd_cross": _eval_macd_cross,
        "atr_stop": _eval_atr_stop,
    }
    for name, fn in expected.items():
        assert BLOCK_REGISTRY[name]["eval"] is fn, f"{name} başka bir eval'e bağlı"
