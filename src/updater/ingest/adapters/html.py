"""HTML extraction with boilerplate stripped (BUILD_SPEC.md section 3).

trafilatura decides what is content and returns it as flat XML::

    <doc><main><head rend="h1">Title</head><p>Body</p></main></doc>

One passage per block element under ``<main>``, anchored by
:class:`~updater.models.DomLocator`.

*The heading trail is the durable half of the citation*: it still reads
correctly after the publisher restyles the page. Cite it to humans.

``css_path`` points into trafilatura's extracted tree, not the live page: the
original DOM does not survive boilerplate removal. Treat it as a stable
within-document ordinal.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import trafilatura
from lxml import etree

from updater.ingest.base import build_passage
from updater.ingest.errors import ExtractionFailed
from updater.models import DomLocator, MediaType, Passage, Source

__all__ = ["HtmlExtractor"]

_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

_BLOCK_TAGS = frozenset({"item", "row", "cell", "p", "head", "quote", "list", "table"})
"""trafilatura's block-level vocabulary, used to decide where spaces belong."""

_WHITESPACE = re.compile(r"\s+")


class HtmlExtractor:
    """Content extraction that keeps the heading trail intact."""

    media_types: ClassVar[set[str]] = {MediaType.HTML}

    def extract(self, path: Path, source: Source) -> list[Passage]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        xml = trafilatura.extract(
            raw,
            output_format="xml",
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
        if not xml:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason="trafilatura found no main content in it",
            )

        main = etree.fromstring(xml.encode("utf-8")).find("main")
        if main is None:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason="trafilatura returned no <main> element",
            )

        tree = main.getroottree()
        extracted_at = datetime.now(UTC)
        heading_trail: list[str] = []
        passages: list[Passage] = []

        for element in main:
            text = _element_text(element)
            if not text:
                continue
            if element.tag == "head":
                _push_heading(heading_trail, element, text)

            passages.append(
                build_passage(
                    source=source,
                    locator=DomLocator(
                        css_path=_css_path(str(tree.getpath(element))),
                        heading_trail=tuple(heading_trail),
                    ),
                    text=text,
                    locator_key=f"dom{len(passages)}",
                    extracted_at=extracted_at,
                )
            )

        if not passages:
            raise ExtractionFailed(
                path=path,
                source_id=source.id,
                reason="extracted to no block elements with text in them",
            )
        return passages


def _element_text(element: Any) -> str:
    """Flatten an element to text, putting spaces only between block children.

    ``"".join(itertext())`` runs list items together into "AB"; joining every
    text node with a space instead puts one before the "!" in
    ``<p>Hello <b>world</b>!</p>``. Recursing over block children only gets both
    cases right.
    """
    blocks = [child for child in element if child.tag in _BLOCK_TAGS]
    if blocks:
        text = " ".join(_element_text(child) for child in blocks)
    else:
        text = "".join(element.itertext())
    return _WHITESPACE.sub(" ", text).strip()


def _push_heading(trail: list[str], element: Any, text: str) -> None:
    """Update the running heading trail for a ``<head>`` element.

    A heading includes itself in its own trail. A ``<head>`` with no
    recognisable ``rend`` is treated as one level deeper than the current one.
    """
    level = _HEADING_LEVELS.get(element.get("rend") or "", len(trail) + 1)
    del trail[level - 1 :]
    trail.append(text)


def _css_path(xpath: str) -> str:
    """Rewrite lxml's ``/doc/main/p[2]`` as ``doc > main > p:nth-of-type(2)``."""
    steps: list[str] = []
    for step in xpath.strip("/").split("/"):
        if step.endswith("]"):
            tag, _, index = step[:-1].partition("[")
            steps.append(f"{tag}:nth-of-type({index})")
        else:
            steps.append(step)
    return " > ".join(steps)
