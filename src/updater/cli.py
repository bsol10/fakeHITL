"""The command line: ``updater ingest``, ``search``, ``extract`` and ``serve``.

Each command is thin: it turns arguments into calls on the rest of the package
and prints the result in a form somebody can check.

``ingest`` refuses a file that is not in the approved-source registry (design
rule 3) rather than trusting the operator to "just index this one file".
``search`` prints an anchor for every hit. ``extract`` prints every verified
quote beside its citation, and the discard count, so each quote can be read
against its source (BACKEND_PLAN.md phase 5); the model comes from ``.env``.
``serve`` runs the API and serves the dashboard from the same origin, keeping
its state in the same database file as the index, so all four commands share
one ``--db``. It needs the ``api`` extra, imported only when it runs.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from updater.citations import render_anchor
from updater.extract import (
    ExtractionResult,
    ModelCallFailed,
    ModelUnavailable,
    extract_evidence,
    model_from_env,
)
from updater.ingest import IngestError
from updater.registry import (
    REGISTRY_PATH_ENV_VAR,
    RegistryError,
    SourceNotRegistered,
    load_registry,
)
from updater.retrieve.embed import EmbeddingUnavailable, SentenceTransformerEmbedder
from updater.retrieve.index import IndexFailure, index_registry
from updater.retrieve.search import (
    DenseRetriever,
    HybridRetriever,
    RetrievalResult,
    Retriever,
    SparseRetriever,
)
from updater.retrieve.store import PassageStore, StoreError, database_path
from updater.retrieve.tokenize import TokenizerUnavailable
from updater.state import StateError

__all__ = ["FRONTEND_DIR", "main"]

LOGGER = logging.getLogger("updater")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "front-end"
"""The dashboard, in a source checkout: ``src/updater/cli.py`` is two levels
below the repository root. An installed wheel has no such directory, and then
``serve`` runs the API alone."""


def _configure_console() -> None:
    """Force UTF-8 on the console.

    The Windows console defaults to cp1252, which mangles the curly quotes and
    dashes in real documents, and mojibake in a printed passage could be the
    extractor's fault or the terminal's.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "database location, shared by the index and the API's state "
            "(default: $UPDATER_DB_PATH, else data/updater.sqlite3)"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log what each stage is doing"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="updater",
        description=(
            "ASIL annual research update agent. Indexes approved sources and "
            "searches them, with every result anchored to a place in a document, "
            "and serves the API the dashboard runs on."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser(
        "ingest",
        help="extract, chunk, embed and index approved sources",
        description=(
            "With no path, indexes every local source in the registry. With "
            "paths, indexes those files — each one must be in the registry."
        ),
    )
    ingest.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files to index (default: everything in the registry)",
    )
    ingest.add_argument(
        "--registry",
        type=Path,
        default=None,
        help=(
            f"registry location (default: ${REGISTRY_PATH_ENV_VAR}, else registry.yaml)"
        ),
    )
    ingest.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "drop the index and build it afresh, rather than replacing the named "
            "sources in it; needed after a change of model, chunk size or schema"
        ),
    )
    _add_common(ingest)

    search = subcommands.add_parser(
        "search",
        help="retrieve passages from the index, with their anchors",
    )
    search.add_argument("query", help="what to look for")
    search.add_argument(
        "-k", "--top-k", type=int, default=5, help="results to show (default: 5)"
    )
    search.add_argument(
        "--chars",
        type=int,
        default=300,
        help="characters of passage text per result; 0 for all (default: 300)",
    )
    search.add_argument(
        "--retriever",
        choices=("hybrid", "dense", "sparse"),
        default="hybrid",
        help=(
            "hybrid fuses the other two; dense and sparse alone are for seeing "
            "why a result came back (default: hybrid)"
        ),
    )
    _add_common(search)

    extract = subcommands.add_parser(
        "extract",
        help="retrieve passages for a claim and extract verified evidence",
        description=(
            "Retrieves passages for the claim, asks the configured model for "
            "evidence, and prints each finding whose quote verified against its "
            "passage, with the citation, plus the count of findings discarded. "
            "The model is UPDATER_MODEL_PROVIDER (anthropic or gemini) with "
            "that provider's key and model id, read from the environment or "
            ".env."
        ),
    )
    extract.add_argument("claim", help="the claim to look for evidence about")
    extract.add_argument(
        "-k",
        "--top-k",
        type=int,
        default=8,
        help="passages to give the model (default: 8)",
    )
    extract.add_argument(
        "--retriever",
        choices=("hybrid", "dense", "sparse"),
        default="hybrid",
        help="how passages are found (default: hybrid)",
    )
    _add_common(extract)

    serve = subcommands.add_parser(
        "serve",
        help="run the HTTP API the dashboard talks to",
        description=(
            "Serves the approved prototype's claims and sources, and keeps what "
            "people do with them (decisions, reviews, the audit log, runs, "
            "settings, source changes) in the database, so a restart loses "
            "nothing. Interactive docs are at /docs. There is no "
            "authentication, so keep it bound to localhost."
        ),
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)"
    )
    serve.add_argument(
        "--port", type=int, default=8000, help="port to listen on (default: 8000)"
    )
    serve.add_argument(
        "--frontend",
        type=Path,
        default=FRONTEND_DIR,
        help=(
            "directory served at / beside the API (default: the repository's "
            "front-end/, when there is one)"
        ),
    )
    serve.add_argument(
        "--no-frontend",
        dest="frontend",
        action="store_const",
        const=None,
        help="serve the API alone",
    )
    _add_common(serve)

    return parser


def _ingest(args: argparse.Namespace) -> int:
    try:
        registry = load_registry(args.registry)
    except RegistryError as error:
        print(f"registry: {error}", file=sys.stderr)
        return 2

    only: set[str] | None = None
    if args.paths:
        try:
            only = {registry.source_for_path(path).id for path in args.paths}
        except SourceNotRegistered as error:
            # Rule 3: the fix is a person adding the document to the registry.
            print(f"refusing to ingest: {error}", file=sys.stderr)
            return 2

    print(f"registry: {len(registry)} source(s) from {registry.path}")

    failures: list[IndexFailure] = []
    try:
        with PassageStore(args.db, rebuild=args.rebuild) as store:
            if args.rebuild:
                print(f"index:    rebuilt {store.path}")
            embedder = SentenceTransformerEmbedder()
            reports, failures = index_registry(
                registry, store=store, embedder=embedder, only=only
            )
            for report in reports:
                print(f"  ok      {report}")
            for failure in failures:
                print(f"  FAILED  {failure}", file=sys.stderr)

            print(
                f"index:    {store.chunk_count()} chunk(s), "
                f"{store.window_count()} vector(s) in {store.path}"
            )
            kinds = store.locator_kinds()
            if kinds:
                summary = ", ".join(f"{kind}: {count}" for kind, count in kinds.items())
                print(f"anchors:  {summary}")
    except (StoreError, EmbeddingUnavailable, TokenizerUnavailable) as error:
        print(f"{error}", file=sys.stderr)
        return 2

    if failures:
        print(
            f"\n{len(failures)} source(s) failed. They are listed above, not "
            f"silently skipped: a source that produced nothing and a source "
            f"that says nothing are different facts.",
            file=sys.stderr,
        )
        return 1
    return 0


def _search(args: argparse.Namespace) -> int:
    try:
        with PassageStore(args.db) as store:
            if store.chunk_count() == 0:
                print(
                    f"{store.path} holds no chunks. Run: updater ingest",
                    file=sys.stderr,
                )
                return 2
            retriever = _retriever(store, args.retriever)
            results = retriever.retrieve(args.query, top_k=args.top_k)

            titles = {
                source_id: store.get_source(source_id).title
                for source_id in store.source_ids()
            }
            print(f'query: "{args.query}"  ({len(results)} result(s))\n')
            for rank, result in enumerate(results, start=1):
                anchor = result.passage.anchor
                citation = render_anchor(anchor, title=titles.get(anchor.source_id))
                text = result.passage.text
                shown = text if args.chars <= 0 else text[: args.chars]
                elided = len(text) - len(shown)
                print(f"{rank}. score {result.score:.4f}  ({citation})")
                print(
                    f"   chunk {result.passage.id}  "
                    f"chars [{anchor.char_start}, {anchor.char_end})  "
                    f"{result.passage.token_count} tokens  "
                    f"via {_how_found(result)}"
                )
                print(f"   {shown}" + (f" ... (+{elided} chars)" if elided else ""))
                print()
    except (StoreError, EmbeddingUnavailable, TokenizerUnavailable) as error:
        print(f"{error}", file=sys.stderr)
        return 2
    except IngestError as error:  # pragma: no cover - search does not ingest
        print(f"{error}", file=sys.stderr)
        return 2
    return 0


def _extract(args: argparse.Namespace) -> int:
    load_dotenv(find_dotenv(usecwd=True))  # never overrides a variable already set
    try:
        model = model_from_env()
    except ModelUnavailable as error:
        print(f"no model: {error}", file=sys.stderr)
        return 2

    try:
        with PassageStore(args.db) as store:
            if store.chunk_count() == 0:
                print(
                    f"{store.path} holds no chunks. Run: updater ingest",
                    file=sys.stderr,
                )
                return 2
            results = _retriever(store, args.retriever).retrieve(
                args.claim, top_k=args.top_k
            )
            titles = {
                source_id: store.get_source(source_id).title
                for source_id in store.source_ids()
            }
    except (StoreError, EmbeddingUnavailable, TokenizerUnavailable) as error:
        print(f"{error}", file=sys.stderr)
        return 2

    print(f'claim: "{args.claim}"')
    print(f"model: {model.provider} {model.model}, over {len(results)} passage(s)\n")
    try:
        extraction = extract_evidence(
            args.claim, [result.passage for result in results], model=model
        )
    except ModelCallFailed as error:
        print(f"extraction failed: {error}", file=sys.stderr)
        return 1
    _print_extraction(extraction, titles)
    return 0


def _print_extraction(result: ExtractionResult, titles: dict[str, str]) -> None:
    if result.model is None:
        print("No passages were retrieved, so no model was asked: cannot verify.")
        return
    print(f"answered by: {result.model}\n")
    if not result.evidence:
        print("No verified evidence in the retrieved passages.\n")
    for number, found in enumerate(result.evidence, start=1):
        evidence = found.evidence
        anchor = evidence.anchor
        match = "exact" if found.exact else "whitespace-flexible"
        print(f"{number}. [{found.type}] {evidence.finding}")
        print(f"   ({render_anchor(anchor, title=titles.get(anchor.source_id))})")
        print(
            f"   chunk {found.passage.id}  "
            f"chars [{anchor.char_start}, {anchor.char_end})  quote: {match}"
        )
        print(f'   "{found.quote}"')
        for label, value in (
            ("statistic", evidence.statistic),
            ("as of", evidence.as_of_date),
            ("method", evidence.method),
            ("caveat", evidence.caveat),
        ):
            if value is not None:
                print(f"   {label}: {value}")
        print()
    print(f"discarded: {result.discarded_count}")
    for discarded in result.discarded:
        print(f"  {discarded.reason}: {discarded.detail}")


def _retriever(store: PassageStore, kind: str) -> Retriever:
    if kind == "sparse":
        return SparseRetriever(store)
    dense = DenseRetriever(store)
    if kind == "dense":
        return dense
    return HybridRetriever(dense, SparseRetriever(store))


def _how_found(result: RetrievalResult) -> str:
    """Which retriever ranked this where. A dash means that retriever did not
    return the chunk at all, which is dense's usual answer for a figure."""
    dense = (
        "-"
        if result.dense_rank is None
        else f"#{result.dense_rank} (window {result.window_ordinal})"
    )
    sparse = "-" if result.sparse_rank is None else f"#{result.sparse_rank}"
    if result.retriever == DenseRetriever.name:
        return f"dense {dense}"
    if result.retriever == SparseRetriever.name:
        return f"sparse {sparse}"
    return f"{result.retriever}: dense {dense}, sparse {sparse}"


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from updater.api import create_app
        from updater.api.mock import MockState
    except ImportError as error:
        print(
            f"the API needs its optional dependencies ({error.name} is missing). "
            "Run: uv sync --extra api",
            file=sys.stderr,
        )
        return 2
    path = database_path(args.db)
    try:
        state = MockState(path=path)
    except StateError as error:
        print(f"{error}", file=sys.stderr)
        return 2
    frontend: Path | None = args.frontend
    if frontend is not None and not (frontend / "index.html").is_file():
        if args.frontend != FRONTEND_DIR:
            print(f"no index.html in {frontend}", file=sys.stderr)
            state.close()
            return 2
        frontend = None  # an installed package: the default checkout path is absent
    print(f"state:    {path}")
    print(
        f"dashboard: http://{args.host}:{args.port}/"
        if frontend is not None
        else "dashboard: not served (API only)"
    )
    print(f"api docs: http://{args.host}:{args.port}/docs")
    try:
        uvicorn.run(
            create_app(state, frontend=frontend), host=args.host, port=args.port
        )
    finally:
        state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "serve":
        return _serve(args)
    if args.command == "extract":
        return _extract(args)
    return _search(args)


if __name__ == "__main__":
    raise SystemExit(main())
