"""Restarting the server loses nothing anybody did.

``test_api.py`` and ``test_api_policies.py`` pin the responses themselves; these
tests pin that they are the same on either side of a restart. Also here: a
change and its audit entry are saved together or not at all, and registry
actions are audited without appearing in a report's review trail.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from updater.api import create_app
from updater.api.fixtures import load
from updater.api.mock import SOURCE_ACTION, MockState
from updater.api.policy import SOURCE_TRANSITIONS
from updater.state import IncompatibleState, StateStore

NOW = datetime(2026, 9, 16, 10, 24, tzinfo=timezone(timedelta(hours=10)))
APPROVED = [s["id"] for s in load("sources.json") if s["status"] == "approved"]
DRAFTABLE = [c["id"] for c in load("claims.json") if c["status"] != "cannot"]
NEW_SOURCE = {
    "name": "Youth Mental Health Survey 2026",
    "url": "https://example.org/survey",
    "owner": "R. Osei",
    "topic": "Wellbeing",
    "format": "PDF",
    "update_frequency": "Annual",
}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "updater.sqlite3"


@contextmanager
def server(db: Path) -> Iterator[TestClient]:
    """One lifetime of ``updater serve`` against ``db``."""
    state = MockState(clock=lambda: NOW, path=db)
    try:
        with TestClient(create_app(state)) as client:
            yield client
    finally:
        state.close()


def start_run(client: TestClient) -> str:
    response = client.post("/api/runs", json={"source_ids": APPROVED})
    assert response.status_code == 201, response.text
    run_id: str = response.json()["run_id"]
    return run_id


def everything_readable(client: TestClient, run_id: str) -> dict[str, Any]:
    """Every response that reflects state, except polling, which advances it."""
    paths = [
        "/api/sources",
        "/api/settings",
        f"/api/runs/{run_id}/claims",
        f"/api/runs/{run_id}/draft",
        f"/api/runs/{run_id}/audit",
        f"/api/runs/{run_id}/publish",
    ]
    readable = {path: client.get(path).json() for path in paths}
    csv = client.get(f"/api/runs/{run_id}/export", params={"format": "audit_csv"})
    readable["audit_csv"] = csv.text
    return readable


def test_a_review_survives_a_restart_midway(db: Path) -> None:
    with server(db) as client:
        run_id = start_run(client)
        for claim_id in (1, 2, 6):
            client.put(f"/api/claims/{claim_id}/decision", json={"decision": "inc"})
        client.put("/api/claims/3/decision", json={"decision": "skip"})
        client.post("/api/claims/1/review", json={"verdict": "edited", "text": "New."})
        client.post("/api/claims/2/review", json={"note": "Which edition?"})
        client.post("/api/claims/6/review", json={"verdict": "flagged"})
        before = everything_readable(client, run_id)

    with server(db) as client:
        after = everything_readable(client, run_id)
        assert after == before
        assert [e["action"] for e in after[f"/api/runs/{run_id}/audit"]] == [
            "flagged",
            "note_added",
            "edited",
            "skipped",
            "included",
            "included",
            "included",
        ]
        # And the review carries on from where it stopped.
        resumed = client.post("/api/claims/6/review", json={"verdict": "approved"})
        assert resumed.status_code == 200
        assert len(client.get(f"/api/runs/{run_id}/audit").json()) == 8


def test_a_signed_off_report_stays_locked_after_a_restart(db: Path) -> None:
    with server(db) as client:
        run_id = start_run(client)
        for claim_id in DRAFTABLE:
            client.post(f"/api/claims/{claim_id}/review", json={"verdict": "approved"})
        signed = client.post(f"/api/runs/{run_id}/sign-off")
        assert signed.status_code == 200

    with server(db) as client:
        publish = client.get(f"/api/runs/{run_id}/publish").json()
        assert publish == signed.json()
        assert publish["signed_off_at"] == NOW.isoformat()
        assert client.post(f"/api/runs/{run_id}/sign-off").status_code == 409
        locked = client.post("/api/claims/1/review", json={"verdict": "rejected"})
        assert locked.status_code == 409


def test_run_progress_and_numbering_survive_a_restart(db: Path) -> None:
    outcomes = load("outcomes.json")
    with server(db) as client:
        run_id = start_run(client)
        client.get(f"/api/runs/{run_id}")
        client.get(f"/api/runs/{run_id}")

    with server(db) as client:
        run = client.get(f"/api/runs/{run_id}").json()
        finished = [(s["state"], s["detail"]) for s in run["sources"][:3]]
        assert finished == [(o["state"], o["detail"]) for o in outcomes[:3]]
        assert run["sources"][3]["state"] == "scanning"
        assert start_run(client) == "r_2026_09_16_02"


def test_settings_survive_a_restart(db: Path) -> None:
    with server(db) as client:
        settings = client.get("/api/settings").json()
        settings["scan"]["frequency"] = "monthly"
        settings["thresholds"]["flag_below"] = None
        assert client.put("/api/settings", json=settings).status_code == 200

    with server(db) as client:
        assert client.get("/api/settings").json() == settings


def test_source_changes_survive_a_restart(db: Path) -> None:
    with server(db) as client:
        new_id = client.post("/api/sources", json=NEW_SOURCE).json()["id"]
        client.patch(f"/api/sources/{new_id}", json={"status": "approved"})
        client.patch(f"/api/sources/{APPROVED[0]}", json={"status": "paused"})
        client.patch(
            "/api/sources/community-foundations-giving-report",
            json={"status": "approved", "url": "https://example.org/new-home"},
        )
        before = client.get("/api/sources").json()

    with server(db) as client:
        assert client.get("/api/sources").json() == before
        by_id = {s["id"]: s for s in before}
        assert by_id[new_id]["status"] == "approved"
        assert by_id[APPROVED[0]]["status"] == "paused"
        assert by_id["community-foundations-giving-report"]["url"] == (
            "https://example.org/new-home"
        )
        scan = client.post("/api/runs", json={"source_ids": [new_id]})
        assert scan.status_code == 201
        refused = client.post("/api/runs", json={"source_ids": [APPROVED[0]]})
        assert refused.status_code == 409


# Registry actions are audited


def registry_log(db: Path) -> list[tuple[str, str, str, str | None]]:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(
            "SELECT who, action, source_id, detail FROM audit "
            "WHERE source_id IS NOT NULL ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()


def test_every_source_change_is_audited_and_kept_out_of_the_review_trail(
    db: Path,
) -> None:
    with server(db) as client:
        new_id = client.post("/api/sources", json=NEW_SOURCE).json()["id"]
        client.patch(f"/api/sources/{new_id}", json={"status": "approved"})
        client.patch(f"/api/sources/{new_id}", json={"status": "paused"})
        client.patch(f"/api/sources/{new_id}", json={"status": "approved"})
        client.patch(
            "/api/sources/rental-affordability-index", json={"status": "approved"}
        )
        # A refused change records nothing.
        client.patch(f"/api/sources/{new_id}", json={"status": "approved"})

        run_id = start_run(client)
        assert client.get(f"/api/runs/{run_id}/audit").json() == []
        csv = client.get(f"/api/runs/{run_id}/export", params={"format": "audit_csv"})
        assert csv.text.splitlines() == ["t,who,action,claim"]

    rental_url = next(
        s["url"]
        for s in load("sources.json")
        if s["id"] == "rental-affordability-index"
    )
    assert registry_log(db) == [
        ("Kathleen D.", "source_proposed", new_id, None),
        ("Kathleen D.", "source_approved", new_id, None),
        ("Kathleen D.", "source_paused", new_id, None),
        ("Kathleen D.", "source_resumed", new_id, None),
        (
            "Kathleen D.",
            "source_url_confirmed",
            "rental-affordability-index",
            rental_url,
        ),
    ]


def test_every_permitted_status_change_has_an_audit_action() -> None:
    assert set(SOURCE_ACTION) == SOURCE_TRANSITIONS


# A change and its audit entry are one unit of work


def test_nothing_is_saved_when_its_audit_entry_cannot_be(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the audit entry could not be written")

    with server(db) as client:
        run_id = start_run(client)
        with monkeypatch.context() as patch:
            patch.setattr(StateStore, "append_audit", fail)
            attempts: list[tuple[str, str, dict[str, Any]]] = [
                ("PUT", "/api/claims/1/decision", {"decision": "inc"}),
                ("POST", "/api/claims/1/review", {"verdict": "approved"}),
                ("POST", "/api/sources", NEW_SOURCE),
                ("PATCH", f"/api/sources/{APPROVED[0]}", {"status": "paused"}),
            ]
            for method, path, body in attempts:
                with pytest.raises(RuntimeError):
                    client.request(method, path, json=body)

    with server(db) as client:
        claims = {c["id"]: c for c in client.get(f"/api/runs/{run_id}/claims").json()}
        assert claims[1]["decision"] is None
        draft = {
            d["claim_id"]: d for d in client.get(f"/api/runs/{run_id}/draft").json()
        }
        assert draft[1]["review"] is None
        assert client.get("/api/sources").json() == load("sources.json")
    assert registry_log(db) == []


# An incompatible database is refused


def test_state_about_other_claims_is_refused_at_startup(db: Path) -> None:
    with StateStore(db, claim_set="prototype-fixtures:an-earlier-prototype") as store:
        store.set_decision(3, "inc")
    with pytest.raises(IncompatibleState, match="an-earlier-prototype"):
        MockState(path=db)


# updater serve


def test_serve_keeps_state_in_the_configured_database(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from updater.cli import main

    def serve_one_decision(app: Any, host: str, port: int) -> None:
        with TestClient(app) as client:
            response = client.put("/api/claims/1/decision", json={"decision": "inc"})
            assert response.status_code == 200

    monkeypatch.setattr(uvicorn, "run", serve_one_decision)
    monkeypatch.setenv("UPDATER_DB_PATH", str(db))
    assert main(["serve"]) == 0

    with server(db) as client:
        run_id = start_run(client)
        claims = client.get(f"/api/runs/{run_id}/claims").json()
        assert claims[0]["decision"] == "inc"


def test_serve_refuses_an_incompatible_database(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    from updater.cli import main

    StateStore(db, claim_set="prototype-fixtures:an-earlier-prototype").close()
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served"))
    assert main(["serve", "--db", str(db)]) == 2
    assert "an-earlier-prototype" in capsys.readouterr().err
