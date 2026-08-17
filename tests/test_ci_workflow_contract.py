"""CI iş akışının sessizce zayıflayamayacağı yerler.

Kapsam eşiği (`--cov-fail-under=75`) artık pyproject'te değil, CI komutunda —
çünkü `[tool.coverage.report] fail_under` HER `--cov` koşumunu bağlıyordu ve
tek dosya koşan geliştirici %10 ile kırmızı alıyordu (ölçüldü 2026-08-17:
`pytest tests/test_engine_numeric_anchor.py --cov` → FAIL 10.01%). Eşik "TÜM
SÜİT uygulamanın ne kadarını dolaşıyor" ifadesidir; alt kümeye uygulanınca
ölçtüğü şey kalmaz.

Ama eşik tek yerdeyse o yer de denetlenmeli: bir bayrak komut satırından
düşerse kapı sessizce açılır ve süit yeşil kaldığı için kimse fark etmez.
Bu dosya CI sözleşmesinin ölçülebilir kısmını tutuyor.

Wiki References
---------------
See: [[nau_deepr_toplu_sertlestirme_2026_08]]
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Kök DOSYAYA göre. `Path("ci.yml")` cwd'ye bağlanırdı; `from conftest import
# REPO_ROOT` de çözüm değil — `tests/browser/conftest.py` aynı adı taşıyor ve
# sys.path'te öne geçebiliyor (ölçüldü 2026-08-17: ImportError).
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_coverage_gate_is_actually_passed_to_pytest(ci_text):
    assert "--cov-fail-under=75" in ci_text, (
        "kapsam kapısı CI komutundan düştü; pyproject'te de yok, yani kapı yok"
    )


def test_the_threshold_is_not_also_pinned_in_pyproject(ci_text):
    """İki kopya = ıraksama. Sayı tek yerde durmalı ve orası CI adımı."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    body = pyproject.split("[tool.coverage.report]", 1)
    assert len(body) == 2, "coverage.report bölümü kayboldu"
    assert "fail_under" not in body[1].split("[", 1)[0], (
        "fail_under pyproject'e geri kondu — kısmi koşumları da bağlar"
    )


def test_both_operating_systems_still_run_the_suite(ci_text):
    """Tek OS somut bir boşluktu: sandbox'ın POSIX dalı (RLIMIT_AS) Windows
    runner'da hiç çalışmıyordu."""
    assert "windows-latest" in ci_text
    assert "ubuntu-latest" in ci_text


def test_no_step_hides_its_own_failure(ci_text):
    """`continue-on-error`, gerçek bir gerilemeyi yalnız adımı kırmızı yapıp
    PR'ı bloklamadan geçirir — bu deponun kendi CI yorumları o deseni
    "sessizce merge" diye eleştiriyor.

    YORUMLAR ELENİYOR: ilk sürüm ham metinde arıyordu ve `continue-on-error`'ün
    NEDEN kaldırıldığını anlatan yorumun kendisine takıldı. Bir çıpanın ilk
    kurbanı, çıpanın var olma sebebini yazan satır olmamalı.
    """
    directives = "\n".join(
        line for line in ci_text.splitlines() if not line.lstrip().startswith("#")
    )

    assert "continue-on-error" not in directives
