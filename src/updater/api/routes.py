"""The endpoints. Thin on purpose: each one is a call into the state.

Response types come from the return annotations, so the contract in
:mod:`updater.api.schemas` is also what ``/docs`` publishes and what every
response is checked against on its way out. Endpoints appear in the order of
the contract table in BACKEND_PLAN.md.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, UploadFile

from updater.api.mock import MockState
from updater.api.schemas import (
    ActivityItem,
    AuditEntry,
    Claim,
    DecisionRequest,
    DraftItem,
    ExportFormat,
    NewRun,
    NewSource,
    Notification,
    PriorReport,
    Publish,
    ReviewRequest,
    Run,
    Settings,
    Source,
    SourceUpdate,
    SummaryCard,
)

__all__ = ["router"]

router = APIRouter(prefix="/api")


def _state(request: Request) -> MockState:
    state: MockState = request.app.state.updater
    return state


State = Annotated[MockState, Depends(_state)]


# --- dashboard and header --------------------------------------------------


@router.get("/summary", tags=["dashboard"])
def summary(state: State) -> list[SummaryCard]:
    return state.summary


@router.get("/activity", tags=["dashboard"])
def activity(state: State) -> list[ActivityItem]:
    return state.activity


@router.get("/notifications", tags=["dashboard"])
def notifications(state: State) -> list[Notification]:
    return state.notifications


# --- 1 Source registry -----------------------------------------------------


@router.get("/sources", tags=["1 source registry"])
def sources(state: State) -> list[Source]:
    return state.sources()


@router.post("/sources", status_code=201, tags=["1 source registry"])
def add_source(body: NewSource, state: State) -> Source:
    """Propose a source. It always starts as ``pending``."""
    return state.add_source(body)


@router.patch("/sources/{source_id}", tags=["1 source registry"])
def update_source(source_id: str, body: SourceUpdate, state: State) -> Source:
    """Approve, pause, resume, or confirm a changed URL."""
    return state.update_source(source_id, body)


# --- 2 Source scan ---------------------------------------------------------


@router.post("/runs", status_code=201, tags=["2 source scan"])
def start_run(body: NewRun, state: State) -> Run:
    """Start a scan. Refused if any source named is not approved (rule 3)."""
    return state.start_run(body.source_ids)


@router.get("/runs/{run_id}", tags=["2 source scan"])
def poll_run(run_id: str, state: State) -> Run:
    """Per-source progress. In the mock, each poll advances the scan by one."""
    return state.poll_run(run_id)


# --- 3 Extraction ----------------------------------------------------------


@router.get("/runs/{run_id}/claims", tags=["3 extraction"])
def claims(run_id: str, state: State) -> list[Claim]:
    return state.claims_for(run_id)


@router.put("/claims/{claim_id}/decision", tags=["3 extraction"])
def decide(claim_id: int, body: DecisionRequest, state: State) -> Claim:
    """Include, skip or mark for a human check. A ``cannot`` claim is never
    included."""
    return state.decide(claim_id, body.decision)


@router.post("/reports/prior", status_code=201, tags=["3 extraction"])
def prior_report(file: UploadFile, state: State) -> PriorReport:
    """Upload last year's report, DOCX or PDF. Checked, not yet parsed."""
    return state.receive_prior_report(file.filename or "upload", file.file.read())


# --- 4 Draft review --------------------------------------------------------


@router.get("/runs/{run_id}/draft", tags=["4 draft review"])
def draft(run_id: str, state: State) -> list[DraftItem]:
    return state.draft(run_id)


@router.post("/claims/{claim_id}/review", tags=["4 draft review"])
def review(claim_id: int, body: ReviewRequest, state: State) -> DraftItem:
    """Approve, edit, flag or reject, and/or leave a note. Writes the audit log."""
    return state.review(claim_id, body)


@router.get("/runs/{run_id}/audit", tags=["4 draft review"])
def audit(run_id: str, state: State) -> list[AuditEntry]:
    return state.audit(run_id)


# --- 5 Sign-off ------------------------------------------------------------


@router.get("/runs/{run_id}/publish", tags=["5 sign-off"])
def publish(run_id: str, state: State) -> Publish:
    return state.publish(run_id)


@router.post("/runs/{run_id}/sign-off", tags=["5 sign-off"])
def sign_off(run_id: str, state: State) -> Publish:
    """Staff sign-off (rule 5). Refused while any draft claim lacks a verdict."""
    return state.sign_off(run_id)


@router.get("/runs/{run_id}/export", tags=["5 sign-off"])
def export(
    run_id: str,
    fmt: Annotated[ExportFormat, Query(alias="format")],
    state: State,
) -> Response:
    """``audit_csv`` at any time; the report formats only after sign-off."""
    content = state.export(run_id, fmt)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-audit.csv"'},
    )


# --- Settings --------------------------------------------------------------


@router.get("/settings", tags=["settings"])
def settings(state: State) -> Settings:
    return state.settings()


@router.put("/settings", tags=["settings"])
def update_settings(body: Settings, state: State) -> Settings:
    """The two policies are read-only: a request that changes one is refused."""
    return state.update_settings(body)
