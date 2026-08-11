"""`ComposedStrategySpec.validate()` boyutlandırma dalları + `build_spec` clamp'leri.

DeepR 2026-08-11 [YÜKSEK]: `composer_spec.py:150-330` gerçek para büyüklüğünü
belirleyen kod. Test edilen kısmı yalnızca isim/entry-blok/atr_period dalları
(`test_fixes.py`) ve custom-blok rol çelişkisi (`test_auto_360_fixes.py`) idi.
Test EDİLMEYEN ve burada çivilenen davranışlar:

  * `percent_equity` → sonlu-sayı kontrolü, `pct <= 0`, `pct > 100`
    (kaldıraçsız üst sınır);
  * `atr_target` → `trade_size_atr_risk <= 0`;
  * `fixed_usdt` → `trade_size_usdt <= 0`;
  * `vol_target` → `trade_size_vol_target <= 0`, `trade_size_vol_span < 2`,
    `trade_size_capital <= 0`;
  * bracket dalları (`sl_value <= 0`, TP açıkken `tp_value <= 0`);
  * `build_spec`'in whitelist/clamp'leri — özellikle `vol_target`'ın
    trade_size_mode whitelist'inde KALMASI (docstring'in kendisi eski
    strategy.py clamp'inin vol_target'ı sessizce "fixed"e düşürdüğünü anlatıyor);
  * `_as_bool` — HTML form ""/"0"/"false"/"off" dönüşümü. Buradaki bir
    regresyon işaretlenmemiş checkbox'ları `use_bracket=True`/`allow_short=True`
    yapar, yani kullanıcının istemediği emirleri gönderir.

Clamp'ler sessizce yanlış çalışırsa HER backtest yanlış ölçekte koşar; bu yüzden
her dal için hem reddetme hem de SINIR DEĞERDE kabul ayrı ayrı test edilir.

Wiki References: [[webapp_module_map]], [[vol_targeted_trend]]
"""

from __future__ import annotations

import math

import pytest

from composer_spec import (
    ComposedStrategySpec,
    SignalBlock,
    _as_bool,
    build_spec,
    new_spec_id,
)


def _spec(**kwargs) -> ComposedStrategySpec:
    """Geçerli bir taban spec — her test yalnızca ilgilendiği alanı bozar."""
    return ComposedStrategySpec(
        id="x",
        name="Sizing probe",
        description="",
        blocks=[
            SignalBlock(type="ma_cross", role="entry", params={"fast": 5, "slow": 10})
        ],
        **kwargs,
    )


def _blocks() -> list[SignalBlock]:
    return [SignalBlock(type="ma_cross", role="entry", params={"fast": 5, "slow": 10})]


# ---------------------------------------------------------------------------
# validate() — percent_equity
# ---------------------------------------------------------------------------


class TestValidatePercentEquity:
    def test_typical_value_is_accepted(self):
        assert (
            _spec(trade_size_mode="percent_equity", trade_size_percent=5.0).validate()
            is None
        )

    @pytest.mark.parametrize("pct", [0.0, -0.001, -50.0])
    def test_non_positive_percent_is_rejected(self, pct):
        err = _spec(trade_size_mode="percent_equity", trade_size_percent=pct).validate()
        assert err is not None and "trade_size_percent must be > 0" in err

    def test_smallest_positive_percent_is_accepted(self):
        """Alt sınır AÇIK: 0 reddedilir, 0'dan büyük her şey kabul."""
        assert (
            _spec(
                trade_size_mode="percent_equity", trade_size_percent=0.0001
            ).validate()
            is None
        )

    def test_hundred_percent_is_the_inclusive_upper_bound(self):
        """Üst sınır KAPALI: %100 (tüm sermaye) kaldıraçsız olarak geçerli."""
        assert (
            _spec(trade_size_mode="percent_equity", trade_size_percent=100.0).validate()
            is None
        )

    @pytest.mark.parametrize("pct", [100.0001, 150.0, 1_000.0])
    def test_over_hundred_percent_is_rejected_as_leverage(self, pct):
        err = _spec(trade_size_mode="percent_equity", trade_size_percent=pct).validate()
        assert err is not None and "<= 100" in err

    @pytest.mark.parametrize("pct", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_percent_is_rejected(self, pct):
        """NaN/inf, `pct <= 0` ve `pct > 100` karşılaştırmalarının İKİSİNİ de
        atlatır (NaN her karşılaştırmada False) — bu yüzden `math.isfinite`
        ayrı bir kapı olmak zorunda."""
        err = _spec(trade_size_mode="percent_equity", trade_size_percent=pct).validate()
        assert err is not None and "finite" in err

    @pytest.mark.parametrize("pct", ["abc", None, [], object()])
    def test_uncastable_percent_is_rejected_instead_of_raising(self, pct):
        """Doğrudan POST edilen çöp değer istisna değil, kullanıcıya mesaj üretir."""
        spec = _spec(trade_size_mode="percent_equity")
        spec.trade_size_percent = pct
        err = spec.validate()
        assert err is not None and "finite" in err

    def test_numeric_string_is_accepted_after_coercion(self):
        """`float(...)` dönüşümü sayısal string'i kabul eder (form yolu)."""
        spec = _spec(trade_size_mode="percent_equity")
        spec.trade_size_percent = "12.5"
        assert spec.validate() is None

    def test_bad_percent_is_ignored_in_other_sizing_modes(self):
        """Dallar moda BAĞLI: percent_equity seçili değilken alan denetlenmez.

        Aksi hâlde formda kalmış eski bir yüzde değeri, ilgisiz bir
        fixed_usdt stratejisini kaydedilemez hâle getirirdi.
        """
        assert (
            _spec(trade_size_mode="fixed", trade_size_percent=-5.0).validate() is None
        )


# ---------------------------------------------------------------------------
# validate() — atr_target / fixed_usdt
# ---------------------------------------------------------------------------


class TestValidateAtrTargetAndFixedUsdt:
    @pytest.mark.parametrize("risk", [0.0, -1.0])
    def test_non_positive_atr_risk_is_rejected(self, risk):
        err = _spec(trade_size_mode="atr_target", trade_size_atr_risk=risk).validate()
        assert err is not None and "trade_size_atr_risk must be > 0" in err

    def test_positive_atr_risk_is_accepted(self):
        assert (
            _spec(trade_size_mode="atr_target", trade_size_atr_risk=0.5).validate()
            is None
        )

    def test_atr_risk_is_ignored_outside_atr_target_mode(self):
        assert (
            _spec(trade_size_mode="fixed", trade_size_atr_risk=-1.0).validate() is None
        )

    @pytest.mark.parametrize("usdt", [0.0, -100.0])
    def test_non_positive_usdt_is_rejected(self, usdt):
        err = _spec(trade_size_mode="fixed_usdt", trade_size_usdt=usdt).validate()
        assert err is not None and "trade_size_usdt must be > 0" in err

    def test_positive_usdt_is_accepted(self):
        assert (
            _spec(trade_size_mode="fixed_usdt", trade_size_usdt=25.0).validate() is None
        )

    def test_usdt_is_ignored_outside_fixed_usdt_mode(self):
        assert _spec(trade_size_mode="fixed", trade_size_usdt=0.0).validate() is None


# ---------------------------------------------------------------------------
# validate() — vol_target (üç ayrı kapı)
# ---------------------------------------------------------------------------


class TestValidateVolTarget:
    def test_defaults_are_accepted(self):
        assert _spec(trade_size_mode="vol_target").validate() is None

    @pytest.mark.parametrize("target", [0.0, -0.02])
    def test_non_positive_target_is_rejected(self, target):
        err = _spec(
            trade_size_mode="vol_target", trade_size_vol_target=target
        ).validate()
        assert err is not None and "trade_size_vol_target must be > 0" in err

    @pytest.mark.parametrize("span", [1, 0, -3])
    def test_span_below_two_is_rejected(self, span):
        """EWMA volatilitesi en az iki gözlem ister; span=1 sıfır vol üretir
        ve `size = (vol_target / vol) * capital / price` sonsuza gider."""
        err = _spec(trade_size_mode="vol_target", trade_size_vol_span=span).validate()
        assert err is not None and "trade_size_vol_span must be >= 2" in err

    def test_span_of_exactly_two_is_the_inclusive_lower_bound(self):
        assert (
            _spec(trade_size_mode="vol_target", trade_size_vol_span=2).validate()
            is None
        )

    @pytest.mark.parametrize("capital", [0.0, -10_000.0])
    def test_non_positive_capital_is_rejected(self, capital):
        err = _spec(trade_size_mode="vol_target", trade_size_capital=capital).validate()
        assert err is not None and "trade_size_capital must be > 0" in err

    def test_target_is_checked_before_span(self):
        """Çoklu hata varken ilk kapı konuşur — mesaj sırası deterministik."""
        err = _spec(
            trade_size_mode="vol_target",
            trade_size_vol_target=0.0,
            trade_size_vol_span=1,
            trade_size_capital=0.0,
        ).validate()
        assert "trade_size_vol_target" in err

    def test_vol_fields_are_ignored_outside_vol_target_mode(self):
        assert (
            _spec(
                trade_size_mode="fixed",
                trade_size_vol_target=0.0,
                trade_size_vol_span=1,
                trade_size_capital=-1.0,
            ).validate()
            is None
        )


# ---------------------------------------------------------------------------
# validate() — bracket (SL/TP) dalları
# ---------------------------------------------------------------------------


class TestValidateBracket:
    @pytest.mark.parametrize("sl", [0.0, -2.0])
    def test_non_positive_sl_is_rejected_when_bracket_is_on(self, sl):
        err = _spec(use_bracket=True, sl_value=sl).validate()
        assert err is not None and "sl_value must be > 0" in err

    def test_non_positive_sl_is_ignored_when_bracket_is_off(self):
        """Bracket kapalıyken SL alanı emre dönüşmez; doğrulanması da gerekmez."""
        assert _spec(use_bracket=False, sl_value=0.0).validate() is None

    def test_non_positive_tp_is_rejected_only_when_tp_is_active(self):
        assert _spec(use_bracket=True, tp_type="off", tp_value=0.0).validate() is None
        err = _spec(use_bracket=True, tp_type="percent", tp_value=0.0).validate()
        assert err is not None and "tp_value must be > 0" in err

    def test_valid_bracket_passes(self):
        assert (
            _spec(
                use_bracket=True,
                sl_type="atr",
                sl_value=1.5,
                tp_type="atr",
                tp_value=3.0,
            ).validate()
            is None
        )


# ---------------------------------------------------------------------------
# build_spec — whitelist / clamp / dönüşüm
# ---------------------------------------------------------------------------


class TestBuildSpecWhitelists:
    @pytest.mark.parametrize(
        "field,bad,default,good",
        [
            ("entry_logic", "XOR", "OR", "AND"),
            ("exit_logic", "", "OR", "AND"),
            ("order_type", "stop", "market", "limit"),
            ("sl_type", "trailing", "percent", "atr"),
            ("tp_type", "trailing", "off", "percent"),
            ("trade_size_mode", "kelly", "fixed", "percent_equity"),
        ],
    )
    def test_unknown_value_falls_back_and_known_value_survives(
        self, field, bad, default, good
    ):
        """Whitelist dışı değer sessizce varsayılana düşer, geçerli değer korunur.

        İki yön de gerekli: yalnız reddetmeyi test etmek, whitelist'i fazla
        daraltıp meşru bir seçeneği düşüren bir regresyonu kaçırır.
        """
        assert (
            getattr(
                build_spec(name="n", description="", blocks=_blocks(), **{field: bad}),
                field,
            )
            == default
        )
        assert (
            getattr(
                build_spec(name="n", description="", blocks=_blocks(), **{field: good}),
                field,
            )
            == good
        )

    def test_none_falls_back_to_the_default(self):
        spec = build_spec(
            name="n",
            description="",
            blocks=_blocks(),
            entry_logic=None,
            order_type=None,
            sl_type=None,
            tp_type=None,
            trade_size_mode=None,
        )
        assert (
            spec.entry_logic,
            spec.order_type,
            spec.sl_type,
            spec.tp_type,
            spec.trade_size_mode,
        ) == ("OR", "market", "percent", "off", "fixed")

    def test_vol_target_stays_in_the_sizing_whitelist(self):
        """build_spec'in varlık sebeplerinden biri.

        Eski strategy.py clamp'i `vol_target`'ı whitelist'e almıyordu ve
        vol_target stratejilerini sessizce "fixed"e düşürüyordu: kullanıcı
        volatilite hedefli boyutlandırma seçiyor, kaydedilen spec sabit
        boyutla koşuyordu. Bu assert o regresyonun geri gelmesini engeller.
        """
        spec = build_spec(
            name="n", description="", blocks=_blocks(), trade_size_mode="vol_target"
        )
        assert spec.trade_size_mode == "vol_target"

    @pytest.mark.parametrize(
        "mode", ["fixed", "fixed_usdt", "percent_equity", "atr_target", "vol_target"]
    )
    def test_every_declared_sizing_mode_round_trips(self, mode):
        """TradeSizeMode literal'i ile whitelist'in AYNI kümeyi anlatması şart."""
        spec = build_spec(
            name="n", description="", blocks=_blocks(), trade_size_mode=mode
        )
        assert spec.trade_size_mode == mode
        assert ComposedStrategySpec.from_dict(spec.to_dict()).trade_size_mode == mode


class TestBuildSpecCoercion:
    def test_html_form_strings_become_numbers(self):
        """Form değerleri string gelir; spec sayısal alan bekler.

        Dönüşüm burada yapılmazsa `sl_value <= 0` gibi karşılaştırmalar
        string/float TypeError'ıyla patlar.
        """
        spec = build_spec(
            name="n",
            description="",
            blocks=_blocks(),
            trade_size="0.25",
            limit_offset_bps="7.5",
            sl_value="1.5",
            tp_value="3",
            atr_period="21",
            trade_size_percent="10",
            trade_size_atr_risk="2",
            trade_size_usdt="500",
            trade_size_vol_target="0.03",
            trade_size_vol_span="20",
            trade_size_capital="5000",
            trend_ema_period="100",
        )
        assert spec.trade_size == 0.25
        assert spec.limit_offset_bps == 7.5
        assert spec.sl_value == 1.5 and spec.tp_value == 3.0
        assert spec.atr_period == 21 and isinstance(spec.atr_period, int)
        assert spec.trade_size_vol_span == 20 and isinstance(
            spec.trade_size_vol_span, int
        )
        assert spec.trade_size_capital == 5000.0
        assert spec.trend_ema_period == 100

    def test_name_and_description_are_stripped(self):
        spec = build_spec(
            name="  Trend  ", description="  bir açıklama  ", blocks=_blocks()
        )
        assert spec.name == "Trend"
        assert spec.description == "bir açıklama"

    def test_blank_name_is_kept_blank_so_validate_rejects_it(self):
        """Sessiz "unnamed" fallback YOK.

        Boş ve yalnızca-boşluk isim aynı şekilde ele alınır; ikisi de
        validate()'e boş gider ve çağıran kullanıcıya yeniden sorar. Fallback
        olsaydı katalogda birbirinden ayırt edilemez satırlar oluşurdu.
        """
        for raw in ("", "   ", None):
            spec = build_spec(name=raw, description=None, blocks=_blocks())
            assert spec.name == ""
            assert spec.description == ""
            assert spec.validate() == "Strategy name is required."

    @pytest.mark.parametrize(
        "raw,expected", [(None, "60"), ("", "60"), ("  240  ", "240"), ("D", "D")]
    )
    def test_trend_interval_falls_back_to_hourly(self, raw, expected):
        spec = build_spec(
            name="n", description="", blocks=_blocks(), trend_interval=raw
        )
        assert spec.trend_interval == expected

    def test_blocks_list_is_copied(self):
        """Çağıranın listesini sonradan değiştirmesi spec'i bozmamalı."""
        blocks = _blocks()
        spec = build_spec(name="n", description="", blocks=blocks)
        blocks.append(SignalBlock(type="atr_stop", role="exit", params={}))
        assert len(spec.blocks) == 1

    def test_each_call_gets_a_fresh_id(self):
        a = build_spec(name="n", description="", blocks=_blocks())
        b = build_spec(name="n", description="", blocks=_blocks())
        assert a.id != b.id and len(a.id) == 12

    def test_new_spec_id_is_a_12_char_hex(self):
        sid = new_spec_id()
        assert len(sid) == 12 and int(sid, 16) >= 0

    def test_defaults_produce_a_valid_spec(self):
        """Hiçbir opsiyon verilmeden kurulan spec doğrudan geçerli olmalı."""
        assert build_spec(name="n", description="", blocks=_blocks()).validate() is None


class TestBuildSpecBooleans:
    @pytest.mark.parametrize(
        "field", ["use_bracket", "allow_short", "emulate", "trend_filter"]
    )
    def test_unchecked_checkbox_stays_false(self, field):
        """HTML formunda işaretlenmemiş checkbox "" gönderir → False."""
        spec = build_spec(name="n", description="", blocks=_blocks(), **{field: ""})
        assert getattr(spec, field) is False

    @pytest.mark.parametrize(
        "field", ["use_bracket", "allow_short", "emulate", "trend_filter"]
    )
    def test_checked_checkbox_becomes_true(self, field):
        spec = build_spec(name="n", description="", blocks=_blocks(), **{field: "on"})
        assert getattr(spec, field) is True


class TestAsBool:
    """`_as_bool` — işaretlenmemiş checkbox'ı True'ya çeviren bir regresyon,
    kullanıcının hiç istemediği bracket emirlerini ve short işlemleri açar."""

    @pytest.mark.parametrize(
        "raw", ["", "0", "false", "False", "FALSE", "off", "OFF", "no", "  ", " off "]
    )
    def test_form_falsy_vocabulary(self, raw):
        assert _as_bool(raw) is False

    @pytest.mark.parametrize("raw", ["on", "1", "true", "True", "yes", "y"])
    def test_form_truthy_vocabulary(self, raw):
        assert _as_bool(raw) is True

    def test_none_is_false(self):
        assert _as_bool(None) is False

    def test_real_bools_pass_through(self):
        assert _as_bool(True) is True
        assert _as_bool(False) is False

    def test_integer_zero_and_one(self):
        assert _as_bool(0) is False
        assert _as_bool(1) is True


# ---------------------------------------------------------------------------
# build_spec → validate() zinciri
# ---------------------------------------------------------------------------


class TestBuildSpecMeetsValidate:
    def test_clamped_spec_with_bad_sizing_is_still_rejected_by_validate(self):
        """Clamp whitelist'tir, doğrulama değil.

        build_spec `trade_size_percent`'i clamp'lemez (yalnızca float'a çevirir);
        sınır denetimi validate()'in işi. İki katmanın rollerinin karışmadığını
        çiviler.
        """
        spec = build_spec(
            name="n",
            description="",
            blocks=_blocks(),
            trade_size_mode="percent_equity",
            trade_size_percent="250",
        )
        assert spec.trade_size_percent == 250.0
        assert "<= 100" in spec.validate()

    def test_vol_target_spec_built_and_validated_end_to_end(self):
        spec = build_spec(
            name="Vol target",
            description="",
            blocks=_blocks(),
            trade_size_mode="vol_target",
            trade_size_vol_target="0.02",
            trade_size_vol_span="10",
            trade_size_capital="10000",
        )
        assert spec.validate() is None
        again = ComposedStrategySpec.from_dict(spec.to_dict())
        assert again.validate() is None
        assert math.isclose(again.trade_size_vol_target, 0.02)
