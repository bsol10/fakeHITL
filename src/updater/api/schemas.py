"""The API's wire format. Pydantic lives here, at the boundary, and nowhere else.

Field names and order are the prototype's, verbatim (BACKEND_PLAN.md, "API
contract"), so each screen renders a response as it rendered the array it
replaces. That is why ``lastYear`` sits beside ``update_frequency``. Fields are
added, never renamed: a claim carries its ``decision`` and a draft item its
``review``, so a reloaded page can restore what the prototype kept in memory.

Nullable fields are nullable on purpose (design rule 2). A claim's ``after`` is
null when nothing was retrieved, never "Not retrieved"; the frontend renders the
label. Every model forbids unknown fields, which for a request body is a policy:
``POST /api/sources`` cannot be sent a ``status`` to skip sign-off.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Topic = Literal[
    "Employment", "Housing", "Wellbeing", "Funding", "Education", "Scholarships"
]
SourceFormat = Literal["PDF", "Web", "Dataset"]
SourceStatus = Literal["approved", "pending", "url_changed", "paused"]
UpdateFrequency = Literal["Annual", "Biennial", "Half-yearly", "Quarterly"]
ScanState = Literal["queued", "scanning", "complete", "attention", "skipped"]
ClaimType = Literal["Statistic", "Finding", "Caveat", "Date", "Method"]
ClaimStatus = Literal["updated", "unchanged", "new", "cannot"]
Decision = Literal["inc", "skip", "check"]
"""The prototype's three extraction-screen choices: include, skip, human check."""
Verdict = Literal["approved", "edited", "flagged", "rejected"]
AuditAction = Literal[
    "included",
    "skipped",
    "marked_for_check",
    "approved",
    "edited",
    "flagged",
    "rejected",
    "note_added",
    "signed_off",
]
ExportFormat = Literal["pdf", "docx", "audit_csv", "newsletter"]

URL_PATTERN = r"^https?://\S+$"


class Model(BaseModel):
    # Attribute docstrings become field descriptions in /docs, which is where the
    # frontend team reads this contract.
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )


class Source(Model):
    id: str
    name: str
    url: str
    owner: str
    topic: Topic
    format: SourceFormat
    update_frequency: UpdateFrequency
    status: SourceStatus
    last_checked: AwareDatetime | None


class NewSource(Model):
    """A source proposed for the registry. It always starts as ``pending``."""

    name: str = Field(min_length=1)
    url: str = Field(pattern=URL_PATTERN)
    owner: str = Field(min_length=1)
    topic: Topic
    format: SourceFormat
    update_frequency: UpdateFrequency


class SourceUpdate(Model):
    status: Literal["approved", "paused"]
    """``approved`` approves a pending source, resumes a paused one or confirms a
    changed URL; ``paused`` pauses an approved one."""
    url: str | None = Field(default=None, pattern=URL_PATTERN)
    """Only when confirming a changed URL: the address being confirmed."""


class RunSource(Model):
    source_id: str
    name: str
    format: SourceFormat
    state: ScanState
    detail: str


class Run(Model):
    run_id: str
    state: Literal["scanning", "complete"]
    sources: list[RunSource]


class NewRun(Model):
    source_ids: list[str] = Field(min_length=1)


class Claim(Model):
    id: int
    type: ClaimType
    status: ClaimStatus
    title: str
    src: str
    page: str | None
    """``updater.citations.render_anchor()`` output; null with no anchor."""
    before: str | None
    """Last year's value; null when last year's report did not have it."""
    after: str | None
    """This year's value; null when nothing was retrieved."""
    conf: int = Field(ge=0, le=100)
    note: str = Field(min_length=1)
    """The comparison rationale. Never empty, ``unchanged`` included."""
    section: str
    lastYear: str
    decision: Decision | None = None


class DecisionRequest(Model):
    decision: Decision


class Review(Model):
    verdict: Verdict | None = None
    text: str | None = None
    """The replacement sentence, once the claim has been edited."""
    note: str | None = None


class ReviewRequest(Model):
    verdict: Verdict | None = None
    text: str | None = None
    """Required with ``edited``, refused with any other verdict."""
    note: str | None = None
    """A note for the audit log. An empty string removes it."""


class DraftItem(Model):
    claim_id: int
    sent: str
    passage: str
    """Source text at the verified anchor. Never text the model wrote."""
    prev: str | None
    """Last year's sentence; null when last year's report did not have it."""
    review: Review | None = None


class AuditEntry(Model):
    t: AwareDatetime
    who: str
    action: AuditAction
    claim: int | None
    """The claim acted on; null for the sign-off, which covers the report."""


class Counts(Model):
    approved: int
    edited: int
    flagged: int
    rejected: int
    cannot: int


class ReportItem(Model):
    claim_id: int
    text: str
    cite: int
    """The claim's position in the draft, as the prototype numbers citations."""


class ReportSection(Model):
    section: str
    items: list[ReportItem]


class Publish(Model):
    run_id: str
    counts: Counts
    unresolved: int
    """Claims in the draft with no verdict yet."""
    blockers: list[str]
    """Why sign-off would be refused right now; empty when it would not be."""
    signed_off_by: str | None
    signed_off_at: AwareDatetime | None
    title: str
    sections: list[ReportSection]
    """Approved and edited claims only, grouped by section."""


class PriorReport(Model):
    filename: str
    media_type: str
    size: int


class SummaryCard(Model):
    label: str
    percent: int = Field(ge=0, le=100)
    detail: str


class ActivityItem(Model):
    ico: str
    title: str
    detail: str
    when: str


class Notification(Model):
    id: int
    kind: Literal["review", "scan", "source", "system"]
    ico: str
    when: Literal["today", "yesterday", "earlier"]
    t: str
    title: str
    unread: bool
    body: str
    acts: list[list[str]]
    """``[label, target]`` or ``[label, target, style]``, as in the prototype."""


class ScanSettings(Model):
    frequency: Literal["weekly", "fortnightly", "monthly", "manual"]
    run_on: Literal[
        "Monday, 9:00 am", "Monday, 6:00 am", "Wednesday, 9:00 am", "Friday, 5:00 pm"
    ]
    topics: list[Topic]
    retry_blocked: bool
    skip_unchanged: bool
    paused: bool


class Thresholds(Model):
    flag_below: Literal[60, 70, 80] | None
    """Confidence below which a finding is marked for attention; null: never."""
    change_sensitivity: Literal["balanced", "major", "every"]


class Policies(Model):
    """Fixed by policy. Readable through the API, never writable through it."""

    only_cite_approved: bool
    staff_sign_off: bool


class Settings(Model):
    scan: ScanSettings
    thresholds: Thresholds
    policies: Policies
