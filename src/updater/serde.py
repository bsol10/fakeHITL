"""Round-tripping provenance records through JSON and SQLite.

The locator union carries a literal ``kind`` tag for exactly this reason
(DESIGN.md, 2026-08-20): once a record is in a database column, ``isinstance``
is not available and the tag is the only thing that says which variant is
written there.

The rule this module exists to enforce is that a round trip is *lossless and
checked*. An anchor that comes back from SQLite with a field quietly defaulted
is no longer provenance — it is a plausible guess about provenance, which is the
same failure as a confabulated statistic wearing a different hat. So every
loader here fails loudly on an unknown ``kind`` or a missing field rather than
substituting a default.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from updater.models import (
    BlockLocator,
    CellLocator,
    DomLocator,
    Locator,
    PageLocator,
    Passage,
    SlideLocator,
    SourceAnchor,
    TimeLocator,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "LOCATOR_KINDS",
    "SerdeError",
    "anchor_from_row",
    "locator_from_dict",
    "locator_from_json",
    "locator_to_dict",
    "locator_to_json",
    "passage_from_row",
]


class SerdeError(ValueError):
    """A record could not be read back as the thing it claims to be."""


_BUILDERS: dict[str, Callable[..., Locator]] = {
    "page": PageLocator,
    "block": BlockLocator,
    "cell": CellLocator,
    "slide": SlideLocator,
    "dom": DomLocator,
    "time": TimeLocator,
}

LOCATOR_KINDS = frozenset(_BUILDERS)
"""Every ``kind`` tag this module can read. One per variant of the union."""


def locator_to_dict(locator: Locator) -> dict[str, Any]:
    """A locator as plain data, ``kind`` tag included."""
    return asdict(locator)


def locator_to_json(locator: Locator) -> str:
    """A locator as a JSON string, for a SQLite text column.

    Keys are sorted so two equal locators serialise identically — which is what
    lets a stored locator be compared, deduplicated or used as a cache key
    without parsing it first.
    """
    return json.dumps(locator_to_dict(locator), sort_keys=True)


def locator_from_dict(data: dict[str, Any]) -> Locator:
    """Rebuild the right locator variant from its ``kind`` tag.

    Raises:
        SerdeError: the tag is missing or unknown, or the payload does not match
            the variant it names.
    """
    payload = dict(data)
    kind = payload.pop("kind", None)
    if kind is None:
        raise SerdeError(f"locator has no 'kind' tag: {data!r}")
    builder = _BUILDERS.get(str(kind))
    if builder is None:
        known = ", ".join(sorted(LOCATOR_KINDS))
        raise SerdeError(f"unknown locator kind {kind!r}; known kinds: {known}")

    if kind == "dom" and "heading_trail" in payload:
        # A tuple survives JSON as a list, and DomLocator's trail must be a
        # tuple or the frozen anchor stops being hashable.
        payload["heading_trail"] = tuple(payload["heading_trail"])

    try:
        return builder(**payload)
    except TypeError as error:
        raise SerdeError(
            f"payload does not match locator kind {kind!r}: {error}"
        ) from error


def locator_from_json(text: str) -> Locator:
    """Read a locator from a JSON string."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SerdeError(f"locator is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise SerdeError(f"locator must be a JSON object, got {type(data).__name__}")
    return locator_from_dict(data)


def anchor_from_row(
    *,
    source_id: str,
    locator_json: str,
    char_start: int,
    char_end: int,
    extracted_at: str,
) -> SourceAnchor:
    """Rebuild a :class:`~updater.models.SourceAnchor` from stored columns."""
    try:
        timestamp = datetime.fromisoformat(extracted_at)
    except ValueError as error:
        raise SerdeError(
            f"extracted_at is not an ISO-8601 timestamp: {extracted_at!r}"
        ) from error
    return SourceAnchor(
        source_id=source_id,
        locator=locator_from_json(locator_json),
        char_start=char_start,
        char_end=char_end,
        extracted_at=timestamp,
    )


def passage_from_row(
    *,
    passage_id: str,
    source_id: str,
    text: str,
    token_count: int,
    locator_json: str,
    char_start: int,
    char_end: int,
    extracted_at: str,
) -> Passage:
    """Rebuild a :class:`~updater.models.Passage` from stored columns."""
    return Passage(
        id=passage_id,
        source_id=source_id,
        text=text,
        anchor=anchor_from_row(
            source_id=source_id,
            locator_json=locator_json,
            char_start=char_start,
            char_end=char_end,
            extracted_at=extracted_at,
        ),
        token_count=token_count,
    )
