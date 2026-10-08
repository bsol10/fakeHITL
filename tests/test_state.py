"""The state store: it survives a restart, the audit log cannot be rewritten,
and it shares a file with the index without either one breaking the other.

BACKEND_PLAN.md phase 1. Three properties, in the order they would hurt:

*The audit log is append-only.* Checked against SQLite itself, not against the
store's good manners: a statement that tries to change an entry is refused by
the database.

*A change and its audit entry are saved together or not at all.* A decision
with no audit entry is a decision nobody can account for.

*Two stores, one file.* The index and the state live in ``UPDATER_DB_PATH``
together. If the state wrote to the index's ``meta`` table, the next
``updater ingest`` would refuse to run; if ``ingest --rebuild`` cleared the
state, it would take the audit log with it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from updater.retrieve.store import PassageStore
from updater.state import (
    IN_MEMORY,
    IncompatibleState,
    ProposedSource,
    ReviewRecord,
    RunRecord,
    RunSourceRecord,
    SourceState,
    StateError,
    StateStore,
)

CLAIMS = "test-claims"
T = datetime(2026, 9, 16, 10, 24, tzinfo=timezone(timedelta(hours=10)))
LATER = T + timedelta(hours=1)
PROPOSAL = ProposedSource(
    id="youth-survey",
    name="Youth Survey",
    url="https://example.org/survey",
    owner="R. Osei",
    topic="Wellbeing",
    format="PDF",
    update_frequency="Annual",
)


def run_record(
    done: int = 0, states: tuple[str, str] = ("scanning", "queued")
) -> RunRecord:
    return RunRecord(
        run_id="r_2026_09_16_01",
        started_at=T,
        done=done,
        sources=tuple(
            RunSourceRecord(
                source_id=f"s{i}", name=f"Source {i}", format="PDF", state=s, detail=s
            )
            for i, s in enumerate(states)
        ),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "updater.sqlite3"


def test_everything_written_survives_reopening(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.propose_source(PROPOSAL)
        store.set_source_status("youth-survey", "pending")
        store.set_source_status("moved", "approved", url="https://example.org/new")
        store.mark_checked("moved", T)
        store.add_run(run_record())
        store.update_run(run_record(done=1, states=("complete", "scanning")))
        store.set_decision(1, "inc")
        store.set_review(2, ReviewRecord(verdict="edited", text="New.", note=None))
        store.append_audit(t=T, who="Kathleen D.", action="included", claim_id=1)
        store.append_audit(
            t=LATER, who="Kathleen D.", action="source_approved", source_id="moved"
        )
        store.set_setting("scan", '{"frequency": "monthly"}')

    with StateStore(db, claim_set=CLAIMS) as store:
        assert store.proposed_sources() == [PROPOSAL]
        assert store.source_states() == {
            "youth-survey": SourceState(
                source_id="youth-survey", status="pending", url=None, last_checked=None
            ),
            "moved": SourceState(
                source_id="moved",
                status="approved",
                url="https://example.org/new",
                last_checked=T,
            ),
        }
        assert store.run_ids() == ["r_2026_09_16_01"]
        assert store.run("r_2026_09_16_01") == run_record(
            done=1, states=("complete", "scanning")
        )
        assert store.decisions() == {1: "inc"}
        assert store.reviews() == {
            2: ReviewRecord(verdict="edited", text="New.", note=None)
        }
        [included, approved] = store.audit()
        assert (included.t, included.action, included.claim_id) == (T, "included", 1)
        assert included.t.utcoffset() == timedelta(hours=10)
        assert (approved.action, approved.source_id, approved.claim_id) == (
            "source_approved",
            "moved",
            None,
        )
        assert included.seq < approved.seq
        assert store.setting("scan") == '{"frequency": "monthly"}'
        assert store.setting("thresholds") is None


def test_a_later_status_without_a_url_keeps_the_confirmed_address(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.set_source_status("moved", "approved", url="https://example.org/new")
        store.set_source_status("moved", "paused")
        assert store.source_states()["moved"].url == "https://example.org/new"


def test_a_review_can_be_replaced_and_removed(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.set_review(3, ReviewRecord(verdict=None, text=None, note="Check it"))
        store.set_review(3, ReviewRecord(verdict="approved", text=None, note=None))
        assert store.reviews()[3].verdict == "approved"
        store.set_review(3, None)
        assert store.reviews() == {}


def test_progress_on_an_unknown_run_or_source_is_refused() -> None:
    with StateStore(IN_MEMORY, claim_set=CLAIMS) as store:
        with pytest.raises(StateError, match="r_2026_09_16_01"):
            store.update_run(run_record())
        with pytest.raises(StateError, match="nowhere"):
            store.mark_checked("nowhere", T)
        assert store.run("r_nope") is None


def test_an_in_memory_store_starts_empty_every_time() -> None:
    with StateStore(IN_MEMORY, claim_set=CLAIMS) as store:
        store.set_decision(1, "inc")
    with StateStore(IN_MEMORY, claim_set=CLAIMS) as store:
        assert store.decisions() == {}


# --- the audit log is append-only ------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit SET who = 'Somebody Else'",
        "UPDATE audit SET action = 'rejected' WHERE claim_id = 1",
        "DELETE FROM audit",
    ],
)
def test_sqlite_itself_refuses_to_change_the_audit_log(
    db: Path, statement: str
) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.append_audit(t=T, who="Kathleen D.", action="approved", claim_id=1)

    connection = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
    finally:
        connection.close()

    with StateStore(db, claim_set=CLAIMS) as store:
        [entry] = store.audit()
        assert (entry.who, entry.action) == ("Kathleen D.", "approved")


def test_the_store_offers_no_way_to_change_an_audit_entry() -> None:
    public = {name for name in dir(StateStore) if not name.startswith("_")}
    assert {name for name in public if "audit" in name} == {"append_audit", "audit"}


# --- a change and its audit entry are one unit of work ---------------------


def test_a_failed_transaction_saves_nothing(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        with pytest.raises(RuntimeError), store.transaction():
            store.set_decision(1, "inc")
            raise RuntimeError("the audit entry could not be written")
        assert store.decisions() == {}
        assert store.audit() == []


def test_a_nested_unit_of_work_joins_the_enclosing_one(db: Path) -> None:
    """``add_run`` is atomic on its own, and rolls back with what encloses it."""
    with StateStore(db, claim_set=CLAIMS) as store:
        with pytest.raises(RuntimeError), store.transaction():
            store.add_run(run_record())
            raise RuntimeError("something after it failed")
        assert store.run_ids() == []


# --- an incompatible file is refused and left alone ------------------------


def test_another_schema_version_is_refused_and_left_alone(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.append_audit(t=T, who="Kathleen D.", action="approved", claim_id=1)
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "UPDATE state_meta SET value = '0' WHERE key = 'schema_version'"
        )
    connection.close()

    with pytest.raises(IncompatibleState, match="schema version 0"):
        StateStore(db, claim_set=CLAIMS)

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM audit").fetchone() == (1,)
    finally:
        connection.close()


def test_state_about_other_claims_is_refused(db: Path) -> None:
    with StateStore(db, claim_set="prototype:aaaa") as store:
        store.set_decision(3, "inc")
    with pytest.raises(IncompatibleState, match="prototype:aaaa"):
        StateStore(db, claim_set="prototype:bbbb")
    with StateStore(db, claim_set="prototype:aaaa") as store:
        assert store.decisions() == {3: "inc"}


# --- two stores, one file --------------------------------------------------


def open_index(path: Path, *, rebuild: bool = False) -> PassageStore:
    """The index store, which needs sqlite-vec (``uv sync --extra retrieve``)."""
    pytest.importorskip("sqlite_vec")
    return PassageStore(path, dimensions=8, rebuild=rebuild)


def test_the_index_opens_in_a_file_that_already_holds_state(db: Path) -> None:
    with StateStore(db, claim_set=CLAIMS) as store:
        store.append_audit(t=T, who="Kathleen D.", action="approved", claim_id=1)
    open_index(db).close()
    # And again, now that the index has recorded its settings beside the state.
    open_index(db).close()


def test_rebuilding_the_index_leaves_the_state_alone(db: Path) -> None:
    """A rebuild drops whole tables, so a name wrongly on its list would take
    the audit log with it."""
    open_index(db).close()
    with StateStore(db, claim_set=CLAIMS) as store:
        store.set_decision(1, "inc")
        store.append_audit(t=T, who="Kathleen D.", action="included", claim_id=1)

    open_index(db, rebuild=True).close()  # what `updater ingest --rebuild` does

    with StateStore(db, claim_set=CLAIMS) as store:
        assert store.decisions() == {1: "inc"}
        assert [entry.action for entry in store.audit()] == ["included"]
