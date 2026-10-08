"""Annual Research Update Agent for A Start in Life (ASIL).

`BUILD_SPEC.md` is the contract this package implements; `DESIGN.md` states the
five non-negotiable design rules that govern every change to it.

This module stays deliberately thin — import from the submodule you need
(``updater.models``, ``updater.ingest``, ...) so the package has no import
cycles and no side effects at import time.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
