"""`run_multi_symbol`'ün genelleme skorlaması ve etiketleme eşikleri.

DeepR 2026-08-11 [ORTA]: bu fonksiyon kullanıcıya doğrudan bir HÜKÜM gösteriyor
("✓ Generalizable" / "⚠ Limited" / "✗ Symbol specific") ve hükmün çıktığı mantık
saf: geçerli-sonuç filtresi (`not error and n_trades >= 5 and
excess_return_fraction is not None`), `pass_rate = n_positive / n_valid`,
`>= 0.7` / `>= 0.4` eşikleri, `n_valid == 0` → "— (yetersiz veri)" ve yalnız
geçerli sonuçlardan hesaplanan `avg_sharpe`. `grep -rn "run_multi_symbol"
tests/` sıfır referans veriyordu; kaynağın kendi yorumu filtrenin bir kez 3'ten
5'e çıkarıldığını (`# 3→5`) not ediyor, yani bu sabitler DEĞİŞİYOR ve hiçbir
test onları korumuyordu. Eşiklerden ya da `n_trades >= 5` filtresinden birinin
bozulması, veri eksikliği yüzünden hiç ölçülememiş bir stratejiyi
"Generalizable" diye etiketleyebilir.

Testler ``run_many`` enjeksiyonundan gidiyor: paralel dal her sembolü bir iş
birimi olarak dışarı verdiği için gerçek veri/backtest'e hiç dokunmadan saf
skorlama mantığı sürülebiliyor.

Ayrıca dün bu alanda değişen iki davranış da pinleniyor:
* peer sepeti DİKİŞ-farkındalıklı (`agent_backtest._peer_exclusions`),
* "veri yok" ile "hepsi patladı" ayrımı bedelsiz değil (`_ms_score_factor`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backtest_robustness import run_multi_symbol


class _Spec:
    """`run_multi_symbol`'ün paralel dalda spec'ten istediği tek şey."""

    name = "t"

    def to_dict(self) -> dict:
        return {"id": "t", "name": "t"}


def _payload(**metrics) -> dict:
    m = {
        "pnl": 100.0,
        "pnl_pct": 0.01,
        "sharpe": 1.0,
        "n_trades": 20,
        "benchmark_return_fraction": 0.0,
        "excess_return_fraction": 0.05,
    }
    m.update(metrics)
    return {"metrics": m}


def _run(per_symbol: dict[str, dict]) -> dict:
    """`per_symbol`: {sembol: payload} — payload'ı olduğu gibi run_many döndürür."""
    symbols = list(per_symbol)

    def fake_run_many(units, **kw):
        assert [u["symbol"] for u in units] == symbols
        return {f"ms:{s}": per_symbol[s] for s in symbols}

    return run_multi_symbol(
        _Spec(),
        primary_symbol=symbols[0],
        symbols=symbols,
        interval="60",
        run_many=fake_run_many,
    )


# ── Etiket eşikleri ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n_positive,n_total,expected_label,expected_rate",
    [
        (10, 10, "✓ Generalizable", 1.0),  # %100
        (7, 10, "✓ Generalizable", 0.7),  # tam eşikte: DAHİL
        (69, 100, "⚠ Limited", 0.69),  # eşiğin bir tık altı
        (4, 10, "⚠ Limited", 0.4),  # tam eşikte: DAHİL
        (39, 100, "✗ Symbol specific", 0.39),  # eşiğin bir tık altı
        (0, 10, "✗ Symbol specific", 0.0),  # hiçbiri alfa üretmedi
    ],
)
def test_label_thresholds_are_inclusive_at_070_and_040(
    n_positive, n_total, expected_label, expected_rate
):
    """0.7 ve 0.4 sınırları DAHİL; altına düşen bir tık aşağı iner.

    Eşiği `>` yapan bir düzenleme tam sınırdaki koşuları sessizce bir kademe
    aşağı atardı — bu satırlar o kaymayı yakalar.
    """
    per_symbol = {}
    for i in range(n_total):
        excess = 0.05 if i < n_positive else -0.05
        per_symbol[f"S{i}"] = _payload(excess_return_fraction=excess)

    out = _run(per_symbol)

    assert out["generalization_label"] == expected_label
    assert out["pass_rate"] == pytest.approx(round(expected_rate, 2))
    assert out["symbols_valid"] == n_total
    assert out["symbols_positive"] == n_positive
    assert out["symbols_tested"] == n_total


def test_zero_excess_is_not_positive():
    """Alfa "> 0" olmalı: tam sıfır aşkın getiri pozitif SAYILMAZ."""
    out = _run({"A": _payload(excess_return_fraction=0.0)})
    assert out["symbols_valid"] == 1 and out["symbols_positive"] == 0


# ── Geçerli-sonuç filtresi ───────────────────────────────────────────────────


def test_thin_symbols_are_not_counted_as_valid():
    """`n_trades >= 5` filtresi (bir kez 3'ten 5'e çıkarıldı) korunmalı.

    4 trade'lik bir sembol geçerli sayılsaydı, gürültüden ibaret bir sonuç
    genelleme oranını yukarı çekerdi.
    """
    out = _run(
        {
            "THIN": _payload(n_trades=4, excess_return_fraction=0.9),
            "OK": _payload(n_trades=5, excess_return_fraction=-0.1),
        }
    )
    assert out["symbols_tested"] == 2
    assert out["symbols_valid"] == 1  # yalnız OK
    assert out["symbols_positive"] == 0
    assert out["generalization_label"] == "✗ Symbol specific"


def test_unmeasured_excess_is_not_counted_as_valid():
    """`excess_return_fraction is None` = alfa ÖLÇÜLMEDİ.

    Ham PnL pozitif olsa bile (boğa piyasasında sıradan) bu bir geçiş değildir.
    """
    out = _run(
        {
            "NOBENCH": _payload(pnl=9999.0, excess_return_fraction=None),
            "OK": _payload(excess_return_fraction=0.05),
        }
    )
    assert out["symbols_valid"] == 1 and out["symbols_positive"] == 1
    assert out["generalization_label"] == "✓ Generalizable"


def test_errors_and_missing_data_are_excluded_from_the_rate():
    """Hata / veri yok satırları oranın PAYDASINA girmez ama listede kalır."""
    out = _run(
        {
            "ERR": {"error": "boom"},
            "NODATA": {"error": "no data"},
            "OK": _payload(excess_return_fraction=0.05),
        }
    )
    assert out["symbols_tested"] == 3
    assert out["symbols_valid"] == 1
    assert out["pass_rate"] == 1.0
    by_symbol = {r["symbol"]: r for r in out["results"]}
    assert by_symbol["ERR"]["error"] == "boom" and by_symbol["ERR"]["pnl"] is None
    assert by_symbol["NODATA"]["error"] == "no data"
    # Eksik payload da "no result" olarak hata sayılır, sessizce düşmez.
    out2 = _run({"GONE": None, "OK": _payload()})
    assert {r["symbol"]: r["error"] for r in out2["results"]}["GONE"] == "no result"


# ── "Ölçülemedi" ile "başarısız" ayrımı ──────────────────────────────────────


def test_no_valid_symbol_is_labelled_insufficient_data_not_a_failure():
    """Hiç geçerli sembol yoksa etiket "yetersiz veri" — "✗" DEĞİL.

    Bu ayrım bedelsiz değil: `agent_backtest._ms_score_factor` bu etiketi (ya da
    `n_valid == 0`) görünce skoru nötr 0.575 ile çarpıyor; "✗ Symbol specific"
    görseydi asgari 0.15 cezasını uygulayıp hiç ölçülmemiş bir adayı elerdi.
    """
    from web.routes import agent_backtest as ab

    out = _run({"ERR": {"error": "boom"}, "NODATA": {"error": "no data"}})
    assert out["symbols_valid"] == 0
    assert out["pass_rate"] == 0.0  # sayısal alan 0, ama etiket bunu "sıfır" demiyor
    assert "yetersiz" in out["generalization_label"]
    assert out["avg_sharpe"] is None

    # Skorlama tarafı bu 0.0'ı gerçek bir sıfır sanmamalı.
    assert ab._ms_score_factor({"multi_symbol": out}) == 0.575
    # Karşılaştırma: gerçekten ölçülüp sıfır alan bir koşu cezayı yer.
    measured_zero = _run(
        {f"S{i}": _payload(excess_return_fraction=-0.1) for i in range(5)}
    )
    assert measured_zero["generalization_label"] == "✗ Symbol specific"
    assert ab._ms_score_factor({"multi_symbol": measured_zero}) == pytest.approx(0.15)


# ── avg_sharpe ───────────────────────────────────────────────────────────────


def test_avg_sharpe_uses_only_valid_results():
    """Ortalama Sharpe yalnız GEÇERLİ sonuçlardan; ince/hatalı semboller girmez."""
    out = _run(
        {
            "A": _payload(sharpe=2.0, excess_return_fraction=0.05),
            "B": _payload(sharpe=1.0, excess_return_fraction=-0.05),
            "THIN": _payload(sharpe=99.0, n_trades=1),  # filtre dışı
            "ERR": {"error": "boom"},
        }
    )
    assert out["avg_sharpe"] == pytest.approx(1.5)


def test_avg_sharpe_is_none_when_no_symbol_reported_one():
    out = _run({"A": _payload(sharpe=None)})
    assert out["symbols_valid"] == 1
    assert out["avg_sharpe"] is None


# ── Peer sepeti: dikiş farkındalığı ──────────────────────────────────────────


def test_peer_basket_excludes_stitch_components_both_ways(monkeypatch):
    """QQQC = `stitch:QQQ+QQQQ` olduğu için QQQ bir QQQC koşusunda peer OLAMAZ.

    Düz `p != symbol` karşılaştırması aynı veriyi ikinci kez soruyordu; test
    "genelleşebilirlik" diye görünen o tekrarın geri gelmesini engeller.
    """
    from web.routes import agent_backtest as ab

    manifest = {
        "QQQC": {"source": "stitch:QQQ+QQQQ"},
        "QQQ": {"source": "polygon"},
        "QQQQ": {"source": "polygon"},
        "SPY": {"source": "polygon"},
    }
    monkeypatch.setattr("data.external_manifest", lambda: manifest)

    assert ab._peer_exclusions("QQQC.NASDAQ") == {"QQQC", "QQQ", "QQQQ"}
    # Ters yön: bileşenin koşusunda dikilmiş seri de peer olamaz.
    assert ab._peer_exclusions("QQQ.NASDAQ") == {"QQQ", "QQQC"}
    # Dikişle ilgisi olmayan sembol yalnız kendini dışlar.
    assert ab._peer_exclusions("SPY.ARCA") == {"SPY"}


def test_peer_exclusions_degrade_to_self_when_manifest_unreadable(monkeypatch):
    """Manifest okunamazsa sepet boşalmaz: en azından sembolün kendisi dışlanır."""
    from web.routes import agent_backtest as ab

    def boom():
        raise OSError("manifest locked")

    monkeypatch.setattr("data.external_manifest", boom)
    assert ab._peer_exclusions("QQQC.NASDAQ") == {"QQQC"}


# ── Sonuç satırlarının şekli ─────────────────────────────────────────────────


def test_result_rows_keep_the_reported_numbers():
    """Satırlar yuvarlanır ama içerik korunur (UI doğrudan bunları basıyor)."""
    out = _run(
        {"A": _payload(pnl=123.456, sharpe=1.2345, excess_return_fraction=0.123456789)}
    )
    row = out["results"][0]
    assert row["symbol"] == "A"
    assert row["pnl"] == 123.46
    assert row["sharpe"] == 1.23
    assert row["excess_return_fraction"] == 0.123457
    assert row["error"] is None
    assert out["primary_symbol"] == "A"


def test_progress_lines_report_the_verdict():
    """İlerleme metni son hükmü ve oranı taşımalı (kullanıcı bunu okuyor)."""
    lines: list[str] = []

    def fake_run_many(units, **kw):
        return {f"ms:{u['symbol']}": _payload() for u in units}

    run_multi_symbol(
        _Spec(),
        primary_symbol="A",
        symbols=["A", "B"],
        interval="60",
        run_many=fake_run_many,
        progress_fn=lines.append,
    )
    joined = "\n".join(lines)
    assert "2/2 symbols positive alpha" in joined
    assert "✓ Generalizable" in joined


def test_progress_callback_failure_never_breaks_the_scan():
    """Bozuk bir progress_fn skorlamayı düşürmemeli (yalnız o yutulur)."""

    def boom(_msg):
        raise RuntimeError("ui gone")

    def fake_run_many(units, **kw):
        return {f"ms:{u['symbol']}": _payload() for u in units}

    out = run_multi_symbol(
        _Spec(),
        primary_symbol="A",
        symbols=["A"],
        interval="60",
        run_many=fake_run_many,
        progress_fn=boom,
    )
    assert out["generalization_label"] == "✓ Generalizable"


# ── Seri dal ile paritesi ────────────────────────────────────────────────────


def test_serial_branch_scores_identically(monkeypatch):
    """run_many yokken (seri dal) aynı girdiler aynı hükmü vermeli.

    İki dal ayrı ayrı sonuç biriktiriyor; skorlama bloğu ortak olsa da
    filtrelenen alanları farklı doldururlarsa hüküm ıraksardı.
    """
    import pandas as pd

    import backtest_robustness as br

    bars = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
        },
        index=pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC"),
    )
    monkeypatch.setattr("data.load_bybit_bars", lambda **kw: bars)
    monkeypatch.setattr(
        "backtest._make_bybit_instrument",
        lambda **kw: SimpleNamespace(id=SimpleNamespace(venue="BYBIT")),
    )
    monkeypatch.setattr("backtest._make_bybit_bar_type", lambda *a, **k: object())
    # `_stamp_window_benchmark` gerçek metriği damgalar; testte sabitliyoruz.
    monkeypatch.setattr(br, "_stamp_window_benchmark", lambda m, df: None)

    outcomes = {
        "A": SimpleNamespace(error=None, metrics=_payload()["metrics"]),
        "B": SimpleNamespace(
            error=None, metrics=_payload(excess_return_fraction=-0.05)["metrics"]
        ),
        "C": SimpleNamespace(error="engine blew up", metrics={}),
    }

    def fake_backtest(spec, df, **kw):
        return outcomes[kw["rationale"].rsplit(" · ", 1)[1]]

    monkeypatch.setattr("backtest.run_composed_backtest", fake_backtest)

    out = run_multi_symbol(
        _Spec(), primary_symbol="A", symbols=["A", "B", "C"], interval="60"
    )
    assert out["symbols_tested"] == 3
    assert out["symbols_valid"] == 2 and out["symbols_positive"] == 1
    assert out["pass_rate"] == 0.5
    assert out["generalization_label"] == "⚠ Limited"
    assert {r["symbol"]: r["error"] for r in out["results"]}["C"] == "engine blew up"
