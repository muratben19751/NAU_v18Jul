"""`requirements.txt` ile `pyproject.toml` ıraksamasın.

İki kaynak vardı ve ikisi de sessizce yanlıştı:

* `requests` pyproject'te bildirilmişti, requirements.txt'te YOKTU — CI
  TOPLAMA aşamasında ölüyordu (2026-08-16, commit 4a5851f).
* `streamlit>=1.36` requirements.txt'in tepesinde "legacy/streamlit_app.py için
  tutuluyor" yorumuyla duruyordu; o dosya 2026-08-17'de silindi ve pyproject
  zaten paketi `[legacy]` ekstrasına koymuştu.

Ders bir bağımlılık listesine özgü değil: aynı bilginin iki kopyası varsa
biri bayatlar, ve bayat olan genelde ÇALIŞMAYAN taraf olmaz — sessizce
yanlış olan taraf olur. Bu test kopyayı türetilmiş hâle getiriyor.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _declared() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _listed() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [
        ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_requirements_txt_is_exactly_the_runtime_dependencies():
    assert _listed() == _declared(), (
        "requirements.txt pyproject'in [project] dependencies listesinden ayrıldı"
    )


def test_extras_are_not_smuggled_into_the_runtime_list():
    """`pytest`/`ruff`/`streamlit`/`yfinance` çalışma zamanı bağımlılığı değil.
    Ekstra bir paketi buraya koymak, onu kuran herkese zorunlu kılar — ve
    `streamlit` satırı tam olarak böyle, silinmiş bir dosya için aylarca
    kaldı."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = {
        name.split(">")[0].split("=")[0].split("[")[0].strip()
        for group in (data["project"].get("optional-dependencies") or {}).values()
        for name in group
    }
    listed = {ln.split(">")[0].split("=")[0].split("[")[0].strip() for ln in _listed()}

    assert not (extras & listed), (
        f"ekstra paketler runtime listesinde: {extras & listed}"
    )


def test_the_file_says_where_the_extras_live():
    """Liste daraldıysa okuyan kişi "pytest nereye gitti" diye sormamalı."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "[dev]" in text and "[legacy]" in text
