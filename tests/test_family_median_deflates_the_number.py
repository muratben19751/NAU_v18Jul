"""Söndürücü ölçüt: bir adayın beklentisi, AİLESİNİN medyanıdır.

Bu ölçüt bir keşif değil, bir ölçümün sonucu. 2026-08-21'de üç enstrümanda
(QQQC/SPY/IBM, ~30 MA parametrelendirmesi, serinin iki yarısı) şu ölçüldü:

  * aile içi parametre SIRALAMASI zamanda kalıcı değil — altı ölçümün
    hiçbirinde güven aralığı pozitif tarafta durmadı, üçünde tamamen negatif;
  * ilk yarının ŞAMPİYONUNU seçmek, hiç seçmeyip aile MEDYANINI almaya kıyasla
    ikinci yarıda -0,03 / -0,00 / -0,02 Calmar getirdi — yani sıfır.

Buradan AYIRT EDİCİ bir ölçüt çıkmaz (o tam olarak çürütülen şey), SÖNDÜRÜCÜ
bir ölçüt çıkar: adayın kendi sayısı in-sample seçim gürültüsü taşır, ailesinin
medyanı taşımaz.

Bkz. tests/test_low_frequency_has_its_own_evidence.py — kalıcılık ölçümünün
kendisi ve teşhisin kapsamı orada çivili.
"""

from __future__ import annotations

import ast
import inspect
import os
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import auto.robustness as ar
from auto.robustness import _family_siblings, family_median_expectation
from composer import ComposedStrategySpec


def _spec(fast: int = 20, slow: int = 100, sid: str = "cand-1"):
    return ComposedStrategySpec.from_dict(
        {
            "id": sid,
            "name": f"MA {fast}/{slow}",
            "description": "",
            "blocks": [
                {
                    "type": "ma_cross",
                    "role": "entry",
                    "params": {"fast": fast, "slow": slow, "direction": "up"},
                },
                {
                    "type": "ma_cross",
                    "role": "exit",
                    "params": {"fast": fast, "slow": slow, "direction": "down"},
                },
            ],
            "entry_logic": "OR",
            "exit_logic": "OR",
            "allow_short": False,
            "trade_size_mode": "percent_equity",
            "trade_size_percent": 95.0,
            "trade_size": 10,
        }
    )


def _bars(n: int = 3000) -> pd.DataFrame:
    """Kardeşlerin ÖLÇÜLEBİLECEĞİ kadar uzun bir seri.

    800 barda `slow` 300'e kadar çıkan kardeşlerin çoğu asgari işlem sayısına
    ulaşamıyordu ve iki test atlanıyordu — atlanan test hiçbir şey kanıtlamaz.
    """
    rng = np.random.default_rng(7)
    close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.012, n))
    idx = pd.date_range("2010-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1000.0,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Kardeş üretimi: aile YAPIDIR, sayılar değil
# ---------------------------------------------------------------------------


def test_siblings_keep_the_family_and_change_only_the_numbers():
    sibs = _family_siblings(_spec(), 12, seed=3)
    assert sibs is not None and len(sibs) == 12
    base = _spec().to_dict()
    for sib in sibs:
        # Yapı sabit: blok tipleri, roller, mantık, boyutlandırma.
        assert [b["type"] for b in sib["blocks"]] == [b["type"] for b in base["blocks"]]
        assert [b["role"] for b in sib["blocks"]] == [b["role"] for b in base["blocks"]]
        assert sib["entry_logic"] == base["entry_logic"]
        assert sib["trade_size_mode"] == base["trade_size_mode"]
        assert sib["trade_size_percent"] == base["trade_size_percent"]
        for blk, ref in zip(sib["blocks"], base["blocks"], strict=True):
            # Enum adayın değerinde kalır — yön değiştirmek AİLEYİ değiştirir.
            assert blk["params"]["direction"] == ref["params"]["direction"]
            # ...ama sayılar geçerli aralıkta ve fast < slow.
            assert 2 <= blk["params"]["fast"] <= 100
            assert 5 <= blk["params"]["slow"] <= 300
            assert blk["params"]["fast"] < blk["params"]["slow"]


def test_sampling_does_not_collapse_to_one_pair():
    """Geçerlilik, çeşitliliği yiyerek sağlanmamalı.

    Projede hazır duran `_fix_fast_slow`, geçersiz çifti sabit 10/40'a çeviriyor.
    Kardeş üretiminde onu kullanmak, örneklemin büyük kısmını TEK bir parametre
    çiftine çökertir ve "aile medyanı" aslında tek bir kardeşin sayısı olurdu.
    """
    sibs = _family_siblings(_spec(), 20, seed=11)
    pairs = {
        (b["blocks"][0]["params"]["fast"], b["blocks"][0]["params"]["slow"])
        for b in sibs
    }
    assert len(pairs) >= 15, f"örneklem çöktü: yalnız {len(pairs)} farklı çift"


def test_siblings_are_deterministic():
    """AYNI spec nesnesinden iki üretim birebir aynı olmalı.

    Test bir ara İKİ ayrı `_spec()` kuruyordu ve aralıklı olarak kırılıyordu:
    `ComposedStrategySpec.from_dict` her çağrıda `created_at`'e o anın damgasını
    basıyor, iki çağrı saniye sınırını geçtiğinde spec'ler ayrışıyor. Ürün
    tarafı bundan etkilenmiyor — `_family_siblings` `to_dict()`'i BİR kez
    okuyor, dolayısıyla bir çağrının bütün kardeşleri aynı damgayı taşıyor —
    ama testin iddiası yanlış kurulmuştu.
    """
    spec = _spec()
    assert _family_siblings(spec, 10, seed=5) == _family_siblings(spec, 10, seed=5)


def test_all_siblings_of_one_call_share_the_candidates_identity_fields():
    """Kardeşler yalnız SAYILARDA ayrışır; damga/kimlik alanları ortak.

    Aksi hâlde "aile" içinde ölçülemeyen bir değişken daha olurdu.
    """
    sibs = _family_siblings(_spec(), 10, seed=5)
    stamps = {s["created_at"] for s in sibs}
    assert len(stamps) == 1, "kardeşler farklı zaman damgası taşıyor"


def test_custom_blocks_are_out_of_scope_in_v1():
    spec = _spec()
    d = spec.to_dict()
    d["blocks"][0]["type"] = "totally_not_a_builtin_block"
    assert _family_siblings(ComposedStrategySpec.from_dict(d), 10, seed=1) is None


# ---------------------------------------------------------------------------
# Ölçümün kendisi
# ---------------------------------------------------------------------------


def test_reports_the_family_median_next_to_the_candidate():
    bars = _bars()
    ev = family_median_expectation(_spec(), bars, n_siblings=10)
    if ev is None:
        pytest.skip("sentetik seride yeterli ölçülebilir kardeş yok")
    assert set(ev) == {
        "own_calmar",
        "median_calmar",
        "iqr_calmar",
        "n_siblings",
        "n_valid",
    }
    assert ev["n_valid"] >= ar.FAMILY_MIN_VALID
    assert ev["n_valid"] <= ev["n_siblings"] == 10
    lo, hi = ev["iqr_calmar"]
    assert lo <= ev["median_calmar"] <= hi


def test_measurement_is_deterministic():
    bars = _bars()
    a = family_median_expectation(_spec(), bars, n_siblings=10)
    b = family_median_expectation(_spec(), bars, n_siblings=10)
    assert a == b


def test_determinism_survives_a_new_process():
    """AYNI süreçteki iki çağrının eşitliği determinizm KANITI DEĞİL.

    Tohum bir ara `hash(spec.id)` ile türetiliyordu. `hash()` str üzerinde
    PYTHONHASHSEED ile süreç başına rastgeleleşir: yukarıdaki test geçiyordu,
    ama iki ayrı KOŞU farklı kardeşler üretiyordu — yani oturum artefaktları
    yeniden üretilemezdi. Ölçüldü: aynı dizge, üç süreç, üç farklı tohum.

    Bu yüzden kanıt süreç sınırının ÖTESİNDEN gelmeli.
    """
    import json
    import subprocess
    import sys

    prog = (
        "import json,logging;logging.disable(logging.WARNING);"
        "import auto.robustness as ar;"
        "from composer import ComposedStrategySpec;"
        "spec=ComposedStrategySpec.from_dict(json.loads(input()));"
        "print(json.dumps(ar._family_siblings(spec,8,ar.zlib.crc32(b'cand-1'))))"
    )
    payload = json.dumps(_spec().to_dict())
    outs = []
    for hashseed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hashseed, "PYTHONUTF8": "1"}
        r = subprocess.run(
            [sys.executable, "-c", prog],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
            timeout=180,
        )
        assert r.returncode == 0, r.stderr[-800:]
        outs.append(r.stdout.strip().splitlines()[-1])
    assert outs[0] == outs[1] == outs[2], (
        "kardeşler PYTHONHASHSEED'e göre değişiyor — tohum süreç-rastgele"
    )


def test_silently_shrinking_the_sample_is_impossible():
    """Kaç kardeş istendi / kaçı ölçüldü — ikisi de raporlanır.

    Sessiz daralma bu projede tekrarlayan bir arıza sınıfı: kapı 5 peer sanarken
    2 ile karar veriyordu. Aynı hata burada "20 kardeşin medyanı" denip 9
    kardeşle karar vermek olurdu.
    """
    bars = _bars()
    ev = family_median_expectation(_spec(), bars, n_siblings=10)
    if ev is None:
        pytest.skip("sentetik seride yeterli ölçülebilir kardeş yok")
    assert ev["n_siblings"] == 10
    assert isinstance(ev["n_valid"], int)


def test_refuses_rather_than_guessing():
    """Asgari geçerli kardeş sayısının altında istek → ölçüm yapılmaz."""
    assert family_median_expectation(_spec(), _bars(), n_siblings=3) is None


def test_no_bars_no_measurement():
    assert family_median_expectation(_spec(), None) is None
    assert family_median_expectation(_spec(), pd.DataFrame()) is None


def test_huge_frames_are_skipped_not_ground_through():
    """1-DAKİKA kripto çerçevesi ~1M bar; 20 kardeş dakikalar sürerdi.

    Ölçüldü: 1-DAY (5.762 bar) tek backtest 0,27 sn, 1-HOUR (40.236 bar) 1,30 sn
    — yani maliyet bar sayısıyla doğrusal. Eşik bunun için var.
    """
    assert ar.FAMILY_MAX_BARS > 40_000, "saatlik seriler ölçülebilmeli"
    idx = pd.date_range(
        "2010-01-01", periods=ar.FAMILY_MAX_BARS + 1, freq="1min", tz="UTC"
    )
    big = pd.DataFrame({"close": np.ones(len(idx))}, index=idx)
    assert family_median_expectation(_spec(), big) is None


# ---------------------------------------------------------------------------
# Neyi YAPMAMALI
# ---------------------------------------------------------------------------


def _body(fn) -> str:
    """Fonksiyonun ÇALIŞAN gövdesi — docstring ve yorumlar hariç.

    Kaynak metni üzerinden iddia kurmak bu oturumda üç kez yanlış alarm verdi:
    açıklama yorumları yasaklı kelimeyi ANLATMAK için içeriyor.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return ast.unparse(node)


def test_the_candidates_rank_within_the_family_is_never_reported():
    """Sıra, tam olarak gürültü olduğu ÖLÇÜLEN büyüklük.

    "20 kardeş içinde 3." satırı ekranda göründüğü an, az önce çürütülen
    çıkarımı davet eder: bu aday ailesinin iyi tarafında. Ölçüm bunun bir
    sonraki dönemde anlamı olmadığını söylüyor.
    """
    ev = family_median_expectation(_spec(), _bars(), n_siblings=10)
    if ev is not None:
        assert "rank" not in ev and "percentile" not in ev
    code = _body(family_median_expectation)
    assert "rank" not in code and "percentile" not in code


def test_the_family_median_never_decides():
    """Rapor eder, KARAR VERMEZ — ne kapıda ne verdict yapılarında."""
    code = _body(family_median_expectation)
    assert "WfoVerdict" not in code
    for gate in (
        ar.wfo_verdict,
        ar.multi_symbol_definitive_failure,
        ar.split_definitive_failure,
    ):
        assert "family_median_expectation" not in inspect.getsource(gate)

    import web.routes.agent_backtest as gate_mod

    tally = inspect.getsource(gate_mod._robustness_tally)
    assert "family" not in tally, "söndürücü ölçüt kapıya sızmış"


def test_the_pipeline_reports_it_and_degrades_to_an_absent_key():
    """Ölçülemezse anahtar HİÇ yazılmaz — boş sözlük 'ölçüldü ve boş' okunurdu."""
    src = inspect.getsource(ar.run_full_robustness)
    assert "family_median_expectation" in src
    assert '**({"family": family} if family else {})' in src
    # Ve hatası paketi düşürmemeli.
    assert "except Exception as fam_exc" in src
