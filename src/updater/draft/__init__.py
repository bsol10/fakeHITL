"""Markdown draft assembly (BUILD_SPEC.md section 4, ``draft``).

Every material sentence carries an inline citation resolving to a
:class:`updater.models.SourceAnchor`, built on
:func:`updater.citations.render_anchor`: "(Source title, p. 14)" for pages,
"(Source title, Sheet1!B4:B12)" for cells. Design rule 4: a draft sentence
without an anchor is a bug, and a linter in this package fails the build over it.

Claims marked ``cannot-verify`` go in a clearly flagged section, never silently
dropped: a missing finding is more dangerous than a flagged one.

Output is always a draft. Design rule 5: nothing here publishes.
"""

from __future__ import annotations

__all__: list[str] = []
