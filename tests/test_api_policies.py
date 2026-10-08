"""The API's write side, and the policies the mock already enforces.

BACKEND_PLAN.md phase 0: the frontend is built against the refusals it will
meet, so they exist before anything real does. In order: a proposed source
starts pending; a decision never puts an unverified claim in the draft; every
review is audited; sign-off follows the prototype's rule exactly (every draft
claim has a verdict and at least one is approved or edited; a flagged claim does
not block, and is left out); the report holds approved and edited claims only;
the policies in Settings cannot be changed.

With ``test_api.py``, this is phase 1's acceptance test: it must keep passing
*unchanged* when the state moves to SQLite.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from conftest import write_docx, write_pdf
from updater.api import create_app
from updater.api.fixtures import load
from updater.api.mock import MockState

NOW = datetime(2026, 9, 16, 10, 24, tzinfo=timezone(timedelta(hours=10)))
CLAIMS = {claim["id"]: claim for claim in load("claims.json")}
UNVERIFIED = next(i for i, c in CLAIMS.items() if c["status"] == "cannot")
DRAFTABLE = [i for i, c in CLAIMS.items() if c["status"] != "cannot"]
NEW_SOURCE = {
    "name": "Youth Mental Health Survey 2026",
    "url": "https://example.org/survey",
    "owner": "R. Osei",
    "topic": "Wellbeing",
    "format": "PDF",
    "update_frequency": "Annual",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(MockState(clock=lambda: NOW))) as client:
        yield client


@pytest.fixture
def run_id(client: TestClient) -> str:
    approved = [s["id"] for s in load("sources.json") if s["status"] == "approved"]
    response = client.post("/api/runs", json={"source_ids": approved})
    assert response.status_code == 201
    run_id: str = response.json()["run_id"]
    return run_id


def review(client: TestClient, claim_id: int, **body: Any) -> Any:
    return client.post(f"/api/claims/{claim_id}/review", json=body)


def review_all(client: TestClient, verdict: str = "approved") -> None:
    for claim_id in DRAFTABLE:
        assert review(client, claim_id, verdict=verdict).status_code == 200


def audit(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = client.get(f"/api/runs/{run_id}/audit").json()
    return entries


# --- 1 Source registry -----------------------------------------------------


def test_a_proposed_source_starts_pending(client: TestClient) -> None:
    response = client.post("/api/sources", json=NEW_SOURCE)
    assert response.status_code == 201
    source = response.json()
    assert source["id"] == "youth-mental-health-survey-2026"
    assert source["status"] == "pending"
    assert source["last_checked"] is None
    assert source in client.get("/api/sources").json()


def test_a_proposed_source_cannot_arrive_approved(client: TestClient) -> None:
    response = client.post("/api/sources", json={**NEW_SOURCE, "status": "approved"})
    assert response.status_code == 422
    ids = [s["id"] for s in client.get("/api/sources").json()]
    assert "youth-mental-health-survey-2026" not in ids


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"name": "Youth Employment Outlook 2026"}, 409),  # already registered
        ({"url": "abs.gov.au/no-scheme"}, 422),
        ({"topic": "Astrology"}, 422),
        ({"name": "  "}, 422),
    ],
)
def test_malformed_or_duplicate_sources_are_rejected(
    client: TestClient, change: dict[str, str], status: int
) -> None:
    assert client.post("/api/sources", json={**NEW_SOURCE, **change}).status_code == (
        status
    )


def test_a_new_source_is_scannable_only_once_approved(client: TestClient) -> None:
    source_id = client.post("/api/sources", json=NEW_SOURCE).json()["id"]
    body = {"source_ids": [source_id]}
    assert client.post("/api/runs", json=body).status_code == 409

    approved = client.patch(f"/api/sources/{source_id}", json={"status": "approved"})
    assert approved.json()["status"] == "approved"
    assert client.post("/api/runs", json=body).status_code == 201


def test_pause_and_resume(client: TestClient) -> None:
    path = "/api/sources/youth-employment-outlook-2026"
    assert client.patch(path, json={"status": "paused"}).json()["status"] == "paused"
    assert client.patch(path, json={"status": "approved"}).json()["status"] == (
        "approved"
    )


def test_confirming_a_changed_url(client: TestClient) -> None:
    moved = next(s for s in load("sources.json") if s["status"] == "url_changed")
    response = client.patch(
        f"/api/sources/{moved['id']}",
        json={"status": "approved", "url": "https://example.org/new-home"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["url"] == "https://example.org/new-home"


@pytest.mark.parametrize(
    ("current", "body", "status"),
    [
        ("approved", {"status": "approved"}, 409),
        ("pending", {"status": "paused"}, 409),
        ("approved", {"status": "pending"}, 422),
        ("paused", {"status": "url_changed"}, 422),
        ("pending", {"status": "approved", "url": "https://example.org/x"}, 422),
    ],
)
def test_status_changes_outside_the_lifecycle_are_refused(
    client: TestClient, current: str, body: dict[str, str], status: int
) -> None:
    source = next(s for s in load("sources.json") if s["status"] == current)
    response = client.patch(f"/api/sources/{source['id']}", json=body)
    assert response.status_code == status
    after = {s["id"]: s for s in client.get("/api/sources").json()}[source["id"]]
    assert after == source


def test_patching_an_unknown_source_is_not_found(client: TestClient) -> None:
    response = client.patch("/api/sources/nope", json={"status": "approved"})
    assert response.status_code == 404


# --- 3 Extraction ----------------------------------------------------------


def test_an_unverified_claim_cannot_be_put_in_the_draft(
    client: TestClient, run_id: str
) -> None:
    response = client.put(
        f"/api/claims/{UNVERIFIED}/decision", json={"decision": "inc"}
    )
    assert response.status_code == 409
    assert "Unverified claims can't go into the draft" in response.json()["detail"]
    claims = client.get(f"/api/runs/{run_id}/claims").json()
    assert {c["id"]: c for c in claims}[UNVERIFIED]["decision"] is None
    assert audit(client, run_id) == []


def test_an_unverified_claim_can_still_be_sent_for_a_human_check(
    client: TestClient,
) -> None:
    response = client.put(
        f"/api/claims/{UNVERIFIED}/decision", json={"decision": "check"}
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "check"


def test_a_decision_is_recorded_and_audited(client: TestClient, run_id: str) -> None:
    response = client.put("/api/claims/1/decision", json={"decision": "inc"})
    assert response.json()["decision"] == "inc"
    claims = client.get(f"/api/runs/{run_id}/claims").json()
    assert {c["id"]: c for c in claims}[1]["decision"] == "inc"
    assert audit(client, run_id) == [
        {"t": NOW.isoformat(), "who": "Kathleen D.", "action": "included", "claim": 1}
    ]


def test_deciding_an_unknown_claim_is_not_found(client: TestClient) -> None:
    response = client.put("/api/claims/999/decision", json={"decision": "inc"})
    assert response.status_code == 404


def test_a_prior_report_is_accepted_by_content_not_by_name(
    client: TestClient, tmp_path: Path
) -> None:
    docx = write_docx(tmp_path / "report.docx").read_bytes()
    response = client.post(
        "/api/reports/prior", files={"file": ("2025 report.docx", docx)}
    )
    assert response.status_code == 201
    assert response.json() == {
        "filename": "2025 report.docx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "size": len(docx),
    }

    pdf = write_pdf(tmp_path / "report.pdf", [["Youth outcomes"]]).read_bytes()
    misnamed = client.post("/api/reports/prior", files={"file": ("notes.txt", pdf)})
    assert misnamed.json()["media_type"] == "application/pdf"


def test_a_prior_report_that_is_not_docx_or_pdf_is_refused(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/reports/prior", files={"file": ("report.pdf", b"just some text\n")}
    )
    assert response.status_code == 415
    assert "report.pdf is text/plain" in response.json()["detail"]


# --- 4 Draft review --------------------------------------------------------


def test_until_anything_is_included_the_draft_is_every_verifiable_claim(
    client: TestClient, run_id: str
) -> None:
    draft = client.get(f"/api/runs/{run_id}/draft").json()
    assert [item["claim_id"] for item in draft] == DRAFTABLE
    assert UNVERIFIED not in [item["claim_id"] for item in draft]


def test_the_draft_holds_the_included_claims(client: TestClient, run_id: str) -> None:
    for claim_id in (6, 1):
        client.put(f"/api/claims/{claim_id}/decision", json={"decision": "inc"})
    client.put("/api/claims/2/decision", json={"decision": "skip"})
    draft = client.get(f"/api/runs/{run_id}/draft").json()
    assert [item["claim_id"] for item in draft] == [1, 6]


def test_a_draft_item_is_the_prototypes_draft_text(
    client: TestClient, run_id: str
) -> None:
    served = {i["claim_id"]: i for i in client.get(f"/api/runs/{run_id}/draft").json()}
    for expected in load("draft.json"):
        item = served[expected["claim_id"]]
        assert {k: item[k] for k in expected} == expected
        assert item["review"] is None
        # The passage is source text, never the claim's own wording.
        assert item["passage"] != item["sent"]


def test_a_review_is_recorded_and_audited(client: TestClient, run_id: str) -> None:
    response = review(client, 1, verdict="approved")
    assert response.status_code == 200
    assert response.json()["review"] == {
        "verdict": "approved",
        "text": None,
        "note": None,
    }
    review(client, 2, verdict="flagged", note="Which edition?")
    assert audit(client, run_id) == [
        {"t": NOW.isoformat(), "who": "Kathleen D.", "action": a, "claim": c}
        for a, c in [("note_added", 2), ("flagged", 2), ("approved", 1)]
    ]


def test_an_edit_keeps_its_text_through_a_later_verdict(
    client: TestClient, run_id: str
) -> None:
    review(client, 1, verdict="edited", text="  Youth unemployment rose to 13.1%. ")
    after = review(client, 1, verdict="approved").json()["review"]
    assert after == {
        "verdict": "approved",
        "text": "Youth unemployment rose to 13.1%.",
        "note": None,
    }


def test_a_note_alone_is_kept_and_an_empty_one_removes_it(
    client: TestClient, run_id: str
) -> None:
    assert review(client, 3, note="Check the ABS table").json()["review"] == {
        "verdict": None,
        "text": None,
        "note": "Check the ABS table",
    }
    assert review(client, 3, note="").json()["review"] is None
    assert [e["action"] for e in audit(client, run_id)] == ["note_added"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"verdict": "edited"},
        {"verdict": "edited", "text": "   "},
        {"verdict": "approved", "text": "A sneaked-in rewrite."},
        {"verdict": "published"},
    ],
)
def test_incomplete_or_contradictory_reviews_are_rejected(
    client: TestClient, run_id: str, body: dict[str, str]
) -> None:
    assert review(client, 1, **body).status_code == 422
    assert audit(client, run_id) == []


def test_a_claim_outside_the_draft_cannot_be_reviewed(client: TestClient) -> None:
    assert review(client, UNVERIFIED, verdict="approved").status_code == 409


def test_the_audit_log_has_no_write_path() -> None:
    paths = create_app(MockState()).openapi()["paths"]
    assert set(paths["/api/runs/{run_id}/audit"]) == {"get"}
    assert not [p for p in paths if "audit" in p and p != "/api/runs/{run_id}/audit"]


# --- 5 Sign-off ------------------------------------------------------------


def test_sign_off_is_refused_while_a_claim_has_no_verdict(
    client: TestClient, run_id: str
) -> None:
    for claim_id in DRAFTABLE[1:]:
        review(client, claim_id, verdict="approved")
    response = client.post(f"/api/runs/{run_id}/sign-off")
    assert response.status_code == 409
    assert "1 claim still needs a decision" in response.json()["detail"]

    publish = client.get(f"/api/runs/{run_id}/publish").json()
    assert publish["unresolved"] == 1
    assert publish["signed_off_at"] is None
    assert "signed_off" not in [e["action"] for e in audit(client, run_id)]


def test_a_note_does_not_resolve_a_claim(client: TestClient, run_id: str) -> None:
    for claim_id in DRAFTABLE[1:]:
        review(client, claim_id, verdict="approved")
    review(client, DRAFTABLE[0], note="Looks fine")
    assert client.get(f"/api/runs/{run_id}/publish").json()["unresolved"] == 1
    assert client.post(f"/api/runs/{run_id}/sign-off").status_code == 409


def test_sign_off_is_refused_when_nothing_is_approved(
    client: TestClient, run_id: str
) -> None:
    review_all(client, verdict="rejected")
    response = client.post(f"/api/runs/{run_id}/sign-off")
    assert response.status_code == 409
    assert "Approve at least one claim" in response.json()["detail"]


def test_a_flagged_claim_does_not_block_sign_off_and_is_left_out(
    client: TestClient, run_id: str
) -> None:
    """The prototype's rule, chosen over the stricter reading of the plan."""
    review_all(client)
    flagged = DRAFTABLE[2]
    review(client, flagged, verdict="flagged")

    response = client.post(f"/api/runs/{run_id}/sign-off")
    assert response.status_code == 200
    publish = response.json()
    assert publish["counts"]["flagged"] == 1
    assert publish["signed_off_by"] == "Kathleen D."
    assert publish["signed_off_at"] == NOW.isoformat()
    in_report = [i["claim_id"] for s in publish["sections"] for i in s["items"]]
    assert flagged not in in_report
    assert audit(client, run_id)[0] == {
        "t": NOW.isoformat(),
        "who": "Kathleen D.",
        "action": "signed_off",
        "claim": None,
    }


def test_the_report_holds_approved_and_edited_claims_only(
    client: TestClient, run_id: str
) -> None:
    review_all(client)
    review(client, 2, verdict="rejected")
    review(client, 6, verdict="edited", text="Edited sentence about homelessness.")
    publish = client.get(f"/api/runs/{run_id}/publish").json()

    items = {i["claim_id"]: i for s in publish["sections"] for i in s["items"]}
    assert 2 not in items
    assert UNVERIFIED not in items
    assert items[6]["text"] == "Edited sentence about homelessness."
    drafted = {d["claim_id"]: d["sent"] for d in load("draft.json")}
    assert items[1]["text"] == drafted[1]
    assert publish["counts"] == {
        "approved": len(DRAFTABLE) - 2,
        "edited": 1,
        "flagged": 0,
        "rejected": 1,
        "cannot": 1,
    }


def test_citation_numbers_are_draft_positions_and_sections_keep_their_order(
    client: TestClient, run_id: str
) -> None:
    review_all(client)
    publish = client.get(f"/api/runs/{run_id}/publish").json()
    cites = {i["claim_id"]: i["cite"] for s in publish["sections"] for i in s["items"]}
    assert cites == {claim_id: n for n, claim_id in enumerate(DRAFTABLE, start=1)}

    first_seen = list(dict.fromkeys(CLAIMS[i]["section"] for i in DRAFTABLE))
    assert [s["section"] for s in publish["sections"]] == first_seen
    assert publish["title"] == "2026 Youth Outcomes Report"


def test_after_sign_off_the_report_is_locked(client: TestClient, run_id: str) -> None:
    review_all(client)
    assert client.post(f"/api/runs/{run_id}/sign-off").status_code == 200

    assert client.post(f"/api/runs/{run_id}/sign-off").status_code == 409
    assert review(client, 1, verdict="rejected").status_code == 409
    decision = client.put("/api/claims/1/decision", json={"decision": "skip"})
    assert decision.status_code == 409


# --- exports ---------------------------------------------------------------


def test_the_audit_csv_is_always_available(client: TestClient, run_id: str) -> None:
    review(client, 1, verdict="approved")
    review(client, 2, verdict="flagged")
    response = client.get(f"/api/runs/{run_id}/export", params={"format": "audit_csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows == [
        ["t", "who", "action", "claim"],
        [NOW.isoformat(), "Kathleen D.", "approved", "1"],
        [NOW.isoformat(), "Kathleen D.", "flagged", "2"],
    ]


@pytest.mark.parametrize("fmt", ["pdf", "docx", "newsletter"])
def test_report_exports_wait_for_sign_off(
    client: TestClient, run_id: str, fmt: str
) -> None:
    path = f"/api/runs/{run_id}/export"
    assert client.get(path, params={"format": fmt}).status_code == 409
    review_all(client)
    client.post(f"/api/runs/{run_id}/sign-off")
    # Signed off, and not built yet: phase 8. Never a placeholder file.
    assert client.get(path, params={"format": fmt}).status_code == 501


def test_an_unknown_export_format_is_rejected(client: TestClient, run_id: str) -> None:
    path = f"/api/runs/{run_id}/export"
    assert client.get(path, params={"format": "xlsx"}).status_code == 422


# --- settings --------------------------------------------------------------


def test_settings_can_be_changed(client: TestClient) -> None:
    settings = client.get("/api/settings").json()
    settings["scan"]["frequency"] = "monthly"
    settings["thresholds"]["flag_below"] = None
    assert client.put("/api/settings", json=settings).json() == settings
    assert client.get("/api/settings").json() == settings


@pytest.mark.parametrize("policy", ["only_cite_approved", "staff_sign_off"])
def test_the_policies_cannot_be_changed(client: TestClient, policy: str) -> None:
    before = client.get("/api/settings").json()
    attempt = {**before, "policies": {**before["policies"], policy: False}}
    attempt["scan"] = {**before["scan"], "frequency": "monthly"}
    response = client.put("/api/settings", json=attempt)
    assert response.status_code == 422
    assert "fixed by policy" in response.json()["detail"]
    # Refused whole: the permitted change in the same request did not land.
    assert client.get("/api/settings").json() == before


@pytest.mark.parametrize(
    "change",
    [
        {"scan": {"frequency": "hourly"}},
        {"thresholds": {"flag_below": 50}},
        {"scan": {"topics": ["Astrology"]}},
    ],
)
def test_settings_outside_the_prototypes_options_are_rejected(
    client: TestClient, change: dict[str, dict[str, Any]]
) -> None:
    settings = client.get("/api/settings").json()
    for section, values in change.items():
        settings[section] = {**settings[section], **values}
    assert client.put("/api/settings", json=settings).status_code == 422
