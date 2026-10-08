"""The research update's state: what people and runs create, kept in SQLite.

BACKEND_PLAN.md phase 1. Decisions, reviews, the audit log, scan runs, settings
and the source registry's lifecycle, in the same file as the index
(``UPDATER_DB_PATH``) but in tables of their own, with a schema version of
their own in ``state_meta``. Not in ``meta``: the index treats a ``meta`` table
that holds anything but its own keys as an incompatible index, so sharing it
would make the next ``updater ingest`` refuse to run.

*What is stored, and what is not.* This holds what people and runs create.
Reviewed inputs are read where they live and never copied in: the registry's
base list (the prototype's fixtures while the API is fixture-backed,
``registry.yaml`` from phase 3) and, until phase 6 produces real ones, the
claims. A source's status is an overlay keyed by its id, so nothing here ever
writes the YAML.

Three properties are worth stating because nothing in the schema shows them.

*The audit log is append-only twice over.* :meth:`StateStore.append_audit` is
the only method that writes it, and SQLite triggers abort any ``UPDATE`` or
``DELETE`` on it, so a future method that tried would fail rather than quietly
rewrite history.

*A change and its audit entry are one transaction.* :meth:`StateStore.transaction`
is the unit of work. A decision without its audit entry, or an entry for a
decision that was never saved, cannot exist.

*An incompatible file is refused, never rebuilt.* The index can be rebuilt from
its sources in thirty seconds; the audit log cannot be rebuilt from anything.
A different schema version is refused with the tables left as they are, and the
first migration gets written alongside the first schema change. Likewise state
written against a different set of claims is refused: decisions, reviews and
audit entries name claims by id, and against other claims they would attach to
the wrong ones.

Plain ``sqlite3``, without ``sqlite-vec``: the ``api`` extra does not install
it, and SQLite only needs the module for the index's virtual table, which
nothing here touches. Not thread-safe; callers serialise, as
:class:`updater.api.mock.MockState` does with its lock.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

__all__ = [
    "IN_MEMORY",
    "SCHEMA_VERSION",
    "AuditRecord",
    "IncompatibleState",
    "ProposedSource",
    "ReviewRecord",
    "RunRecord",
    "RunSourceRecord",
    "SourceState",
    "StateError",
    "StateStore",
]

SCHEMA_VERSION = 1
"""Bumped when the tables change shape. An older file is refused, not dropped."""

IN_MEMORY = Path(":memory:")
"""A store that lasts as long as the process. What the API's tests run against."""

_SCHEMA = (
    """
    CREATE TABLE state_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # The registry's lifecycle, as an overlay on a base list kept elsewhere.
    # NULL in url or last_checked means the base list's value stands.
    """
    CREATE TABLE source_status (
        source_id    TEXT PRIMARY KEY,
        status       TEXT NOT NULL,
        url          TEXT,
        last_checked TEXT
    )
    """,
    """
    CREATE TABLE proposed_sources (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        url              TEXT NOT NULL,
        owner            TEXT NOT NULL,
        topic            TEXT NOT NULL,
        format           TEXT NOT NULL,
        update_frequency TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE runs (
        run_id     TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        done       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # A run records the source as it was when scanned: renaming it later does
    # not rewrite what an earlier run says it looked at.
    """
    CREATE TABLE run_sources (
        run_id    TEXT NOT NULL REFERENCES runs(run_id),
        position  INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        name      TEXT NOT NULL,
        format    TEXT NOT NULL,
        state     TEXT NOT NULL,
        detail    TEXT NOT NULL,
        PRIMARY KEY (run_id, position)
    )
    """,
    """
    CREATE TABLE decisions (
        claim_id INTEGER PRIMARY KEY,
        decision TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE reviews (
        claim_id INTEGER PRIMARY KEY,
        verdict  TEXT,
        text     TEXT,
        note     TEXT
    )
    """,
    # AUTOINCREMENT so a sequence number is never reused: seq is the order
    # things happened in, which a timestamp to the second does not settle.
    """
    CREATE TABLE audit (
        seq       INTEGER PRIMARY KEY AUTOINCREMENT,
        t         TEXT NOT NULL,
        who       TEXT NOT NULL,
        action    TEXT NOT NULL,
        claim_id  INTEGER,
        source_id TEXT,
        detail    TEXT
    )
    """,
    """
    CREATE TRIGGER audit_refuses_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END
    """,
    """
    CREATE TRIGGER audit_refuses_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END
    """,
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


class StateError(Exception):
    """The state could not be opened, written or read."""


class IncompatibleState(StateError):
    """The file holds state this code must not read or add to.

    Never resolved by dropping the tables, because one of them is the audit log.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceState:
    """A source's lifecycle, overlaid on its entry in the base list."""

    source_id: str
    status: str
    url: str | None
    """A confirmed new address; ``None`` keeps the base list's."""
    last_checked: datetime | None
    """Written by the fetch stage from phase 3; ``None`` keeps the base list's."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedSource:
    """A source added through the API rather than through a reviewed file."""

    id: str
    name: str
    url: str
    owner: str
    topic: str
    format: str
    update_frequency: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSourceRecord:
    source_id: str
    name: str
    format: str
    state: str
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRecord:
    run_id: str
    started_at: datetime
    done: int
    """How many of ``sources``, from the front, have finished."""
    sources: tuple[RunSourceRecord, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRecord:
    verdict: str | None
    text: str | None
    note: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditRecord:
    """One recorded action. A claim action names its claim; a registry action
    names its source; the sign-off names neither, since it covers the report."""

    seq: int
    t: datetime
    who: str
    action: str
    claim_id: int | None
    source_id: str | None
    detail: str | None


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class StateStore:
    """Read/write access to the state tables. Use it as a context manager."""

    def __init__(self, path: Path, *, claim_set: str) -> None:
        """Open the state in ``path``, creating its tables on first use.

        ``claim_set`` names what the claim ids in decisions, reviews and the
        audit log refer to. A file written against a different one is refused.
        """
        self.path = path
        self.claim_set = claim_set
        if path != IN_MEMORY:
            path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, so the only transactions are the ones transaction() opens.
        self._connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._create_or_check()
        except BaseException:
            self._connection.close()
            raise

    def _create_or_check(self) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state_meta'"
        ).fetchone()
        if exists is None:
            with self.transaction():
                for statement in _SCHEMA:
                    self._connection.execute(statement)
                self._connection.executemany(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("claim_set", self.claim_set),
                    ],
                )
            return

        stored = {
            row["key"]: row["value"]
            for row in self._connection.execute("SELECT key, value FROM state_meta")
        }
        if stored.get("schema_version") != str(SCHEMA_VERSION):
            raise IncompatibleState(
                f"{self.path} holds state at schema version "
                f"{stored.get('schema_version')}, and this code reads version "
                f"{SCHEMA_VERSION}. It has been left as it is: it holds the audit "
                "log, which cannot be rebuilt. It needs a migration, not a reset."
            )
        if stored.get("claim_set") != self.claim_set:
            raise IncompatibleState(
                f"{self.path} holds decisions, reviews and audit entries about "
                f"claims {stored.get('claim_set')!r}, and this server has claims "
                f"{self.claim_set!r}. They name claims by id, so they would attach "
                "to the wrong ones. Move the file aside or point UPDATER_DB_PATH "
                "at a new one; the index rebuilds with: updater ingest"
            )

    # ------------------------------------------------------------- lifecycle

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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One unit of work: everything inside is saved together or not at all.

        Nested use joins the enclosing transaction, so a method that is atomic on
        its own is also atomic as part of something larger. ``IMMEDIATE`` takes
        the write lock up front, rather than discovering half way through that
        ``updater ingest`` holds it.
        """
        if self._connection.in_transaction:
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # ------------------------------------------------- the registry's lifecycle

    def source_states(self) -> dict[str, SourceState]:
        return {
            row["source_id"]: SourceState(
                source_id=row["source_id"],
                status=row["status"],
                url=row["url"],
                last_checked=_timestamp(row["last_checked"]),
            )
            for row in self._connection.execute("SELECT * FROM source_status")
        }

    def set_source_status(
        self, source_id: str, status: str, *, url: str | None = None
    ) -> None:
        """Record a status. A ``url`` confirms a new address; without one, any
        address confirmed earlier stands."""
        self._connection.execute(
            """
            INSERT INTO source_status (source_id, status, url) VALUES (?, ?, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                status = excluded.status,
                url = COALESCE(excluded.url, source_status.url)
            """,
            (source_id, status, url),
        )

    def mark_checked(self, source_id: str, at: datetime) -> None:
        """Record when a source was last fetched. The fetch stage's, from phase 3."""
        updated = self._connection.execute(
            "UPDATE source_status SET last_checked = ? WHERE source_id = ?",
            (at.isoformat(), source_id),
        )
        if updated.rowcount == 0:
            raise StateError(f"no status recorded for source {source_id!r}")

    def proposed_sources(self) -> list[ProposedSource]:
        """In the order they were proposed."""
        return [
            ProposedSource(**dict(row))
            for row in self._connection.execute(
                "SELECT id, name, url, owner, topic, format, update_frequency "
                "FROM proposed_sources ORDER BY rowid"
            )
        ]

    def propose_source(self, source: ProposedSource) -> None:
        self._connection.execute(
            """
            INSERT INTO proposed_sources
                (id, name, url, owner, topic, format, update_frequency)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.name,
                source.url,
                source.owner,
                source.topic,
                source.format,
                source.update_frequency,
            ),
        )

    # ------------------------------------------------------------------ runs

    def run_ids(self) -> list[str]:
        return [
            row["run_id"]
            for row in self._connection.execute(
                "SELECT run_id FROM runs ORDER BY rowid"
            )
        ]

    def run(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        sources = self._connection.execute(
            "SELECT source_id, name, format, state, detail FROM run_sources "
            "WHERE run_id = ? ORDER BY position",
            (run_id,),
        )
        return RunRecord(
            run_id=row["run_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            done=row["done"],
            sources=tuple(RunSourceRecord(**dict(source)) for source in sources),
        )

    def add_run(self, run: RunRecord) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO runs (run_id, started_at, done) VALUES (?, ?, ?)",
                (run.run_id, run.started_at.isoformat(), run.done),
            )
            self._write_run_sources(run, insert=True)

    def update_run(self, run: RunRecord) -> None:
        """Save a run's progress: how many sources are done, and each one's state."""
        with self.transaction():
            updated = self._connection.execute(
                "UPDATE runs SET done = ? WHERE run_id = ?", (run.done, run.run_id)
            )
            if updated.rowcount == 0:
                raise StateError(f"no run {run.run_id!r}")
            self._write_run_sources(run, insert=False)

    def _write_run_sources(self, run: RunRecord, *, insert: bool) -> None:
        if insert:
            self._connection.executemany(
                """
                INSERT INTO run_sources
                    (run_id, position, source_id, name, format, state, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (run.run_id, i, s.source_id, s.name, s.format, s.state, s.detail)
                    for i, s in enumerate(run.sources)
                ],
            )
            return
        self._connection.executemany(
            "UPDATE run_sources SET state = ?, detail = ? "
            "WHERE run_id = ? AND position = ?",
            [(s.state, s.detail, run.run_id, i) for i, s in enumerate(run.sources)],
        )

    # ------------------------------------------------- decisions and reviews

    def decisions(self) -> dict[int, str]:
        return {
            row["claim_id"]: row["decision"]
            for row in self._connection.execute("SELECT * FROM decisions")
        }

    def set_decision(self, claim_id: int, decision: str) -> None:
        self._connection.execute(
            """
            INSERT INTO decisions (claim_id, decision) VALUES (?, ?)
            ON CONFLICT (claim_id) DO UPDATE SET decision = excluded.decision
            """,
            (claim_id, decision),
        )

    def reviews(self) -> dict[int, ReviewRecord]:
        return {
            row["claim_id"]: ReviewRecord(
                verdict=row["verdict"], text=row["text"], note=row["note"]
            )
            for row in self._connection.execute("SELECT * FROM reviews")
        }

    def set_review(self, claim_id: int, review: ReviewRecord | None) -> None:
        """Record a claim's review so far; ``None`` removes it. The history of
        how it got there is the audit log's job, not this table's."""
        if review is None:
            self._connection.execute(
                "DELETE FROM reviews WHERE claim_id = ?", (claim_id,)
            )
            return
        self._connection.execute(
            """
            INSERT INTO reviews (claim_id, verdict, text, note) VALUES (?, ?, ?, ?)
            ON CONFLICT (claim_id) DO UPDATE SET
                verdict = excluded.verdict, text = excluded.text, note = excluded.note
            """,
            (claim_id, review.verdict, review.text, review.note),
        )

    # ----------------------------------------------------------------- audit

    def append_audit(
        self,
        *,
        t: datetime,
        who: str,
        action: str,
        claim_id: int | None = None,
        source_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Record an action. The only way anything reaches the audit log."""
        self._connection.execute(
            """
            INSERT INTO audit (t, who, action, claim_id, source_id, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (t.isoformat(), who, action, claim_id, source_id, detail),
        )

    def audit(self) -> list[AuditRecord]:
        """Every entry, oldest first."""
        return [
            AuditRecord(
                seq=row["seq"],
                t=datetime.fromisoformat(row["t"]),
                who=row["who"],
                action=row["action"],
                claim_id=row["claim_id"],
                source_id=row["source_id"],
                detail=row["detail"],
            )
            for row in self._connection.execute("SELECT * FROM audit ORDER BY seq")
        ]

    # -------------------------------------------------------------- settings

    def setting(self, key: str) -> str | None:
        """A stored setting, serialised by the caller; ``None`` if never set."""
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
