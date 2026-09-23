"""Locate the marketplace lib/ directory from inside any skill's scripts/.

Every skill folder is independently valid per the agentskills.io spec, but the
skills share one vendored library at the marketplace root. Each script calls
``ensure_lib_on_path()`` before importing ``geo_audit``.

Layout::

    <marketplace root>/
        lib/geo_audit/...
        skills/<skill>/scripts/<script>.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_marketplace_root(start: Path) -> Path | None:
    """Walk upward from *start* looking for a directory containing lib/geo_audit."""
    for candidate in [start, *start.parents]:
        if (candidate / "lib" / "geo_audit" / "__init__.py").is_file():
            return candidate
    return None


def ensure_lib_on_path(script_file: str) -> Path:
    """Put the marketplace lib/ on sys.path. Returns the marketplace root."""
    here = Path(script_file).resolve()
    root = find_marketplace_root(here.parent)
    if root is None:
        raise RuntimeError(
            "Could not locate the marketplace root (a directory containing "
            f"lib/geo_audit/) by walking up from {here}. The skill folder must "
            "stay inside the marketplace tree."
        )
    lib = str(root / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return root
