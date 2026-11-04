"""Invariants over anchors that every stage after ingest has to preserve.

BUILD_SPEC.md section 4: chunk within locator boundaries, never across them.
Nothing in the type system enforces that. A :class:`~updater.models.Passage`
assembled from two pages' text is well formed and carries a citation that is a
lie nobody catches by reading the draft. So the rule is a predicate here, and
every stage that derives an anchor from another (chunking, evidence
verification) calls :func:`require_derives_within`.

Ingest satisfies the rule by construction: one passage per locator unit, and no
code path that concatenates two units.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from updater.models import SourceAnchor

__all__ = [
    "ProvenanceError",
    "derives_within",
    "require_derives_within",
    "same_locator_unit",
]


class ProvenanceError(Exception):
    """A derived anchor does not sit inside the anchor it claims to come from."""


def same_locator_unit(parent: SourceAnchor, child: SourceAnchor) -> bool:
    """True when both anchors address the same unit of the same source.

    Locators compare by value, and different variants never compare equal:
    ``PageLocator(page=2)`` and ``BlockLocator(paragraph_index=2, ...)`` are
    different units.
    """
    return parent.source_id == child.source_id and parent.locator == child.locator


def derives_within(parent: SourceAnchor, child: SourceAnchor) -> bool:
    """True when ``child`` narrows ``parent`` rather than straddling it."""
    return (
        same_locator_unit(parent, child)
        and parent.char_start <= child.char_start
        and child.char_end <= parent.char_end
    )


def require_derives_within(parent: SourceAnchor, child: SourceAnchor) -> None:
    """Raise :class:`ProvenanceError` unless ``child`` narrows ``parent``.

    Call this from any stage that splits, merges or re-anchors passages. The
    silent alternative is a draft whose citations point at the wrong page.
    """
    if derives_within(parent, child):
        return

    if not same_locator_unit(parent, child):
        detail = (
            f"different locator unit — parent is {parent.source_id}/"
            f"{parent.locator}, child is {child.source_id}/{child.locator}"
        )
    else:
        detail = (
            f"span [{child.char_start}, {child.char_end}) escapes the parent's "
            f"[{parent.char_start}, {parent.char_end})"
        )
    raise ProvenanceError(f"derived anchor is not inside its parent: {detail}")
