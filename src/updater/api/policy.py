"""The rules the API enforces, as plain functions over the wire models.

Kept apart from the state that holds the data, because the state is a phase 0
stand-in and these are not. Phase 1 replaces the in-memory store with SQLite;
the rules carry over unchanged, and so do the tests that pin them.

A refusal is an exception, never a quietly filtered result. A scan that drops
the one unapproved source it was asked for, and reports success on the rest,
is how unapproved material ends up treated as checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from updater.api.schemas import (
    AuditAction,
    Claim,
    Counts,
    Decision,
    DraftItem,
    Policies,
    ReportItem,
    ReportSection,
    Review,
    ReviewRequest,
    Source,
    SourceStatus,
    SourceUpdate,
    Verdict,
)

__all__ = [
    "IN_REPORT",
    "STATUS_LABEL",
    "Invalid",
    "NotBuilt",
    "NotFound",
    "Refused",
    "Unsupported",
    "apply_review",
    "count_unresolved",
    "count_verdicts",
    "draft_ids",
    "refuse_policy_change",
    "refuse_unapproved",
    "refuse_unverified_inclusion",
    "report_sections",
    "sign_off_blockers",
    "transition",
]

IN_REPORT: frozenset[Verdict | None] = frozenset({"approved", "edited"})
"""The verdicts that put a claim in the report. Flagged and rejected do not."""

STATUS_LABEL: dict[SourceStatus, str] = {
    "approved": "approved",
    "pending": "pending sign-off",
    "url_changed": "URL changed, not yet confirmed",
    "paused": "paused",
}
"""Status wording for messages staff will read, as the prototype words it."""


class PolicyError(Exception):
    """Base for refusals. The message is written for ASIL staff to read."""


class NotFound(PolicyError):
    """The request names something that does not exist."""


class Refused(PolicyError):
    """The request is well formed and a policy forbids it."""


class Invalid(PolicyError):
    """The request is self-contradictory or incomplete."""


class Unsupported(PolicyError):
    """An upload of a kind that is not accepted."""


class NotBuilt(PolicyError):
    """Part of the contract that a later phase implements."""


def refuse_unapproved(sources: Iterable[Source]) -> None:
    """Refuse a scan that includes any source not currently approved.

    Design rule 3. The whole request is refused and every offending source is
    named, so the person sees all of them at once rather than one per attempt.
    """
    blocked = [source for source in sources if source.status != "approved"]
    if blocked:
        named = "; ".join(f"{s.name} ({STATUS_LABEL[s.status]})" for s in blocked)
        raise Refused(
            "Only approved sources can be scanned (design rule 3). "
            f"Not approved: {named}."
        )


SOURCE_TRANSITIONS: frozenset[tuple[SourceStatus, SourceStatus]] = frozenset(
    {
        ("pending", "approved"),  # sign-off by a Research Lead
        ("paused", "approved"),  # resume
        ("url_changed", "approved"),  # confirm the new URL
        ("approved", "paused"),
    }
)
"""Every status change a person may make. A source starts ``pending`` and a
scan sets ``url_changed``; neither can be chosen through the API."""


def transition(source: Source, update: SourceUpdate) -> Source:
    if (source.status, update.status) not in SOURCE_TRANSITIONS:
        raise Refused(
            f"{source.name} is {STATUS_LABEL[source.status]} and cannot be set "
            f"to {update.status}."
        )
    if update.url is not None and source.status != "url_changed":
        raise Invalid("A URL is only accepted when confirming a changed URL.")
    url = update.url if update.url is not None else source.url
    return source.model_copy(update={"status": update.status, "url": url})


def refuse_unverified_inclusion(claim: Claim, decision: Decision) -> None:
    """An unverified claim cannot go into the draft (design rules 2 and 4)."""
    if decision == "inc" and claim.status == "cannot":
        raise Refused(
            "Unverified claims can't go into the draft. Fix the source and "
            "re-run the scan, or check it by hand."
        )


def draft_ids(claims: Iterable[Claim], decisions: Mapping[int, Decision]) -> list[int]:
    """The claims in the draft, in claim order: the prototype's ``buildDraft``.

    The included claims; or, until anything is included, every claim that can
    be verified, skipped ones too. That fallback is the prototype's, kept so
    its demo flow works without the extraction screen. It is safe because
    :func:`sign_off_blockers` then requires a verdict on every one of them.
    """
    claims = list(claims)
    included = [c.id for c in claims if decisions.get(c.id) == "inc"]
    return included or [c.id for c in claims if c.status != "cannot"]


def apply_review(
    current: Review | None, request: ReviewRequest
) -> tuple[Review | None, list[AuditAction]]:
    """Merge a review request into the review so far; return the audit actions.

    Prototype semantics: a verdict replaces the verdict and leaves an earlier
    edit's text in place, and a note is set, or removed by an empty one,
    independently of the verdict. Only a non-empty note is logged.
    """
    if request.verdict is None and request.note is None:
        raise Invalid("Send a verdict, a note, or both.")
    if request.verdict == "edited" and not (request.text or "").strip():
        raise Invalid("An edit needs the replacement sentence.")
    if request.verdict != "edited" and request.text is not None:
        raise Invalid("Replacement text is only accepted with the edited verdict.")

    review = current or Review()
    actions: list[AuditAction] = []
    if request.verdict is not None:
        changes: dict[str, str] = {"verdict": request.verdict}
        if request.text is not None:
            changes["text"] = request.text.strip()
        review = review.model_copy(update=changes)
        actions.append(request.verdict)
    if request.note is not None:
        note = request.note.strip()
        review = review.model_copy(update={"note": note or None})
        if note:
            actions.append("note_added")
    if review.verdict is None and review.note is None:
        return None, actions
    return review, actions


def count_verdicts(
    draft: Sequence[int], reviews: Mapping[int, Review], claims: Iterable[Claim]
) -> Counts:
    verdicts = [_verdict(reviews, claim_id) for claim_id in draft]
    return Counts(
        approved=verdicts.count("approved"),
        edited=verdicts.count("edited"),
        flagged=verdicts.count("flagged"),
        rejected=verdicts.count("rejected"),
        # Every claim, not only the draft's, as the prototype counts them.
        cannot=sum(1 for claim in claims if claim.status == "cannot"),
    )


def count_unresolved(draft: Sequence[int], reviews: Mapping[int, Review]) -> int:
    """Claims in the draft with no verdict. A note alone does not resolve one."""
    return sum(1 for claim_id in draft if _verdict(reviews, claim_id) is None)


def sign_off_blockers(
    draft: Sequence[int], reviews: Mapping[int, Review], *, signed_off: bool
) -> list[str]:
    """Why sign-off would be refused now. Empty means it would be accepted.

    The prototype's rule, matched exactly (``buildPublish`` and the publish
    button): every claim in the draft has a verdict, and at least one is
    approved or edited. A flagged claim does not block sign-off. It is left out
    of this edition, and the prototype's checklist says so. Design rule 5 is
    why the rule exists: nothing reaches the report without a person's verdict.
    """
    blockers = []
    if signed_off:
        blockers.append("This report is already signed off.")
    unresolved = count_unresolved(draft, reviews)
    if unresolved:
        still = "claim still needs" if unresolved == 1 else "claims still need"
        blockers.append(f"{unresolved} {still} a decision in Draft review.")
    if not any(_verdict(reviews, claim_id) in IN_REPORT for claim_id in draft):
        blockers.append("Approve at least one claim before publishing.")
    return blockers


def report_sections(
    draft: Sequence[DraftItem], claims: Mapping[int, Claim]
) -> list[ReportSection]:
    """Approved and edited claims, grouped by section in order of appearance.

    A claim's position in the draft is its citation number, so the numbers
    match what the reviewer saw on the Draft review screen.
    """
    sections: dict[str, list[ReportItem]] = {}
    for cite, item in enumerate(draft, start=1):
        if item.review is None or item.review.verdict not in IN_REPORT:
            continue
        text = item.review.text or item.sent
        sections.setdefault(claims[item.claim_id].section, []).append(
            ReportItem(claim_id=item.claim_id, text=text, cite=cite)
        )
    return [ReportSection(section=s, items=items) for s, items in sections.items()]


POLICY_LABEL = {
    "only_cite_approved": "Only cite approved sources",
    "staff_sign_off": "Staff sign-off before publishing",
}


def refuse_policy_change(current: Policies, requested: Policies) -> None:
    changed = [
        POLICY_LABEL[name]
        for name in Policies.model_fields
        if getattr(current, name) != getattr(requested, name)
    ]
    if changed:
        verb = "is" if len(changed) == 1 else "are"
        raise Invalid(
            f"{' and '.join(changed)} {verb} fixed by policy and cannot be "
            "changed in Settings."
        )


def _verdict(reviews: Mapping[int, Review], claim_id: int) -> Verdict | None:
    review = reviews.get(claim_id)
    return review.verdict if review is not None else None
