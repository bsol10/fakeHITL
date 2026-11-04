"""The HTTP API against the prototype's contract, and the policies it enforces.

The fixtures are the prototype's own data, so responses are compared to them
directly: a renamed, dropped or reordered field fails here.

These are the acceptance tests for the API's state: keep them passing
*unchanged* when the state behind the API changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from updater.api import create_app
from updater.api.fixtures import load
from updater.api.mock import QUEUED, SCANNING, MockState
from updater.cli import build_parser

PROTOTYPE_DAY = datetime(2026, 9, 16, 10, 24, tzinfo=timezone(timedelta(hours=10)))
APPROVED = [s["id"] for s in load("sources.json") if s["status"] == "approved"]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(MockState(clock=lambda: PROTOTYPE_DAY))) as client:
        yield client


def start_run(client: TestClient, source_ids: list[str] | None = None) -> Any:
    response = client.post("/api/runs", json={"source_ids": source_ids or APPROVED})
    assert response.status_code == 201, response.text
    return response.json()


def source_by_status(status: str) -> dict[str, Any]:
    return next(s for s in load("sources.json") if s["status"] == status)


# Shapes


@pytest.mark.parametrize(
    ("path", "fixture"),
    [
        ("/api/summary", "summary.json"),
        ("/api/activity", "activity.json"),
        ("/api/notifications", "notifications.json"),
        ("/api/sources", "sources.json"),
        ("/api/settings", "settings.json"),
    ],
)
def test_read_endpoints_serve_the_prototypes_data(
    client: TestClient, path: str, fixture: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == load(fixture)


def test_a_source_has_the_contracts_fields_in_order(client: TestClient) -> None:
    first = client.get("/api/sources").json()[0]
    assert list(first) == [
        "id",
        "name",
        "url",
        "owner",
        "topic",
        "format",
        "update_frequency",
        "status",
        "last_checked",
    ]
    assert first["id"] == "youth-employment-outlook-2026"
    assert first["last_checked"] == "2026-09-13T09:00:00+10:00"


def test_a_claim_matches_the_prototypes_claims_0_field_by_field(
    client: TestClient,
) -> None:
    run = start_run(client)
    served = client.get(f"/api/runs/{run['run_id']}/claims").json()
    prototype = load("claims.json")
    assert len(served) == len(prototype)

    claim, expected = served[0], prototype[0]
    assert list(claim)[: len(expected)] == list(expected)
    for name, value in expected.items():
        assert claim[name] == value, name
    assert claim["decision"] is None
    assert set(claim) - set(expected) == {"decision"}


def test_every_claim_carries_a_rationale(client: TestClient) -> None:
    run = start_run(client)
    for claim in client.get(f"/api/runs/{run['run_id']}/claims").json():
        assert claim["note"].strip(), claim["id"]


# Scan progress


def test_a_run_starts_on_its_first_source_and_returns_immediately(
    client: TestClient,
) -> None:
    run = start_run(client)
    assert run["run_id"] == "r_2026_09_16_01"
    assert run["state"] == "scanning"
    states = [source["state"] for source in run["sources"]]
    assert states == ["scanning"] + ["queued"] * (len(APPROVED) - 1)
    assert run["sources"][0]["detail"] == SCANNING
    assert run["sources"][1]["detail"] == QUEUED
    assert [source["source_id"] for source in run["sources"]] == APPROVED


def test_each_poll_advances_one_source_through_the_prototypes_outcomes(
    client: TestClient,
) -> None:
    run_id = start_run(client)["run_id"]
    outcomes = load("outcomes.json")

    for done in range(1, len(APPROVED) + 1):
        run = client.get(f"/api/runs/{run_id}").json()
        finished = run["sources"][:done]
        assert [(s["state"], s["detail"]) for s in finished] == [
            (o["state"], o["detail"]) for o in outcomes[:done]
        ]
        if done < len(APPROVED):
            assert run["state"] == "scanning"
            assert run["sources"][done]["state"] == "scanning"
    assert run["state"] == "complete"

    # A finished run stays finished.
    assert client.get(f"/api/runs/{run_id}").json() == run


def test_run_ids_count_up_within_a_day(client: TestClient) -> None:
    assert start_run(client)["run_id"] == "r_2026_09_16_01"
    assert start_run(client)["run_id"] == "r_2026_09_16_02"


# Rule 3: only approved sources are scanned


@pytest.mark.parametrize("status", ["pending", "paused", "url_changed"])
def test_a_source_that_is_not_approved_cannot_be_scanned(
    client: TestClient, status: str
) -> None:
    blocked = source_by_status(status)
    response = client.post(
        "/api/runs", json={"source_ids": [*APPROVED[:2], blocked["id"]]}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert blocked["name"] in detail
    assert "design rule 3" in detail


def test_a_refused_run_is_refused_whole_rather_than_filtered(
    client: TestClient,
) -> None:
    pending = source_by_status("pending")["id"]
    assert (
        client.post("/api/runs", json={"source_ids": [APPROVED[0], pending]})
    ).status_code == 409
    # Nothing was started on the approved half: the next run is still the first.
    assert start_run(client)["run_id"] == "r_2026_09_16_01"


def test_every_blocked_source_is_named_at_once(client: TestClient) -> None:
    blocked = [s for s in load("sources.json") if s["status"] != "approved"]
    response = client.post("/api/runs", json={"source_ids": [s["id"] for s in blocked]})
    assert response.status_code == 409
    for source in blocked:
        assert source["name"] in response.json()["detail"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"source_ids": []}, 422),
        ({"source_ids": ["no-such-source"]}, 404),
        ({"source_ids": [APPROVED[0], APPROVED[0]]}, 422),
        ({"source_ids": APPROVED, "status": "approved"}, 422),
    ],
)
def test_malformed_run_requests_are_rejected(
    client: TestClient, body: dict[str, Any], status: int
) -> None:
    assert client.post("/api/runs", json=body).status_code == status


@pytest.mark.parametrize("path", ["/api/runs/r_nope", "/api/runs/r_nope/claims"])
def test_an_unknown_run_is_not_found(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "r_nope" in response.json()["detail"]


# Serving


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://localhost:5173", True),
        ("http://127.0.0.1:8080", True),
        ("https://example.org", False),
        ("null", False),
    ],
)
def test_cors_admits_local_development_origins_only(
    client: TestClient, origin: str, allowed: bool
) -> None:
    response = client.get("/api/sources", headers={"Origin": origin})
    granted = response.headers.get("access-control-allow-origin")
    assert (granted == origin) is allowed


def test_serve_binds_to_localhost_by_default() -> None:
    args = build_parser().parse_args(["serve"])
    assert (args.host, args.port) == ("127.0.0.1", 8000)


def test_serve_runs_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from updater.cli import main

    calls: list[tuple[Any, str, int]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, host, port: calls.append((app, host, port))
    )
    assert main(["serve", "--port", "8123"]) == 0
    [(app, host, port)] = calls
    assert (host, port) == ("127.0.0.1", 8123)
    assert "/api/sources" in app.openapi()["paths"]
