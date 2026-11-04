"""Evaluation harness (BUILD_SPEC.md section 6; built in BACKEND_PLAN.md phase 11).

Without it there is no basis for claiming the system works.

Citation fidelity
    Sample 30 draft claims, resolve each anchor, manually verify the passage
    actually supports the claim. Target above 95%.
Abstention
    Remove a source deliberately; confirm affected claims flip to
    ``cannot-verify`` rather than being answered from model knowledge.
Comparison accuracy
    Hand-label 20 claims against known-changed sources, measure agreement.

All three run under pytest as regression tests on every change.

Import it as ``updater.eval`` (or aliased), never as a bare ``eval``, which
would shadow the builtin.
"""

from __future__ import annotations

__all__: list[str] = []
