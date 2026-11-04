"""Ingest real files and print every passage with its full anchor.

A developer inspector, not a pipeline stage. Drop documents in ``data/sources/``
(gitignored: the repo holds no client material) and run::

    uv run python scripts/dump_passages.py data/sources

For one file, with nothing truncated::

    uv run python scripts/dump_passages.py data/sources/report.pdf --limit 0 --chars 0

It shows, on real documents, the three things BUILD_SPEC.md section 3 says go
wrong silently: whether the locator is right, whether the offsets slice back to
the text they claim, and whether the extracted text is readable.

``--chunk`` prints chunks rather than passages. A passage is its whole locator
unit, so the first thing that legitimately reads ``PARTIAL`` is a chunk.

``--worst N`` shows the N lowest by alphanumeric ratio. Wrong column labels are
less prose-like than right ones, so a header bug sinks its passages to the
bottom of this list. Run it first against newly supplied material.

The script fabricates a :class:`~updater.models.Source` from the file itself,
because it exists to look at a document *before* anybody has approved it. That
makes it the one sanctioned exception to the registry: nothing it prints becomes
evidence. Do not import :func:`provisional_source` from anything that retrieves,
extracts evidence, or drafts.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The Windows console defaults to cp1252 and would turn every curly quote and
# dash in the extracted text into a replacement character, hiding whether the
# extractor or the terminal produced it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from updater.citations import render_locator
from updater.ingest import (
    ExtractionFailed,
    IngestError,
    NeedsOCR,
    UnsupportedMediaType,
    extract,
    sniff_media_type,
)
from updater.ingest.quality import alnum_ratio
from updater.models import Passage, Source, UpdateFrequency
from updater.retrieve.chunk import DEFAULT_CHUNK_CONFIG, chunk_passages
from updater.retrieve.tokenize import TokenizerUnavailable, default_token_counter

RULE = "=" * 78
THIN = "-" * 78


def provisional_source(path: Path) -> Source:
    """A throwaway registry entry so ingest has something to anchor against.

    Not the registry (see the module docstring). ``owner`` and ``topic`` read
    as unknown rather than being guessed, so they do not look filled in.
    """
    return Source(
        id=path.stem,
        title=path.stem,
        url_or_path=str(path),
        owner="unknown (not from the registry)",
        topic="unknown (not from the registry)",
        year=datetime.now(UTC).year,
        update_frequency=UpdateFrequency.UNKNOWN,
        media_type=sniff_media_type(path),
    )


def dump_passage(
    passage: Passage,
    *,
    ordinal: int,
    total: int,
    chars: int,
    label: str = "passage",
    unit_chars: int | None = None,
) -> None:
    anchor = passage.anchor
    # Offsets are relative to the locator unit, so a chunk's anchor indexes into
    # its parent's text. Rebasing by char_start makes one slice check work for
    # both a whole passage and a chunk of one.
    span = passage.text[: anchor.char_end - anchor.char_start]

    # "Whole unit" needs the unit's length, which a chunk does not carry, so the
    # caller passes it. char_start == 0 alone would call the first chunk of a
    # split page whole.
    covered = anchor.char_end - anchor.char_start
    unit = unit_chars if unit_chars is not None else covered
    coverage = (
        "whole unit"
        if anchor.char_start == 0 and covered == unit
        else f"PARTIAL — {covered} of the unit's {unit} chars"
    )

    print(THIN)
    print(f"{label} {ordinal}/{total}")
    print(f"  id           {passage.id}")
    print(f"  source_id    {passage.source_id}")
    print(f"  locator      {anchor.locator!r}")
    print(f"  cites as     ({render_locator(anchor.locator)})")
    print(
        f"  offsets      [{anchor.char_start}, {anchor.char_end}) "
        f"= {anchor.char_end - anchor.char_start} chars, {coverage}"
    )
    print(
        f"  token_count  {passage.token_count}   "
        f"alnum ratio  {alnum_ratio(passage.text):.3f}"
    )
    print(f"  extracted_at {anchor.extracted_at.isoformat()}")

    shown = span if chars <= 0 else span[:chars]
    elided = len(span) - len(shown)
    print("  raw text, in [ ] so leading and trailing whitespace is visible:")
    print(f"  [{shown}]")
    if elided:
        print(f"  ... {elided} more characters (raise --chars)")


def chunked(passages: list[Passage]) -> list[Passage]:
    """Run the chunking stage, or explain why it could not run.

    Chunking needs the embedding model's tokenizer, because the budget is
    counted in that model's units. Without it nothing is chunked, rather than
    chunked to some other budget that nothing else would reproduce.
    """
    try:
        counter = default_token_counter()
    except TokenizerUnavailable as error:
        print(f"  CANNOT CHUNK  {error}")
        return []
    return chunk_passages(passages, counter=counter)


def dump_file(path: Path, *, limit: int, chars: int, chunk: bool, worst: int) -> bool:
    """Ingest one file and print it. False if ingestion failed."""
    print()
    print(RULE)
    print(f"FILE  {path}")

    try:
        source = provisional_source(path)
    except OSError as error:
        print(f"  could not read: {error}")
        return False

    print(f"  sniffed as   {source.media_type}")
    print(f"  source.id    {source.id}  (provisional — not from the registry)")

    try:
        passages = extract(path, source)
    except NeedsOCR as error:
        # A work item, not a bug: the file is scanned and OCR is not built yet.
        print(f"  NEEDS OCR    {error}")
        return False
    except UnsupportedMediaType as error:
        print(f"  UNSUPPORTED  {error}")
        return False
    except ExtractionFailed as error:
        # Failing the whole document is deliberate (DESIGN.md decisions log);
        # the message names each passage that failed the gate.
        print(f"  EXTRACTION FAILED  {error}")
        return False
    except IngestError as error:
        print(f"  INGEST ERROR {error}")
        return False

    total = len(passages)
    print(f"  passages     {total}")

    keys = [repr(passage.anchor.locator) for passage in passages]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        # Two passages pointing at one place makes both citations uncheckable.
        print(
            f"  WARNING: {len(duplicates)} locator(s) claimed by more than one passage"
        )

    label = "passage"
    unit_chars = {passage.id: len(passage.text) for passage in passages}
    if chunk:
        chunks = chunked(passages)
        if not chunks:
            return False
        print(
            f"  chunks       {len(chunks)}  (budget "
            f"{DEFAULT_CHUNK_CONFIG.max_tokens} tokens, overlap "
            f"{DEFAULT_CHUNK_CONFIG.overlap_tokens})"
        )
        if len(chunks) == len(passages):
            print("  note         every unit fitted the budget; nothing was split")
        # A chunk's id is its parent's plus "#n".
        unit_chars = {item.id: unit_chars[item.id.rsplit("#", 1)[0]] for item in chunks}
        passages, label, total = chunks, "chunk", len(chunks)

    if worst > 0:
        selected = sorted(passages, key=lambda item: alnum_ratio(item.text))[:worst]
        print(f"  showing      the {len(selected)} lowest by alphanumeric ratio")
        for ordinal, item in enumerate(selected, start=1):
            dump_passage(
                item,
                ordinal=ordinal,
                total=total,
                chars=chars,
                label=label,
                unit_chars=unit_chars.get(item.id),
            )
        return True

    for ordinal, item in enumerate(passages, start=1):
        if 0 < limit < ordinal:
            print(f"\n  ... {total - limit} more {label}s (raise --limit)")
            break
        dump_passage(
            item,
            ordinal=ordinal,
            total=total,
            chars=chars,
            label=label,
            unit_chars=unit_chars.get(item.id),
        )
    return True


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(
        path
        for path in target.rglob("*")
        if path.is_file() and path.name not in {".gitkeep", "README.md"}
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest files and print every passage with its full anchor.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=Path("data/sources"),
        type=Path,
        help="a file, or a directory to walk (default: data/sources)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="passages printed per file; 0 for all (default: 5)",
    )
    parser.add_argument(
        "--chars",
        type=int,
        default=600,
        help="characters of raw text per passage; 0 for all (default: 600)",
    )
    parser.add_argument(
        "--chunk",
        action="store_true",
        help="run the chunking stage and print chunks instead of passages",
    )
    parser.add_argument(
        "--worst",
        type=int,
        default=0,
        help="show only the N with the lowest alphanumeric ratio (the smoke alarm)",
    )
    args = parser.parse_args()

    target: Path = args.target
    if not target.exists():
        print(f"{target} does not exist. Create it and put documents in it.")
        return 2

    paths = collect(target)
    if not paths:
        print(f"No files under {target}. Drop PDFs, .docx or .html files in there.")
        return 2

    failed = [
        path
        for path in paths
        if not dump_file(
            path,
            limit=args.limit,
            chars=args.chars,
            chunk=args.chunk,
            worst=args.worst,
        )
    ]

    print()
    print(RULE)
    print(f"{len(paths) - len(failed)}/{len(paths)} file(s) ingested")
    for path in failed:
        print(f"  failed: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
