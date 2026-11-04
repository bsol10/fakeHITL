"""The state behind the API: the prototype's content, and what people do with it.

The state holds **one research update**: one claim set, one set of decisions and
reviews, one sign-off. A run id selects scan progress and nothing else, as in the
prototype. Claims become per run when the pipeline produces them.

Two kinds of data, kept apart:

- *The prototype's content*: claims, draft text, scan outcomes, dashboard cards,
  notifications and the registry's base list. Read from the fixtures and never
  written anywhere: the prototype's invented passages have no anchor, and must
  not enter the database that holds the real, anchored chunks (design rule 1).
- *What people and runs create*: proposed sources and status changes, runs,
  decisions, reviews, the audit log and settings. Kept in
  :class:`updater.state.StateStore` and read from it on every call, so a restart
  loses nothing and there is no in-memory copy to drift from the database.

The policies in :mod:`updater.api.policy` are enforced here already, so the
frontend is built against the refusals it will meet.

FastAPI runs plain ``def`` endpoints on a thread pool, so every method that
touches state takes the lock: two overlapping polls of one run must advance it
by two sources, not by one source twice.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import TypeAdapter

from updater.api import fixtures
from updater.api.policy import (
    Invalid,
    NotBuilt,
    NotFound,
    Refused,
    Unsupported,
    apply_review,
    count_unresolved,
    count_verdicts,
    draft_ids,
    refuse_policy_change,
    refuse_unapproved,
    refuse_unverified_inclusion,
    report_sections,
    sign_off_blockers,
    transition,
)
from updater.api.schemas import (
    ActivityItem,
    AuditAction,
    AuditEntry,
    Claim,
    Decision,
    DraftItem,
    ExportFormat,
    Model,
    NewSource,
    Notification,
    PriorReport,
    Publish,
    Review,
    ReviewRequest,
    Run,
    RunSource,
    ScanSettings,
    Settings,
    Source,
    SourceStatus,
    SourceUpdate,
    SummaryCard,
    Thresholds,
)
from updater.ingest import sniff_media_type
from updater.models import MediaType
from updater.state import (
    IN_MEMORY,
    ProposedSource,
    ReviewRecord,
    RunRecord,
    RunSourceRecord,
    SourceState,
    StateStore,
)

__all__ = ["MockState"]

DECISION_ACTION: dict[Decision, AuditAction] = {
    "inc": "included",
    "skip": "skipped",
    "check": "marked_for_check",
}

SourceAction = Literal[
    "source_proposed",
    "source_approved",
    "source_resumed",
    "source_url_confirmed",
    "source_paused",
]
"""Registry actions. Recorded in the same audit log as claim actions, with the
source's id, and not part of a report's review trail."""

SOURCE_ACTION: dict[tuple[SourceStatus, SourceStatus], SourceAction] = {
    ("pending", "approved"): "source_approved",
    ("paused", "approved"): "source_resumed",
    ("url_changed", "approved"): "source_url_confirmed",
    ("approved", "paused"): "source_paused",
}
"""One per status change :func:`updater.api.policy.transition` permits."""

PRIOR_REPORT_TYPES = frozenset({MediaType.DOCX, MediaType.PDF})

QUEUED = "Waiting for scan to start"
SCANNING = "Fetching and checking for changes"
"""The prototype's wording for a source that has not finished."""

M = TypeVar("M", bound=Model)
_DECISION: TypeAdapter[Decision] = TypeAdapter(Decision)


class Outcome(Model):
    """One entry of the prototype's ``outcomes``: how a scanned source ended."""

    state: Literal["complete", "attention"]
    detail: str


class DraftText(Model):
    """One entry of the prototype's ``draftText``."""

    claim_id: int
    sent: str
    passage: str
    prev: str | None


class Meta(Model):
    today: date
    reviewer: str
    """Who every action is recorded as. There is no auth (BUILD_SPEC.md
    section 7), so the API acts as the prototype's signed-in user."""
    report_title: str


def _records(model: type[M], name: str) -> list[M]:
    return [model.model_validate(item) for item in fixtures.load(name)]


def _local_now() -> datetime:
    # Seconds suit an audit trail people read; the log keeps the order of
    # entries within one second regardless.
    return datetime.now().astimezone().replace(microsecond=0)


def _claim_set() -> str:
    """What the claim ids in decisions, reviews and the audit log refer to.

    The fixtures' claims and draft sentences, by digest. A review approves a
    sentence, so a changed sentence makes a different claim set.
    """
    content = json.dumps(
        [fixtures.load("claims.json"), fixtures.load("draft.json")], sort_keys=True
    )
    return f"prototype-fixtures:{hashlib.sha256(content.encode()).hexdigest()[:16]}"


def _view(run: RunRecord) -> Run:
    return Run(
        run_id=run.run_id,
        state="complete" if run.done == len(run.sources) else "scanning",
        sources=[RunSource.model_validate(asdict(row)) for row in run.sources],
    )


def _overlay(source: Source, state: SourceState | None) -> Source:
    """A source as its base list has it, with its lifecycle applied."""
    if state is None:
        return source
    return Source.model_validate(
        {
            **source.model_dump(),
            "status": state.status,
            "url": source.url if state.url is None else state.url,
            "last_checked": (
                source.last_checked
                if state.last_checked is None
                else state.last_checked
            ),
        }
    )


@dataclass
class MockState:
    clock: Callable[[], datetime] = _local_now
    """Where "now" comes from. Injected so tests can fix the date."""
    path: Path | None = None
    """The database file; ``None`` keeps the state in memory, as the API's
    tests do. ``updater serve`` passes the index's file."""

    base_sources: list[Source] = field(init=False)
    """The registry's base list. Its statuses are overlaid from the state."""
    claims: dict[int, Claim] = field(init=False)
    outcomes: list[Outcome] = field(init=False)
    summary: list[SummaryCard] = field(init=False)
    activity: list[ActivityItem] = field(init=False)
    notifications: list[Notification] = field(init=False)
    draft_text: dict[int, DraftText] = field(init=False)
    meta: Meta = field(init=False)
    default_settings: Settings = field(init=False)
    """Scan and thresholds until somebody changes them. The policies, always:
    they are never written to the database, so nothing stored can change them."""
    _store: StateStore = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.base_sources = _records(Source, "sources.json")
        self.claims = {c.id: c for c in _records(Claim, "claims.json")}
        self.outcomes = _records(Outcome, "outcomes.json")
        self.summary = _records(SummaryCard, "summary.json")
        self.activity = _records(ActivityItem, "activity.json")
        self.notifications = _records(Notification, "notifications.json")
        self.draft_text = {d.claim_id: d for d in _records(DraftText, "draft.json")}
        self.meta = Meta.model_validate(fixtures.load("meta.json"))
        self.default_settings = Settings.model_validate(fixtures.load("settings.json"))

        # Every draftable claim needs its verified passage. The prototype's
        # fallback would show the reviewer the claim's own text as the source's.
        missing = [
            c.id
            for c in self.claims.values()
            if c.status != "cannot" and c.id not in self.draft_text
        ]
        if missing:
            raise ValueError(f"claims {missing} can be drafted but have no passage")

        self._store = StateStore(
            IN_MEMORY if self.path is None else self.path, claim_set=_claim_set()
        )

    def close(self) -> None:
        self._store.close()

    # 1 Source registry

    def sources(self) -> list[Source]:
        """The base list, then proposed sources, each with its lifecycle."""
        with self._lock:
            return list(self._sources().values())

    def add_source(self, new: NewSource) -> Source:
        """Propose a source. It starts ``pending``, whoever proposed it."""
        with self._lock:
            source_id = re.sub(r"[^a-z0-9]+", "-", new.name.lower()).strip("-")
            if not source_id:
                raise Invalid("A source's name needs at least one letter or digit.")
            existing = self._sources()
            if source_id in existing:
                raise Refused(f"{existing[source_id].name} is already in the registry.")
            with self._store.transaction():
                self._store.propose_source(
                    ProposedSource(id=source_id, **new.model_dump())
                )
                self._store.set_source_status(source_id, "pending")
                self._log("source_proposed", source_id=source_id)
            return self._sources()[source_id]

    def update_source(self, source_id: str, update: SourceUpdate) -> Source:
        """Approve, pause, resume or confirm a URL. Every change is audited:
        who approved what is a rule 3 question."""
        with self._lock:
            current = self._sources().get(source_id)
            if current is None:
                raise NotFound(f"No such source: {source_id}.")
            changed = transition(current, update)
            action = SOURCE_ACTION[(current.status, changed.status)]
            with self._store.transaction():
                self._store.set_source_status(source_id, changed.status, url=update.url)
                self._log(
                    action,
                    source_id=source_id,
                    # Which address was confirmed is the point of confirming it.
                    detail=changed.url if action == "source_url_confirmed" else None,
                )
            return self._sources()[source_id]

    # 2 Source scan

    def start_run(self, source_ids: Sequence[str]) -> Run:
        """Queue a scan of the named sources and start on the first.

        Refused outright if any source is unknown, listed twice or not approved.
        """
        with self._lock:
            sources = self._sources()
            unknown = [i for i in source_ids if i not in sources]
            if unknown:
                raise NotFound(f"No such source: {', '.join(unknown)}.")
            if len(set(source_ids)) != len(source_ids):
                raise Invalid("A source is listed more than once.")
            chosen = [sources[i] for i in source_ids]
            refuse_unapproved(chosen)

            rows = [
                RunSourceRecord(
                    source_id=source.id,
                    name=source.name,
                    format=source.format,
                    state="queued",
                    detail=QUEUED,
                )
                for source in chosen
            ]
            rows[0] = replace(rows[0], state="scanning", detail=SCANNING)
            run = RunRecord(
                run_id=self._next_run_id(),
                started_at=self.clock(),
                done=0,
                sources=tuple(rows),
            )
            self._store.add_run(run)
            return _view(run)

    def poll_run(self, run_id: str) -> Run:
        """The run's progress, after advancing it by one source.

        Deliberately not idempotent: each poll finishes the source being
        scanned and starts the next, so the frontend can build its progress
        view against something that moves. Outcomes are dealt from the
        prototype's ``outcomes`` by position, as the prototype deals them.
        """
        with self._lock:
            run = self._run(run_id)
            if run.done < len(run.sources):
                rows = list(run.sources)
                outcome = self.outcomes[run.done % len(self.outcomes)]
                rows[run.done] = replace(
                    rows[run.done], state=outcome.state, detail=outcome.detail
                )
                done = run.done + 1
                if done < len(rows):
                    rows[done] = replace(rows[done], state="scanning", detail=SCANNING)
                run = replace(run, done=done, sources=tuple(rows))
                self._store.update_run(run)
            return _view(run)

    # 3 Extraction

    def claims_for(self, run_id: str) -> list[Claim]:
        with self._lock:
            self._run(run_id)
            decisions = self._decisions()
            return [
                claim.model_copy(update={"decision": decisions.get(claim.id)})
                for claim in self.claims.values()
            ]

    def decide(self, claim_id: int, decision: Decision) -> Claim:
        with self._lock:
            claim = self._claim(claim_id)
            self._refuse_once_signed_off()
            refuse_unverified_inclusion(claim, decision)
            with self._store.transaction():
                self._store.set_decision(claim_id, decision)
                self._log(DECISION_ACTION[decision], claim_id=claim_id)
            return claim.model_copy(update={"decision": decision})

    def receive_prior_report(self, filename: str, data: bytes) -> PriorReport:
        """Check last year's report is a kind the adapters can read.

        Judged by content, with the same sniffing ingest uses. Nothing is parsed
        or kept yet: splitting it into prior claims is phase 4.
        """
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "upload"
            path.write_bytes(data)
            media_type = sniff_media_type(path)
        if media_type not in PRIOR_REPORT_TYPES:
            raise Unsupported(
                "Last year's report must be a Word document or a PDF; "
                f"{filename} is {media_type}."
            )
        return PriorReport(filename=filename, media_type=media_type, size=len(data))

    # 4 Draft review

    def draft(self, run_id: str) -> list[DraftItem]:
        with self._lock:
            self._run(run_id)
            return self._draft()

    def review(self, claim_id: int, request: ReviewRequest) -> DraftItem:
        """Record a reviewer's verdict or note. Every one is audited."""
        with self._lock:
            self._claim(claim_id)
            self._refuse_once_signed_off()
            if claim_id not in self._draft_ids():
                raise Refused(
                    f"Claim {claim_id} is not in the draft. Include it on the "
                    "Extraction screen first."
                )
            review, actions = apply_review(self._reviews().get(claim_id), request)
            with self._store.transaction():
                self._store.set_review(
                    claim_id,
                    None if review is None else ReviewRecord(**review.model_dump()),
                )
                for action in actions:
                    self._log(action, claim_id=claim_id)
            return self._draft_item(claim_id, self._reviews())

    def audit(self, run_id: str) -> list[AuditEntry]:
        """Newest first, as the prototype shows it."""
        with self._lock:
            self._run(run_id)
            return self._review_trail()[::-1]

    # 5 Sign-off

    def publish(self, run_id: str) -> Publish:
        with self._lock:
            self._run(run_id)
            return self._publish(run_id)

    def sign_off(self, run_id: str) -> Publish:
        """Record a person's sign-off (design rule 5).

        Publishes nothing. The system produces drafts; sign-off is what makes
        the report exportable, and the export is a person's to send.
        """
        with self._lock:
            self._run(run_id)
            blockers = sign_off_blockers(
                self._draft_ids(),
                self._reviews(),
                signed_off=self._signed_off() is not None,
            )
            if blockers:
                raise Refused(" ".join(blockers))
            self._log("signed_off")
            return self._publish(run_id)

    def export(self, run_id: str, fmt: ExportFormat) -> str:
        """The audit log as CSV, oldest first. Report formats arrive in phase 8."""
        with self._lock:
            self._run(run_id)
            if fmt != "audit_csv":
                if self._signed_off() is None:
                    raise Refused(
                        "The report can be exported after sign-off. The audit "
                        "log CSV is always available."
                    )
                raise NotBuilt(f"The {fmt} export arrives in phase 8.")
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["t", "who", "action", "claim"])
            for entry in self._review_trail():
                claim = "" if entry.claim is None else entry.claim
                writer.writerow([entry.t.isoformat(), entry.who, entry.action, claim])
            return out.getvalue()

    # Settings

    def settings(self) -> Settings:
        with self._lock:
            return self._settings()

    def update_settings(self, requested: Settings) -> Settings:
        with self._lock:
            refuse_policy_change(self.default_settings.policies, requested.policies)
            with self._store.transaction():
                self._store.set_setting("scan", requested.scan.model_dump_json())
                self._store.set_setting(
                    "thresholds", requested.thresholds.model_dump_json()
                )
            return self._settings()

    def _log(
        self,
        action: AuditAction | SourceAction,
        *,
        claim_id: int | None = None,
        source_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._store.append_audit(
            t=self.clock(),
            who=self.meta.reviewer,
            action=action,
            claim_id=claim_id,
            source_id=source_id,
            detail=detail,
        )

    def _review_trail(self) -> list[AuditEntry]:
        """Claim actions and the sign-off, oldest first. Registry actions share
        the log but name a source, and are not part of a report's trail."""
        return [
            AuditEntry.model_validate(
                {"t": e.t, "who": e.who, "action": e.action, "claim": e.claim_id}
            )
            for e in self._store.audit()
            if e.source_id is None
        ]

    def _signed_off(self) -> tuple[str, datetime] | None:
        """Who signed off and when: read from the audit log, which cannot be
        edited, rather than from a flag that could be."""
        for entry in reversed(self._store.audit()):
            if entry.action == "signed_off":
                return entry.who, entry.t
        return None

    def _refuse_once_signed_off(self) -> None:
        if self._signed_off() is not None:
            raise Refused(
                "The report is signed off and locked. Every decision stays in "
                "the audit log."
            )

    def _sources(self) -> dict[str, Source]:
        states = self._store.source_states()
        proposed = [
            Source.model_validate(
                {**asdict(p), "status": "pending", "last_checked": None}
            )
            for p in self._store.proposed_sources()
        ]
        return {
            source.id: _overlay(source, states.get(source.id))
            for source in [*self.base_sources, *proposed]
        }

    def _decisions(self) -> dict[int, Decision]:
        return {
            claim_id: _DECISION.validate_python(decision)
            for claim_id, decision in self._store.decisions().items()
        }

    def _reviews(self) -> dict[int, Review]:
        return {
            claim_id: Review.model_validate(asdict(review))
            for claim_id, review in self._store.reviews().items()
        }

    def _settings(self) -> Settings:
        scan = self._store.setting("scan")
        thresholds = self._store.setting("thresholds")
        defaults = self.default_settings
        return Settings(
            scan=defaults.scan
            if scan is None
            else ScanSettings.model_validate_json(scan),
            thresholds=(
                defaults.thresholds
                if thresholds is None
                else Thresholds.model_validate_json(thresholds)
            ),
            policies=defaults.policies,
        )

    def _draft_ids(self) -> list[int]:
        return draft_ids(self.claims.values(), self._decisions())

    def _draft_item(self, claim_id: int, reviews: dict[int, Review]) -> DraftItem:
        return DraftItem(
            **self.draft_text[claim_id].model_dump(), review=reviews.get(claim_id)
        )

    def _draft(self) -> list[DraftItem]:
        reviews = self._reviews()
        return [self._draft_item(claim_id, reviews) for claim_id in self._draft_ids()]

    def _publish(self, run_id: str) -> Publish:
        draft = self._draft()
        ids = [item.claim_id for item in draft]
        reviews = self._reviews()
        signed_off = self._signed_off()
        by, at = signed_off or (None, None)
        return Publish(
            run_id=run_id,
            counts=count_verdicts(ids, reviews, self.claims.values()),
            unresolved=count_unresolved(ids, reviews),
            blockers=sign_off_blockers(ids, reviews, signed_off=signed_off is not None),
            signed_off_by=by,
            signed_off_at=at,
            title=self.meta.report_title,
            sections=report_sections(draft, self.claims),
        )

    def _claim(self, claim_id: int) -> Claim:
        if claim_id not in self.claims:
            raise NotFound(f"No such claim: {claim_id}.")
        return self.claims[claim_id]

    def _run(self, run_id: str) -> RunRecord:
        run = self._store.run(run_id)
        if run is None:
            raise NotFound(f"No such run: {run_id}.")
        return run

    def _next_run_id(self) -> str:
        stem = f"r_{self.clock():%Y_%m_%d}"
        taken = sum(1 for run_id in self._store.run_ids() if run_id.startswith(stem))
        return f"{stem}_{taken + 1:02d}"
