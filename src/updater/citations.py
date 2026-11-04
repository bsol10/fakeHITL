"""Rendering a locator so a human can go and check it.

A citation has two halves: *which document* and *where in it*. This module owns
the second half, in the form a reviewer would use to find the text by hand
("p. 14", "Sheet1!B4:F4", "paragraph 37"). The draft stage (not built yet) owns
assembling "(Source title, p. 14)" into sentences, and should build on
:func:`render_anchor` so the CLI, the inspector and the draft cannot disagree
about what a citation looks like.

The rule throughout: **address the reader's copy of the document, not ours.**
Page and slide numbers as printed; cell ranges in A1 notation as Excel shows
them; DOCX paragraph indexes counting empty paragraphs too. For HTML the heading
trail comes first, because it survives a publisher restyling the page and the
CSS path does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from updater.models import (
    BlockLocator,
    CellLocator,
    DomLocator,
    PageLocator,
    SlideLocator,
    TimeLocator,
)

if TYPE_CHECKING:
    from updater.models import Locator, SourceAnchor

__all__ = ["render_anchor", "render_locator"]


def render_locator(locator: Locator) -> str:
    """Where in a document, in the terms the document itself uses."""
    match locator:
        case PageLocator(page=page):
            return f"p. {page}"
        case BlockLocator(paragraph_index=index, style=style):
            return f"paragraph {index}" + (f" [{style}]" if style else "")
        case CellLocator(sheet=sheet, cell_range=cell_range):
            return f"{sheet}!{cell_range}"
        case SlideLocator(slide=slide, shape_index=shape):
            return f"slide {slide}, shape {shape}"
        case DomLocator(css_path=css_path, heading_trail=trail):
            return (" > ".join(trail) if trail else "(no heading trail)") + (
                f"  @  {css_path}"
            )
        case TimeLocator(start_sec=start, end_sec=end):
            return f"{start:.1f}s-{end:.1f}s"


def render_anchor(anchor: SourceAnchor, *, title: str | None = None) -> str:
    """A citation: the source, then the place inside it.

    ``title`` is the source title from the registry. Without one the source id
    is used, which is legible enough for a CLI but not for a draft.
    """
    where = render_locator(anchor.locator)
    return f"{title or anchor.source_id}, {where}"
