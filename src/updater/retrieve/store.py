"""The index: chunks, their anchors and their vectors, in one SQLite file.

No vector database service (BUILD_SPEC.md section 4): ``sqlite-vec`` for
nearest neighbours, FTS5 for full text, and the anchors beside their text.

*Anchors are columns, not a blob.* The locator stays JSON (its ``kind`` tag
exists for this round trip), but ``source_id``, ``char_start``, ``char_end`` and
``extracted_at`` are real columns, so citations can be sampled and audited by
query (BUILD_SPEC.md section 6).

*A source is re-indexed atomically, never appended to.* A stale copy left
behind would be retrievable beside the current one, under the same anchor with
different text.

*The index records the model and chunk settings it was built with* and refuses
to mix them.

*Full text is kept in step by triggers.* ``chunks_fts`` is an external-content
FTS5 table over ``chunks.text``, written by triggers in whatever transaction
changes ``chunks``, so no code path can update one index and forget the other.
``chunks`` declares ``rowid INTEGER PRIMARY KEY`` so that ``VACUUM`` cannot
renumber the rowids ``chunks_fts`` points at.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from updater.models import Passage, Source, UpdateFrequency
from updater.retrieve.chunk import DEFAULT_CHUNK_CONFIG, ChunkConfig
from updater.retrieve.embed import EMBEDDING_DIMENSIONS
from updater.retrieve.tokenize import EMBEDDING_MODEL_ID
from updater.serde import locator_from_json, locator_to_json, passage_from_row

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from updater.retrieve.embed import EmbeddedWindow

__all__ = [
    "DB_PATH_ENV_VAR",
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "IncompatibleIndex",
    "PassageStore",
    "ScoredChunk",
    "StoreError",
    "database_path",
    "match_expression",
]

SCHEMA_VERSION = 2
"""Bumped when the tables change shape. An older index is rejected, not
migrated: ``updater ingest --rebuild`` rebuilds it from its sources."""

DEFAULT_DB_PATH = Path("data/updater.sqlite3")
"""Matches ``UPDATER_DB_PATH`` in .env.example."""

DB_PATH_ENV_VAR = "UPDATER_DB_PATH"

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
"""Created on its own, ahead of :data:`_SCHEMA`, because the settings in it are
checked before anything else in the file is touched."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    url_or_path       TEXT NOT NULL,
    owner             TEXT NOT NULL,
    topic             TEXT NOT NULL,
    year              INTEGER NOT NULL,
    update_frequency  TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    last_checked      TEXT,
    last_content_hash TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    rowid        INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    source_id    TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    token_count  INTEGER NOT NULL,
    locator_kind TEXT NOT NULL,
    locator      TEXT NOT NULL,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    extracted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_by_source ON chunks(source_id, ordinal);

CREATE TABLE IF NOT EXISTS windows (
    rowid      INTEGER PRIMARY KEY,
    chunk_id   TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS windows_by_chunk ON windows(chunk_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""

_INDEX_TABLES = ("window_vectors", "windows", "chunks_fts", "chunks", "sources", "meta")
"""Every table the index owns, children before parents. The file also holds the
API's state, so a rebuild drops these by name and nothing else. Never add a
state table here: ``DROP TABLE`` bypasses the audit log's append-only triggers."""


class StoreError(Exception):
    """The index could not be opened, written or read."""


class IncompatibleIndex(StoreError):
    """The index was built with different settings from the ones now in force.

    Rebuild it rather than adding to it: ``updater ingest --rebuild``.
    """


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """One retrieved chunk and how well it matched.

    ``score`` is cosine similarity in [-1, 1] from :meth:`PassageStore.search`,
    and BM25 negated so higher is better from :meth:`PassageStore.search_text`.
    The two scales have nothing in common: compare ranks, never scores.
    """

    passage: Passage
    score: float
    window_ordinal: int = 0
    """Which window of the chunk matched best. Text search matches the whole
    chunk and leaves it at 0."""


def database_path(path: Path | None = None) -> Path:
    """The index location: the argument, the environment, then the default."""
    if path is not None:
        return path
    override = os.environ.get(DB_PATH_ENV_VAR)
    return Path(override) if override else DEFAULT_DB_PATH


def match_expression(query: str) -> str | None:
    """The query as an FTS5 expression that cannot be a syntax error.

    Each whitespace-separated word becomes a quoted FTS5 string, joined with
    ``OR`` so a chunk need not contain every word; BM25 ranks chunks with more,
    and rarer, words first.

    Quoting makes FTS5's operators (``AND``, ``OR``, ``NOT``, ``NEAR``, ``*``,
    ``^``, ``:``) plain words. And because the tokenizer splits ``109,860`` at
    the comma, the quoted word is the *phrase* ``109 860``, which matches
    ``$109,860`` and not a row with 109 in one column and 860 in another.

    A word with no letter or digit would quote to an empty string, and is
    dropped. NUL ends an FTS5 string early, so it is a separator here. Returns
    None when nothing searchable is left.
    """
    words = [
        word
        for word in query.replace("\x00", " ").split()
        if any(character.isalnum() for character in word)
    ]
    if not words:
        return None
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


def _pack(vector: Sequence[float]) -> bytes:
    """A vector in the little-endian float32 layout vec0 expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


class PassageStore:
    """Read/write access to the chunk index. Use it as a context manager."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        dimensions: int = EMBEDDING_DIMENSIONS,
        model_id: str = EMBEDDING_MODEL_ID,
        config: ChunkConfig = DEFAULT_CHUNK_CONFIG,
        rebuild: bool = False,
    ) -> None:
        """Open the index, creating it if the file holds none.

        ``rebuild`` drops the index's tables first, so it also opens an index
        built with other settings or an older schema. The API's state in the
        same file is left alone.

        Raises:
            IncompatibleIndex: the file holds an index built differently, and
                ``rebuild`` was not asked for. The file is left untouched.
        """
        self.path = database_path(path)
        self.dimensions = dimensions
        self.model_id = model_id
        self.config = config
        self._connection = self._connect()
        try:
            if rebuild:
                self._drop_index()
            self._check_settings()
            self._create_schema()
        except BaseException:
            self._connection.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        try:
            connection.enable_load_extension(True)
        except AttributeError as error:  # pragma: no cover - build dependent
            raise StoreError(
                "this Python's sqlite3 was built without extension loading, so "
                "sqlite-vec cannot be used. A python.org or uv-managed "
                "interpreter has it enabled."
            ) from error
        try:
            import sqlite_vec
        except ImportError as error:  # pragma: no cover - depends on the install
            raise StoreError(
                "sqlite-vec is not installed. Install the extra: "
                "uv sync --extra retrieve"
            ) from error
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _drop_index(self) -> None:
        """Drop every table the index owns, in one transaction."""
        with self._connection:
            self._connection.execute("BEGIN")
            for table in _INDEX_TABLES:
                self._connection.execute(f"DROP TABLE IF EXISTS {table}")

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS window_vectors USING vec0("
                f"embedding float[{self.dimensions}] distance_metric=cosine)"
            )

    def _settings(self) -> dict[str, str]:
        return {
            "schema_version": str(SCHEMA_VERSION),
            "embedding_model": self.model_id,
            "dimensions": str(self.dimensions),
            "chunk": json.dumps(
                {
                    "max_tokens": self.config.max_tokens,
                    "min_tokens": self.config.min_tokens,
                    "overlap_ratio": self.config.overlap_ratio,
                },
                sort_keys=True,
            ),
        }

    def _check_settings(self) -> None:
        """Refuse to add to an index built differently from this one.

        Vectors from two models, or chunks cut to two budgets, in one table give
        retrieval that is quietly worse rather than obviously broken. Runs
        before any other table is created, so a refused file is left exactly as
        it was found.
        """
        wanted = self._settings()
        with self._connection:
            self._connection.execute(_META_SCHEMA)
        stored = {
            row["key"]: row["value"]
            for row in self._connection.execute("SELECT key, value FROM meta")
        }
        if not stored:
            with self._connection:
                self._connection.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    sorted(wanted.items()),
                )
            return

        differences = [
            f"{key}: index has {stored.get(key)!r}, this run wants {value!r}"
            for key, value in sorted(wanted.items())
            if stored.get(key) != value
        ]
        if differences:
            raise IncompatibleIndex(
                f"{self.path} was built with different settings:\n  "
                + "\n  ".join(differences)
                + "\nRebuild it: updater ingest --rebuild"
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def upsert_source(self, source: Source) -> None:
        """Record a registry entry. Re-registering updates it in place."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sources (id, title, url_or_path, owner, topic, year,
                                     update_frequency, media_type, last_checked,
                                     last_content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    url_or_path = excluded.url_or_path,
                    owner = excluded.owner,
                    topic = excluded.topic,
                    year = excluded.year,
                    update_frequency = excluded.update_frequency,
                    media_type = excluded.media_type,
                    last_checked = excluded.last_checked,
                    last_content_hash = excluded.last_content_hash
                """,
                (
                    source.id,
                    source.title,
                    source.url_or_path,
                    source.owner,
                    source.topic,
                    source.year,
                    str(source.update_frequency),
                    source.media_type,
                    source.last_checked.isoformat() if source.last_checked else None,
                    source.last_content_hash,
                ),
            )

    def replace_source(
        self,
        source: Source,
        chunks: Sequence[Passage],
        windows: Iterable[EmbeddedWindow],
    ) -> int:
        """Index one source's chunks, replacing anything already stored for it.

        One transaction: an interrupted run leaves the previous index intact.
        """
        by_id = {chunk.id: chunk for chunk in chunks}
        window_list = list(windows)
        unknown = {window.chunk_id for window in window_list} - set(by_id)
        if unknown:
            raise StoreError(
                f"{len(unknown)} window(s) reference chunks that were not "
                f"supplied, e.g. {sorted(unknown)[0]!r}. Vectors and chunks are "
                f"matched by id; storing these would orphan them."
            )
        foreign = [chunk.id for chunk in chunks if chunk.source_id != source.id]
        if foreign:
            raise StoreError(
                f"chunk {foreign[0]!r} belongs to source "
                f"{by_id[foreign[0]].source_id!r}, not {source.id!r}"
            )

        self.upsert_source(source)
        with self._connection:
            self._delete_source_rows(source.id)
            self._connection.executemany(
                """
                INSERT INTO chunks (id, source_id, ordinal, text, token_count,
                                    locator_kind, locator, char_start, char_end,
                                    extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.source_id,
                        ordinal,
                        chunk.text,
                        chunk.token_count,
                        chunk.anchor.locator.kind,
                        locator_to_json(chunk.anchor.locator),
                        chunk.anchor.char_start,
                        chunk.anchor.char_end,
                        chunk.anchor.extracted_at.isoformat(),
                    )
                    for ordinal, chunk in enumerate(chunks)
                ],
            )
            for window in window_list:
                cursor = self._connection.execute(
                    """
                    INSERT INTO windows (chunk_id, ordinal, char_start, char_end)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        window.chunk_id,
                        window.ordinal,
                        window.char_start,
                        window.char_end,
                    ),
                )
                self._connection.execute(
                    "INSERT INTO window_vectors(rowid, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, _pack(window.vector)),
                )
        return len(chunks)

    def _delete_source_rows(self, source_id: str) -> None:
        """Drop a source's chunks, windows and vectors.

        The vec0 table is virtual, so ``ON DELETE CASCADE`` does not reach it:
        its rows are deleted by hand, first. Otherwise its vectors outlive their
        windows and a re-indexed source can silently retrieve nothing.
        """
        rowids = [
            row["rowid"]
            for row in self._connection.execute(
                """
                SELECT w.rowid AS rowid FROM windows w
                JOIN chunks c ON c.id = w.chunk_id
                WHERE c.source_id = ?
                """,
                (source_id,),
            )
        ]
        self._connection.executemany(
            "DELETE FROM window_vectors WHERE rowid = ?", [(r,) for r in rowids]
        )
        self._connection.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))

    def source_ids(self) -> list[str]:
        return [
            row["id"]
            for row in self._connection.execute("SELECT id FROM sources ORDER BY id")
        ]

    def get_source(self, source_id: str) -> Source:
        row = self._connection.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no source {source_id!r} in {self.path}")
        return Source(
            id=row["id"],
            title=row["title"],
            url_or_path=row["url_or_path"],
            owner=row["owner"],
            topic=row["topic"],
            year=row["year"],
            update_frequency=UpdateFrequency(row["update_frequency"]),
            media_type=row["media_type"],
            last_checked=None,
            last_content_hash=row["last_content_hash"],
        )

    def chunk_count(self, source_id: str | None = None) -> int:
        if source_id is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM chunks"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (source_id,)
            ).fetchone()
        return int(row["n"])

    def window_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM windows").fetchone()
        return int(row["n"])

    def chunks(self, source_id: str | None = None) -> list[Passage]:
        """Every stored chunk, in the order it was indexed."""
        if source_id is None:
            rows = self._connection.execute(
                "SELECT * FROM chunks ORDER BY source_id, ordinal"
            )
        else:
            rows = self._connection.execute(
                "SELECT * FROM chunks WHERE source_id = ? ORDER BY ordinal",
                (source_id,),
            )
        return [self._passage(row) for row in rows]

    def get_chunk(self, chunk_id: str) -> Passage:
        row = self._connection.execute(
            "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no chunk {chunk_id!r} in {self.path}")
        return self._passage(row)

    @staticmethod
    def _passage(row: sqlite3.Row) -> Passage:
        return passage_from_row(
            passage_id=row["id"],
            source_id=row["source_id"],
            text=row["text"],
            token_count=row["token_count"],
            locator_json=row["locator"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            extracted_at=row["extracted_at"],
        )

    def locator_kinds(self) -> dict[str, int]:
        """How many chunks carry each locator kind — a one-line index summary."""
        return {
            row["locator_kind"]: int(row["n"])
            for row in self._connection.execute(
                "SELECT locator_kind, COUNT(*) AS n FROM chunks "
                "GROUP BY locator_kind ORDER BY locator_kind"
            )
        }

    def search(
        self, vector: Sequence[float], *, top_k: int = 10, overfetch: int = 5
    ) -> list[ScoredChunk]:
        """Nearest chunks to a query vector, best window wins.

        The search runs over *windows*, several of which can belong to one
        chunk, so it fetches ``top_k * overfetch`` windows and collapses them to
        chunks by best score. Without the over-fetch, a query whose top hits all
        came from one long chunk would return a single result.
        """
        if len(vector) != self.dimensions:
            raise StoreError(
                f"query vector has {len(vector)} dimensions, index has "
                f"{self.dimensions}"
            )
        rows = self._connection.execute(
            """
            SELECT w.chunk_id AS chunk_id, w.ordinal AS ordinal, v.distance AS distance
            FROM window_vectors v
            JOIN windows w ON w.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (_pack(vector), max(1, top_k * overfetch)),
        ).fetchall()

        best: dict[str, tuple[float, int]] = {}
        for row in rows:
            chunk_id = row["chunk_id"]
            distance = float(row["distance"])
            if chunk_id not in best or distance < best[chunk_id][0]:
                best[chunk_id] = (distance, int(row["ordinal"]))

        ranked = sorted(best.items(), key=lambda item: item[1][0])[:top_k]
        return [
            ScoredChunk(
                passage=self.get_chunk(chunk_id),
                score=1.0 - distance,
                window_ordinal=ordinal,
            )
            for chunk_id, (distance, ordinal) in ranked
        ]

    def search_text(self, query: str, *, top_k: int = 10) -> list[ScoredChunk]:
        """Chunks containing the query's words, best BM25 first.

        The query is user text, never FTS5 syntax (see :func:`match_expression`).
        Equal scores are ordered by chunk id, so the order is deterministic.
        """
        expression = match_expression(query)
        if expression is None:
            return []
        rows = self._connection.execute(
            """
            SELECT c.*, bm25(chunks_fts) AS bm25
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY bm25, c.id
            LIMIT ?
            """,
            (expression, max(0, top_k)),
        ).fetchall()
        return [
            ScoredChunk(passage=self._passage(row), score=-float(row["bm25"]))
            for row in rows
        ]

    def locator_of(self, chunk_id: str) -> object:
        """The stored locator for a chunk, without rebuilding the whole passage."""
        row = self._connection.execute(
            "SELECT locator FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no chunk {chunk_id!r} in {self.path}")
        return locator_from_json(row["locator"])
