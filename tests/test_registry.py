"""The approved-source registry, and the boundary it draws.

Design rule 3 is the one rule with no type to enforce it: a ``Source`` is a
plain dataclass and anything can build one. What makes the rule real is that the
pipeline only ever *gets* a ``Source`` from here, and that this module refuses to
produce one for a document nobody approved. These tests are that refusal.

The validation tests look pedantic for a hand-edited YAML file. They are not:
every one of them is a way the file can be wrong while still parsing, and a
registry that parses into the wrong sources fails silently — the run indexes
what it was given and says nothing about what it was not.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from conftest import registry_entry, write_registry
from updater.models import UpdateFrequency
from updater.registry import (
    REGISTRY_PATH_ENV_VAR,
    MalformedRegistry,
    RegistryNotFound,
    SourceNotRegistered,
    load_registry,
    registry_path,
)


def test_a_valid_registry_loads_into_sources(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "registry.yaml",
        [
            registry_entry(),
            registry_entry(id="ons-labour-market", url_or_path="lm.xlsx"),
        ],
    )

    registry = load_registry(path)

    assert len(registry) == 2
    source = registry.get("dfe-destinations-2025")
    assert source.title == "Destinations of KS4 and KS5 students"
    assert source.owner == "Department for Education"
    assert source.update_frequency is UpdateFrequency.ANNUAL
    assert source.year == 2025


def test_optional_fields_round_trip(tmp_path: Path) -> None:
    """``last_checked`` and ``last_content_hash`` are written back by the fetch
    stage, so the loader has to be able to read what it will later write."""
    path = write_registry(
        tmp_path / "registry.yaml",
        [
            registry_entry(
                last_checked="2026-09-01T09:30:00+00:00",
                last_content_hash="a" * 64,
                notes="a human's reason for approving this",
            )
        ],
    )

    source = load_registry(path).get("dfe-destinations-2025")

    assert source.last_checked == datetime.fromisoformat("2026-09-01T09:30:00+00:00")
    assert source.last_content_hash == "a" * 64


def test_a_missing_registry_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(RegistryNotFound) as caught:
        load_registry(tmp_path / "nope.yaml")

    assert "design rule 3" in str(caught.value).lower()


def test_the_environment_can_move_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_PATH_ENV_VAR, str(tmp_path / "elsewhere.yaml"))

    assert registry_path() == tmp_path / "elsewhere.yaml"
    assert registry_path(Path("explicit.yaml")) == Path("explicit.yaml")


# --------------------------------------------------------------------------- #
# Validation — every one of these parses as YAML and means the wrong thing
# --------------------------------------------------------------------------- #


def test_a_mistyped_field_name_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """The silent-loss failure this project keeps meeting.

    ``topics:`` instead of ``topic:`` leaves the real field unset. A lenient
    loader would produce a source with an empty topic and no complaint.
    """
    entry = registry_entry()
    entry["topics"] = entry.pop("topic")
    path = write_registry(tmp_path / "registry.yaml", [entry])

    with pytest.raises(MalformedRegistry) as caught:
        load_registry(path)

    message = str(caught.value)
    assert "topics" in message
    assert "dfe-destinations-2025" in message, "the entry should be named"


def test_a_missing_required_field_names_it(tmp_path: Path) -> None:
    entry = registry_entry()
    del entry["owner"]
    path = write_registry(tmp_path / "registry.yaml", [entry])

    with pytest.raises(MalformedRegistry, match="owner"):
        load_registry(path)


def test_an_invalid_update_frequency_lists_the_valid_ones(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "registry.yaml", [registry_entry(update_frequency="yearly")]
    )

    with pytest.raises(MalformedRegistry) as caught:
        load_registry(path)

    message = str(caught.value)
    assert "annual" in message and "unknown" in message


def test_biannual_is_refused_as_ambiguous(tmp_path: Path) -> None:
    """Twice a year, or every two years? The registry must say which."""
    path = write_registry(
        tmp_path / "registry.yaml", [registry_entry(update_frequency="biannual")]
    )

    with pytest.raises(MalformedRegistry, match="ambiguous") as caught:
        load_registry(path)

    assert "half-yearly" in str(caught.value) and "biennial" in str(caught.value)


@pytest.mark.parametrize(
    ("written", "frequency"),
    [
        ("half-yearly", UpdateFrequency.HALF_YEARLY),
        ("biennial", UpdateFrequency.BIENNIAL),
    ],
)
def test_both_meanings_of_biannual_can_be_written(
    tmp_path: Path, written: str, frequency: UpdateFrequency
) -> None:
    path = write_registry(
        tmp_path / "registry.yaml", [registry_entry(update_frequency=written)]
    )
    [source] = load_registry(path)
    assert source.update_frequency is frequency


def test_year_yes_is_not_a_year(tmp_path: Path) -> None:
    """YAML reads a bare ``yes`` as True, and ``bool`` is an ``int`` in Python.

    Without the explicit check, ``year: yes`` would load as the year 1.
    """
    path = write_registry(tmp_path / "registry.yaml", [registry_entry(year=True)])

    with pytest.raises(MalformedRegistry, match="year"):
        load_registry(path)


def test_two_entries_with_one_id_are_rejected(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "registry.yaml",
        [registry_entry(url_or_path="a.pdf"), registry_entry(url_or_path="b.pdf")],
    )

    with pytest.raises(MalformedRegistry, match="same id"):
        load_registry(path)


def test_two_entries_pointing_at_one_file_are_rejected(tmp_path: Path) -> None:
    """Otherwise ``source_for_path`` has two right answers and picks one."""
    path = write_registry(
        tmp_path / "registry.yaml",
        [registry_entry(), registry_entry(id="a-different-id")],
    )

    with pytest.raises(MalformedRegistry, match="same url_or_path"):
        load_registry(path)


def test_an_empty_registry_is_an_error_not_an_empty_run(tmp_path: Path) -> None:
    (tmp_path / "registry.yaml").write_text("sources: []", encoding="utf-8")

    with pytest.raises(MalformedRegistry, match="lists no sources"):
        load_registry(tmp_path / "registry.yaml")


def test_a_registry_that_is_a_bare_list_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "registry.yaml").write_text("- id: x\n", encoding="utf-8")

    with pytest.raises(MalformedRegistry, match="mapping"):
        load_registry(tmp_path / "registry.yaml")


# --------------------------------------------------------------------------- #
# The rule 3 boundary
# --------------------------------------------------------------------------- #


def test_an_unregistered_file_cannot_be_resolved_to_a_source(tmp_path: Path) -> None:
    path = write_registry(tmp_path / "registry.yaml", [registry_entry()])
    registry = load_registry(path)
    intruder = tmp_path / "leaked-draft.pdf"
    intruder.write_bytes(b"%PDF-1.4")

    with pytest.raises(SourceNotRegistered) as caught:
        registry.source_for_path(intruder)

    message = str(caught.value)
    assert "leaked-draft.pdf" in message
    assert "design rule 3" in message.lower()
    assert "dfe-destinations-2025" in message, "say what *is* approved"


def test_an_unknown_id_cannot_be_resolved_either(tmp_path: Path) -> None:
    registry = load_registry(
        write_registry(tmp_path / "registry.yaml", [registry_entry()])
    )

    with pytest.raises(SourceNotRegistered, match="made-up-source"):
        registry.get("made-up-source")


def test_relative_paths_resolve_against_the_registry_file(tmp_path: Path) -> None:
    """So a checkout can live anywhere without every entry needing editing."""
    documents = tmp_path / "docs"
    documents.mkdir()
    target = documents / "destinations-2025.pdf"
    target.write_bytes(b"%PDF-1.4")
    path = write_registry(
        documents / "registry.yaml",
        [registry_entry(url_or_path="destinations-2025.pdf")],
    )
    registry = load_registry(path)

    assert registry.source_for_path(target).id == "dfe-destinations-2025"
    assert registry.resolve_path(registry.get("dfe-destinations-2025")) == target


def test_path_matching_ignores_case_as_windows_does(tmp_path: Path) -> None:
    """The development machine is Windows, where ``Docs\\A.PDF`` and ``docs/a.pdf``
    are the same file. A case-sensitive comparison would refuse a source that is
    plainly in the registry."""
    documents = tmp_path / "docs"
    documents.mkdir()
    (documents / "Destinations-2025.pdf").write_bytes(b"%PDF-1.4")
    registry = load_registry(
        write_registry(
            documents / "registry.yaml",
            [registry_entry(url_or_path="Destinations-2025.pdf")],
        )
    )

    resolved = registry.source_for_path(documents / "destinations-2025.pdf")

    assert resolved.id == "dfe-destinations-2025"


def test_url_sources_are_not_offered_as_local_files(tmp_path: Path) -> None:
    """The fetch stage downloads those; the index stage only knows about disk."""
    registry = load_registry(
        write_registry(
            tmp_path / "registry.yaml",
            [
                registry_entry(url_or_path="local.pdf"),
                registry_entry(
                    id="web-source", url_or_path="https://example.org/report.html"
                ),
            ],
        )
    )

    ids = [source.id for source, _ in registry.local_paths()]

    assert ids == ["dfe-destinations-2025"]


def test_membership_reads_naturally(tmp_path: Path) -> None:
    registry = load_registry(
        write_registry(tmp_path / "registry.yaml", [registry_entry()])
    )

    assert "dfe-destinations-2025" in registry
    assert "something-else" not in registry


# --------------------------------------------------------------------------- #
# The project's own registry
# --------------------------------------------------------------------------- #


def test_the_projects_registry_is_valid() -> None:
    """The committed registry.yaml has to load, or nothing in the repo runs.

    It is hand-edited YAML listing real client documents; a typo in it is a
    plausible mistake and this is the cheapest place to catch one.
    """
    path = Path(__file__).resolve().parent.parent / "registry.yaml"
    if not path.is_file():
        pytest.skip("no registry.yaml in the repository root")

    registry = load_registry(path)

    assert len(registry) > 0
    for source in registry:
        assert source.owner, f"{source.id} has no owner"
        assert source.topic, f"{source.id} has no topic"
