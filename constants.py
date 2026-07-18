"""Single source of truth for the supported NautilusTrader version.

``NAUTILUS_REQUIRED`` is the only supported ``nautilus_trader`` version for this
app and must match the pin in ``pyproject.toml``. The runtime assertion in
``server.py`` (and ``assert_nautilus_version`` below) catches an installed wheel
that has drifted from this pin — the failure mode scattered version comments
used to hide.
"""

from __future__ import annotations

NAUTILUS_REQUIRED = "1.230.0"


def assert_nautilus_version() -> str:
    """Raise RuntimeError if the installed nautilus_trader != NAUTILUS_REQUIRED.

    Returns the installed version string on success.
    """
    import nautilus_trader

    installed = getattr(nautilus_trader, "__version__", "unknown")
    if installed != NAUTILUS_REQUIRED:
        raise RuntimeError(
            f"nautilus_trader {installed} is installed but this app is pinned to "
            f"{NAUTILUS_REQUIRED} (see constants.NAUTILUS_REQUIRED / pyproject.toml). "
            "Install the pinned version or update the pin deliberately."
        )
    return installed
