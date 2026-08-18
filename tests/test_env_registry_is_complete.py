"""`nau_config.ENV_VARS` ile kodun gerçekte okuduğu ortam değişkenleri örtüşsün.

DeepR 2026-08-11 [ORTA]: 77 ortam değişkeni, beş ön ek, 18 dosya ve hiçbir
merkezî tanım yoktu. Katalog (`nau_config.py`) o boşluğu kapatıyor — ama bir
katalog, güncel tutulması bir SÜREÇ ise çürür. Bu dosya onu bir MEKANİZMAYA
çeviriyor: sürüklenme iki yönde de kırmızıya döner.

  1. Kodda okunup katalogda olmayan değişken → yeni bir düğme eklendi ve
     kimse yazmadı; operatör onu asla öğrenemez.
  2. Katalogda olup kodda hiç okunmayan değişken → ölü düğme; belgelenmiş ama
     hiçbir şeyi etkilemiyor, bu belgelemenin kendisinden daha kötü.

Tarama AST ile değil metinle yapılıyor, bilinçli: değişken adları `os.environ`
dışında da geçiyor (`env_int("NAUTILUS_WF_FOLDS", 3)`, alt süreçlere geçirilen
`env` sözlükleri, hata mesajları). Aranan şey "şu ada bu kod tabanında bir yerde
değiniliyor mu" — kaynağı `nau_config.py`'nin kendisi olmamak şartıyla.

Kardeş kurallar: `tests/test_auto_layer_is_web_free.py` ve
`tests/test_web_layer_has_no_cycle.py` (bunlar sayfa değil MODÜL — wikilink
sözdizimi burada yanlış araçtı, bağ çözülmüyordu).

Wiki References: [[webapp_module_map]], [[surec_yoneticisi_ortami_dondurur]]
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import nau_config

ROOT = Path(__file__).resolve().parent.parent

# Ürün kodu olmayan ağaçlar: LLM'in ürettiği blok kodları, tek seferlik debug
# script'leri, vendored eğitim önbelleği, eski kod. `ruff` da bunları dışlıyor.
_SKIP_DIRS = {
    # `uv sync` sanal ortamı repo köküne kurar (.venv). Üçüncü parti paketler
    # kendi ANTHROPIC_*/NAUTILUS_* değişkenlerini okur ve tarama onları
    # "uygulamanın belgelenmemiş düğmesi" sanıyordu — katalog onları belgeleyemez,
    # çünkü uygulamanın ayar yüzeyi değiller. Kardeş kural
    # `test_data_dir_is_the_only_storage_root` bu dizini zaten dışlıyordu.
    ".venv",
    "venv",
    "site-packages",
    "work",
    "debug",
    "legacy",
    "unsloth_compiled_cache",
    "nautilus_wiki",
    ".claude",
    ".git",
    "__pycache__",
    "tests",
    "auto_out",
    "design2",
}

# Uygulamanın kendi ön ekleri + doğrudan kullandığı sağlayıcı değişkenleri.
# `PATH`, `HOME`, `TEMP` gibi işletim sistemi değişkenleri kapsam dışı: onlar
# uygulamanın ayar yüzeyi değil.
_NAME_RE = re.compile(
    r"[\"'](NAU_[A-Z_0-9]+|NAUTILUS_[A-Z_0-9]+|AGENT_[A-Z_0-9]+|STUDIO_[A-Z_0-9]+"
    r"|OPENROUTER_[A-Z_0-9]+|ANTHROPIC_[A-Z_0-9]+|MASSIVE_[A-Z_0-9]+"
    r"|POLYGON_API_KEY|PM2_HOME)[\"']"
)


def _product_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if p.name == "nau_config.py":
            continue  # kataloğun kendisi kaynak sayılmaz
        out.append(p)
    return out


@pytest.fixture(scope="module")
def referenced() -> dict[str, list[str]]:
    """Ürün kodunda geçen değişken adı → geçtiği dosyalar."""
    hits: dict[str, list[str]] = {}
    files = _product_files()
    assert len(files) > 50, f"tarama çok az dosya buldu ({len(files)}) — çıpa kırık"
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        for name in set(_NAME_RE.findall(text)):
            hits.setdefault(name, []).append(str(path.relative_to(ROOT)))
    return hits


class TestRegistryMatchesTheCode:
    def test_every_env_var_the_code_uses_is_documented(self, referenced):
        missing = sorted(set(referenced) - set(nau_config.BY_NAME))
        detail = "\n".join(f"  {n}  ← {', '.join(referenced[n])}" for n in missing)
        assert not missing, (
            "Kodda geçen ama `nau_config.ENV_VARS` içinde olmayan ortam "
            f"değişkenleri var — operatör bu düğmeleri öğrenemez:\n{detail}"
        )

    def test_no_documented_env_var_is_dead(self, referenced):
        dead = sorted(set(nau_config.BY_NAME) - set(referenced))
        assert not dead, (
            "`nau_config.ENV_VARS` içinde yazılı ama kodda hiç okunmayan "
            f"değişkenler var (ölü düğme): {dead}"
        )


class TestRegistryIsWellFormed:
    def test_names_are_unique(self):
        names = [v.name for v in nau_config.ENV_VARS]
        assert len(names) == len(set(names)), "katalogda tekrar eden ad var"

    def test_every_entry_has_a_purpose_sentence(self):
        thin = [v.name for v in nau_config.ENV_VARS if len(v.purpose) < 20]
        assert not thin, f"amaç cümlesi eksik/çok kısa: {thin}"

    def test_every_group_member_is_in_env_vars(self):
        grouped = [v for _, g in nau_config.GROUPS for v in g]
        assert len(grouped) == len(nau_config.ENV_VARS), (
            "GROUPS ile ENV_VARS ayrışmış — tabloda görünmeyen değişken olur"
        )

    def test_kinds_are_known(self):
        allowed = {"str", "int", "float", "bool", "path", nau_config.SECRET}
        bad = [v.name for v in nau_config.ENV_VARS if v.kind not in allowed]
        assert not bad, f"bilinmeyen tip: {bad}"


class TestSecretsAreNeverPrinted:
    def test_render_table_masks_secret_values(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-super-secret-value"}
        table = nau_config.render_table(env)
        assert "sk-ant-super-secret-value" not in table
        assert "<ayarlı, 25 karakter>" in table

    def test_non_secret_values_are_shown(self):
        table = nau_config.render_table({"NAUTILUS_WF_FOLDS": "7"})
        assert "yürürlükte: 7" in table

    def test_effective_is_empty_for_unset(self):
        assert nau_config.effective("NAU_ACCESS_TOKEN", {}) == ""
