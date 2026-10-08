"""Evaluation harness (BUILD_SPEC.md section 6).

Built in Phase 3, not at the end. Without it there is no basis for claiming the
system works.

Citation fidelity
    Sample 30 draft claims, resolve each anchor, manually verify the passage
    actually supports the claim. Target above 95%.
Abstention
    Remove a source deliberately; confirm affected claims flip to
    ``cannot-verify`` rather than being answered from model knowledge.
Comparison accuracy
    Hand-label 20 claims against known-changed sources, measure agreement.

All three run under pytest as regression tests on every change.

Note: this package is ``updater.eval``, which shadows the ``eval`` builtin only
if imported as a bare name. Import it as ``updater.eval`` or alias it.
"""

from __future__ import annotations

__all__: list[str] = []
