"""The command line, and the refusal that matters most.

``updater ingest some-file.pdf`` on a file nobody approved must fail, and fail
before anything is read, embedded or stored. That is design rule 3 at the one
place where breaking it would be convenient — the operator has the file right
there, and indexing it is one flag away.

The tests that need an embedding model are not here: they would spend twelve
seconds proving something about torch. The pipeline glue is exercised in
``test_index_pipeline.py`` with a stub embedder instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    SCRIPTED_ANSWERED_BY,
    ScriptedModel,
    evidence_reply,
    finding,
    make_passage,
    make_source,
    registry_entry,
    write_docx,
    write_registry,
)
from updater import cli
from updater.cli import build_parser, main
from updater.models import CellLocator
from updater.retrieve.store import PassageStore


def test_ingest_refuses_a_file_that_is_not_in_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = write_registry(
        tmp_path / "registry.yaml", [registry_entry(url_or_path="approved.docx")]
    )
    write_docx(tmp_path / "approved.docx")
    intruder = write_docx(tmp_path / "leaked-draft.docx")

    exit_code = main(
        [
            "ingest",
            str(intruder),
            "--registry",
            str(registry),
            "--db",
            str(tmp_path / "index.sqlite3"),
        ]
    )

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "refusing to ingest" in error
    assert "leaked-draft.docx" in error
    assert "design rule 3" in error.lower()


def test_ingest_without_a_registry_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["ingest", "--registry", str(tmp_path / "absent.yaml")])

    assert exit_code == 2
    assert "no approved-source registry" in capsys.readouterr().err


def test_search_on_an_empty_index_says_what_to_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["search", "apprenticeship starts", "--db", str(tmp_path / "i.db")]
    )

    assert exit_code == 2
    assert "updater ingest" in capsys.readouterr().err


def test_search_finds_a_figure_by_its_text_without_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Full-text search needs no embedding model, so the index here has chunks
    and no vectors. What is checked is the wiring and that every hit still
    prints the place it came from, and which retriever ranked it where."""
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "index.sqlite3"
    salary = make_passage(
        "Avg Salary Low (AUD): $109,860",
        passage_id="src:A13:D13#0",
        source_id="dfe-destinations-2025",
        locator=CellLocator(sheet="Salary Premiums", cell_range="A13:D13"),
    )
    with PassageStore(db) as store:
        store.replace_source(make_source(), [salary], [])

    exit_code = main(["search", "109,860", "--retriever", "sparse", "--db", str(db)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Salary Premiums" in output
    assert "A13:D13" in output
    assert "via sparse #1" in output


def test_the_parser_offers_both_commands() -> None:
    parser = build_parser()

    ingest = parser.parse_args(["ingest"])
    search = parser.parse_args(["search", "a query", "-k", "3"])

    assert ingest.command == "ingest"
    assert ingest.paths == []
    assert search.command == "search"
    assert search.top_k == 3
    assert search.retriever == "hybrid"


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --------------------------------------------------------------------------- #
# updater extract
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real .env, and its keys, out of these tests."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: False)
    for name in (
        "UPDATER_MODEL_PROVIDER",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("no_dotenv")
def test_extract_without_a_model_says_so_before_reading_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "never-opened.sqlite3"

    exit_code = main(["extract", "a claim", "--db", str(db)])

    assert exit_code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not db.exists(), "a configuration error stops before the index"


@pytest.mark.usefixtures("no_dotenv")
def test_extract_prints_each_verified_quote_and_the_discards(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "index.sqlite3"
    salary = make_passage(
        "Avg Salary Low (AUD): $109,860",
        passage_id="src:A13:D13#0",
        source_id="dfe-destinations-2025",
        locator=CellLocator(sheet="Salary Premiums", cell_range="A13:D13"),
    )
    with PassageStore(db) as store:
        store.replace_source(make_source(), [salary], [])
    model = ScriptedModel(
        evidence_reply(
            finding(passage_id=salary.id, quote="$109,860", statistic="$109,860"),
            finding(passage_id=salary.id, quote="$119,860", statistic="$119,860"),
        )
    )
    monkeypatch.setattr(cli, "model_from_env", lambda: model)

    exit_code = main(["extract", "109,860", "--retriever", "sparse", "--db", str(db)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"answered by: {SCRIPTED_ANSWERED_BY}" in output
    assert "Salary Premiums!A13:D13" in output
    assert '"$109,860"' in output
    assert "discarded: 1" in output
    assert "quote-not-found" in output
    assert "$119,860" not in output.split("discarded:")[0], (
        "a discarded quote must not be printed as evidence"
    )


def test_the_parser_offers_extract() -> None:
    args = build_parser().parse_args(["extract", "a claim"])

    assert args.command == "extract"
    assert args.top_k == 8
    assert args.retriever == "hybrid"
