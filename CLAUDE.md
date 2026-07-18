# Nautilus Web App — Claude Instructions

## Code Quality

After every Python file write or edit, `ruff check --fix` and `ruff format` run automatically via a PostToolUse hook (`.claude/settings.json`). No manual step needed.

Ruff runs as `python -m ruff` (resolved from the active interpreter — install with `pip install -e ".[dev]"` or `uv sync --extra dev`). Config lives in `pyproject.toml` under `[tool.ruff]`.
