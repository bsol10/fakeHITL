"""Reading and validating the approved-source registry.

Design rule 3 lives here: *if it isn't in the registry, it isn't evidence.* The
rule is only as good as the boundary that enforces it, so this module provides
that boundary as a function — :meth:`Registry.source_for_path` — rather than as
a convention that each caller is trusted to remember. A file the registry does
not list cannot be turned into a :class:`~updater.models.Source`, and without a
``Source`` nothing can be ingested, chunked, retrieved or cited.

The one sanctioned exception is ``scripts/dump_passages.py``, which fabricates a
provisional ``Source`` so a developer can eyeball extraction on a file before it
is approved. Nothing that script prints becomes evidence. Do not add a second
exception.

Validation is deliberately unforgiving. A registry is a small hand-edited file
that is read once per run, so there is no cost to rejecting it loudly, and the
alternative is the failure mode this project keeps running into: a typo that
silently drops a field, surfacing months later as an absence of evidence that
the compare stage reads as a finding about the world.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from updater.models import Source, UpdateFrequency

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "REGISTRY_PATH_ENV_VAR",
    "MalformedRegistry",
    "Registry",
    "RegistryError",
    "RegistryNotFound",
    "SourceNotRegistered",
    "load_registry",
    "registry_path",
]

DEFAULT_REGISTRY_PATH = Path("registry.yaml")
"""Where the registry lives unless :data:`REGISTRY_PATH_ENV_VAR` says otherwise."""

REGISTRY_PATH_ENV_VAR = "UPDATER_REGISTRY_PATH"
"""Environment variable overriding the registry location."""

_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "title",
        "url_or_path",
        "owner",
        "topic",
        "year",
        "update_frequency",
        "media_type",
    }
)

_OPTIONAL_FIELDS = frozenset({"last_checked", "last_content_hash", "notes"})
"""``notes`` is accepted and ignored: a place for a human to say why a source is
on the list, which is exactly the kind of thing a strict loader would otherwise
push people into writing as a YAML comment nobody parses."""


class RegistryError(Exception):
    """Base class for every registry failure."""


class RegistryNotFound(RegistryError):
    """No registry file at the configured path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"no approved-source registry at {path}. Create it, or set "
            f"{REGISTRY_PATH_ENV_VAR} to point at one. Design rule 3: if a "
            f"document is not in the registry, it is not evidence."
        )


class MalformedRegistry(RegistryError):
    """The registry file exists but cannot be trusted to mean what it says."""

    def __init__(self, path: Path, problem: str, *, entry: str | None = None) -> None:
        self.path = path
        self.problem = problem
        self.entry = entry
        where = f"{path}" if entry is None else f"{path}, {entry}"
        super().__init__(f"{where}: {problem}")


class SourceNotRegistered(RegistryError):
    """Something asked for a source the approved list does not contain.

    This is design rule 3 refusing, and it is not an error to work around: the
    fix is to add the document to the registry, which is a decision a human
    makes, not one the pipeline makes for itself.
    """

    def __init__(self, *, wanted: str, registry: Registry) -> None:
        self.wanted = wanted
        self.registry_path = registry.path
        known = ", ".join(source.id for source in registry) or "none"
        super().__init__(
            f"{wanted!r} is not in the approved-source registry at "
            f"{registry.path}. Registered sources: {known}. Add it to the "
            f"registry if ASIL has approved it — nothing outside the registry "
            f"may be cited (design rule 3)."
        )


def _normalised(path: Path) -> str:
    r"""A comparable form of a filesystem path.

    ``normcase`` because this project is developed on Windows, where
    ``Data\Sources\Report.docx`` and ``data/sources/report.docx`` are the same
    file and a case-sensitive comparison would refuse a registered source.
    """
    return os.path.normcase(str(path.resolve()))


@dataclass(frozen=True, slots=True)
class Registry:
    """The approved sources, and the file they were read from.

    Frozen, like the provenance records: the registry is what the run was
    authorised against, and a pipeline that could append to it mid-run would
    make rule 3 unauditable. The fetch stage updates ``last_checked`` on the
    individual :class:`~updater.models.Source` objects, which stay mutable.
    """

    sources: tuple[Source, ...]
    path: Path

    def __iter__(self) -> Iterator[Source]:
        return iter(self.sources)

    def __len__(self) -> int:
        return len(self.sources)

    def __contains__(self, source_id: object) -> bool:
        return any(source.id == source_id for source in self.sources)

    def get(self, source_id: str) -> Source:
        """The registered source with this id, or raise :class:`SourceNotRegistered`."""
        for source in self.sources:
            if source.id == source_id:
                return source
        raise SourceNotRegistered(wanted=source_id, registry=self)

    def resolve_path(self, source: Source) -> Path:
        """The absolute path of a local source.

        Relative paths in the registry resolve against the registry file's own
        directory, so a registry and the documents beside it can be moved or
        checked out anywhere without every entry needing editing.
        """
        candidate = Path(source.url_or_path)
        if candidate.is_absolute():
            return candidate
        return (self.path.parent / candidate).resolve()

    def source_for_path(self, path: Path) -> Source:
        """The registered source stored at ``path``.

        The rule 3 boundary for anything that starts from a file rather than
        from a source id — the CLI, and the ingest stage behind it.
        """
        wanted = _normalised(path)
        for source in self.sources:
            if _normalised(self.resolve_path(source)) == wanted:
                return source
        raise SourceNotRegistered(wanted=str(path), registry=self)

    def local_paths(self) -> list[tuple[Source, Path]]:
        """Every source that names a local file, with its resolved path.

        Sources whose ``url_or_path`` is a URL are skipped: fetching them is the
        next stage's job, and this stage only knows about files on disk.
        """
        return [
            (source, self.resolve_path(source))
            for source in self.sources
            if "://" not in source.url_or_path
        ]


def registry_path(path: Path | None = None) -> Path:
    """Where to read the registry from: the argument, the environment, default."""
    if path is not None:
        return path
    override = os.environ.get(REGISTRY_PATH_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_REGISTRY_PATH


def load_registry(path: Path | None = None) -> Registry:
    """Read, validate and return the approved-source registry.

    Raises:
        RegistryNotFound: no file at the resolved path.
        MalformedRegistry: the file is not a registry, or an entry is not usable.
    """
    resolved = registry_path(path)
    if not resolved.is_file():
        raise RegistryNotFound(resolved)

    try:
        document = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise MalformedRegistry(resolved, f"is not valid YAML: {error}") from error

    entries = _entries_of(document, resolved)
    sources = [
        _source_from(entry, path=resolved, position=position)
        for position, entry in enumerate(entries, start=1)
    ]
    _reject_duplicates(sources, path=resolved)
    return Registry(sources=tuple(sources), path=resolved)


def _entries_of(document: Any, path: Path) -> Sequence[Any]:
    """The ``sources:`` list, or a clear complaint about what was found instead."""
    if document is None:
        raise MalformedRegistry(path, "is empty; it must contain a 'sources' list")
    if not isinstance(document, dict):
        raise MalformedRegistry(
            path,
            f"must be a mapping with a 'sources' key, not a {type(document).__name__}",
        )
    unknown = set(document) - {"sources"}
    if unknown:
        raise MalformedRegistry(
            path, f"has unknown top-level key(s): {', '.join(sorted(unknown))}"
        )
    entries = document.get("sources")
    if entries is None:
        raise MalformedRegistry(path, "has no 'sources' key")
    if not isinstance(entries, list):
        raise MalformedRegistry(
            path, f"'sources' must be a list, not a {type(entries).__name__}"
        )
    if not entries:
        raise MalformedRegistry(
            path,
            "lists no sources. An empty registry means nothing may be cited "
            "(design rule 3), which is almost certainly not what was meant.",
        )
    return entries


def _source_from(entry: Any, *, path: Path, position: int) -> Source:
    """One YAML mapping to one :class:`~updater.models.Source`."""
    where = f"entry {position}"
    if not isinstance(entry, dict):
        raise MalformedRegistry(
            path, f"is a {type(entry).__name__}, not a mapping", entry=where
        )

    identifier = entry.get("id")
    if isinstance(identifier, str) and identifier:
        where = f"entry {position} ({identifier})"

    present = set(entry)
    missing = _REQUIRED_FIELDS - present
    unknown = present - _REQUIRED_FIELDS - _OPTIONAL_FIELDS

    # Reported together, not one at a time. A mistyped key produces both at once
    # — 'topics: skills' leaves 'topic' missing — and being told only that
    # 'topic' is missing, while `topics:` sits there in the file, makes the
    # reader hunt for a difference the loader has already spotted.
    problems = []
    if missing:
        problems.append(f"is missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        problems.append(f"has unknown field(s): {', '.join(sorted(unknown))}")
    if problems:
        known = ", ".join(sorted(_REQUIRED_FIELDS | _OPTIONAL_FIELDS))
        raise MalformedRegistry(
            path, f"{' and '.join(problems)}. Known fields: {known}", entry=where
        )

    return Source(
        id=_string(entry, "id", path=path, where=where),
        title=_string(entry, "title", path=path, where=where),
        url_or_path=_string(entry, "url_or_path", path=path, where=where),
        owner=_string(entry, "owner", path=path, where=where),
        topic=_string(entry, "topic", path=path, where=where),
        year=_year(entry, path=path, where=where),
        update_frequency=_update_frequency(entry, path=path, where=where),
        media_type=_string(entry, "media_type", path=path, where=where),
        last_checked=_timestamp(entry, path=path, where=where),
        last_content_hash=_optional_string(
            entry, "last_content_hash", path=path, where=where
        ),
    )


def _string(entry: dict[str, Any], field: str, *, path: Path, where: str) -> str:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        raise MalformedRegistry(
            path, f"{field} must be a non-empty string, got {value!r}", entry=where
        )
    return value.strip()


def _optional_string(
    entry: dict[str, Any], field: str, *, path: Path, where: str
) -> str | None:
    if entry.get(field) is None:
        return None
    return _string(entry, field, path=path, where=where)


def _year(entry: dict[str, Any], *, path: Path, where: str) -> int:
    value = entry["year"]
    # bool is an int in Python, and 'year: yes' is valid YAML for True.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRegistry(
            path, f"year must be a whole number, got {value!r}", entry=where
        )
    return value


def _update_frequency(
    entry: dict[str, Any], *, path: Path, where: str
) -> UpdateFrequency:
    value = entry["update_frequency"]
    if value == "biannual":
        # Accepted once, removed because readers split on what it means. Ask
        # rather than pick one: a wrong guess changes when a source is re-checked.
        raise MalformedRegistry(
            path,
            "update_frequency 'biannual' is ambiguous: write 'half-yearly' for "
            "twice a year or 'biennial' for every two years.",
            entry=where,
        )
    try:
        return UpdateFrequency(value)
    except ValueError:
        allowed = ", ".join(member.value for member in UpdateFrequency)
        raise MalformedRegistry(
            path,
            f"update_frequency must be one of: {allowed}. Got {value!r}. Use "
            f"'unknown' rather than guessing a cadence.",
            entry=where,
        ) from None


def _timestamp(entry: dict[str, Any], *, path: Path, where: str) -> datetime | None:
    """``last_checked``, which the fetch stage writes back after a run."""
    value = entry.get("last_checked")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise MalformedRegistry(
        path,
        f"last_checked must be an ISO-8601 timestamp, got {value!r}",
        entry=where,
    )


def _reject_duplicates(sources: Sequence[Source], *, path: Path) -> None:
    """Two entries claiming one id, or one file, make a citation ambiguous."""
    for field, values in (
        ("id", [source.id for source in sources]),
        ("url_or_path", [source.url_or_path for source in sources]),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise MalformedRegistry(
                path,
                f"has more than one entry with the same {field}: "
                f"{', '.join(repr(value) for value in duplicates)}. Every source "
                f"must be uniquely identifiable or its citations are ambiguous.",
            )
