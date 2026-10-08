"""Rendering a locator so a human can go and check it.

A citation has two halves: *which document* and *where in it*. This module owns
the second half — turning each locator variant into the form a reviewer would
use to find the text by hand ("p. 14", "Sheet1!B4:F4", "paragraph 37").

BUILD_SPEC.md's draft stage owns the first half and the assembly: "(Source
title, p. 14)", plus the linter that fails a material sentence carrying no
citation at all. It builds on :func:`render_anchor` rather than replacing it, so
there is one place where a page becomes "p. 14" and the CLI, the inspector and
the draft cannot drift apart about what a citation looks like.

The rendering rule throughout: **address the reader's copy of the document, not
ours.** Page and slide numbers are the ones printed on the page; cell ranges are
A1 notation as Excel shows them; a DOCX paragraph index counts blocks including
the empty ones, because that is what a reviewer scrolling the file will land on.
For HTML the heading trail comes first and the CSS path second, because the
trail still reads correctly after a publisher restyles the page and the path
does not — and a year usually passes between two runs.
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

    ``title`` is the human-readable source title from the registry. Without one
    the source id is used, which is legible enough for a CLI and never for a
    draft — the draft stage always has the registry entry to hand.
    """
    where = render_locator(anchor.locator)
    return f"{title or anchor.source_id}, {where}"
