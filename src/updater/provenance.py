"""Invariants over anchors that every stage after ingest has to preserve.

BUILD_SPEC.md section 4 states the rule that keeps citations honest:

    Chunk within locator boundaries, never across them. A chunk must not span
    two PDF pages, or its anchor is ambiguous.

Nothing in the type system can enforce that. A :class:`~updater.models.Passage`
assembled from two pages' text is a perfectly well-formed ``Passage``; it just
carries a citation that is a lie, and a lie of exactly the kind nobody catches
by reading the draft. So the rule is written here as a predicate, and the stages
that derive passages from other passages call it instead of remembering a
paragraph of prose.

Ingest satisfies the rule trivially: one passage per locator unit, and no code
path that concatenates two units (see :mod:`updater.ingest.adapters.pdf`). The
stage that can break it is chunking, which does not exist yet. That is the
reason for writing this now — an invariant added after the code that violates it
already exists is an invariant that gets quietly relaxed to fit.
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

    Locators are frozen dataclasses, so they compare by value, and two different
    variants never compare equal — ``PageLocator(page=2)`` and
    ``BlockLocator(paragraph_index=2, ...)`` are correctly different units even
    though both are "position 2 of something".
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

    Call this from any stage that splits, merges or re-anchors passages. Failing
    loudly here costs a crash in a test run. Failing silently costs a draft full
    of citations that point at the wrong page, which is the failure this whole
    project exists to prevent.
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
