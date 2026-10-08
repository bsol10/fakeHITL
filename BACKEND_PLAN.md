# Backend build plan

Goal: a backend the approved dashboard prototype can run on, built on top of the
existing `updater` package. Steps 0 to 2 (registry, ingestion, chunking, dense
retrieval) are done: 190 tests pass, 11 skip (tokenizer or client documents not
present), 1 fails on Linux only (see phase 11).

**How to use this file.** One phase at a time. Read the ground rules below, then
the phase. Review, run the checks listed under the phase, commit, and only then
start the next phase. Never combine phases in one change: each phase's review is
where its mistakes get caught.

**Before phase 0:** copy the prototype into the repo so it can be read:

```
mkdir -p docs/frontend
cp ~/Downloads/asil-dashboard-DRAFT-FINAL.html docs/frontend/
```

---

## Ground rules (every phase)

Read `DESIGN.md`, `BUILD_SPEC.md` and this plan before starting a phase.

- The client approved the dashboard prototype in
  `docs/frontend/asil-dashboard-DRAFT-FINAL.html`. It is the source of truth for
  every data shape the API returns. It currently makes no network calls; all of
  its data is hardcoded in its `<script>`.
- The `updater` package already provides the registry, ingestion (PDF, HTML,
  DOCX incl. tables, XLSX, CSV), chunking, embedding, SQLite + sqlite-vec storage
  and dense retrieval, with 190 passing tests.
- The five design rules in `DESIGN.md` are non-negotiable. In particular: nothing
  outside the approved registry is ever retrieved or cited, a value the sources do
  not state is null and never a placeholder, every claim shown to a reviewer
  carries a verified citation, and nothing is published without staff sign-off.
- Keep the existing conventions: stdlib dataclasses inside the package, Pydantic
  only at boundaries (API schemas, LLM output parsing), heavy dependencies behind
  optional extras, frozen provenance records.

Before writing code, list the files to be added or changed and why, and agree
them first. Keep each change reviewable. Run the full test suite before calling
a phase done.

---

## API contract

Derived from the prototype. **Use the prototype's field names verbatim in JSON** so
wiring the frontend is a find-and-replace of hardcoded arrays with `fetch` calls.
Where this table and the prototype disagree, the prototype wins: read its
`<script>` before implementing a shape.

| Screen | Endpoint | Purpose |
|---|---|---|
| Dashboard | `GET /api/summary` | The four metric cards |
| Dashboard | `GET /api/activity` | Recent activity feed |
| Header | `GET /api/notifications` | Notification drawer |
| 1 Source registry | `GET /api/sources` | Registry table |
| 1 Source registry | `POST /api/sources` | Add source; always starts as `pending` |
| 1 Source registry | `PATCH /api/sources/{id}` | Approve, pause, confirm a changed URL |
| 2 Source scan | `POST /api/runs` | Start a run over chosen approved sources |
| 2 Source scan | `GET /api/runs/{run_id}` | Per-source progress |
| 3 Extraction | `GET /api/runs/{run_id}/claims` | Claim list |
| 3 Extraction | `PUT /api/claims/{id}/decision` | Include or exclude a claim from the draft |
| 3 Extraction | `POST /api/reports/prior` | Upload last year's report (see phase 4) |
| 4 Draft review | `GET /api/runs/{run_id}/draft` | Draft sentence, source passage, last year's sentence |
| 4 Draft review | `POST /api/claims/{id}/review` | Approve, edit, flag or reject; writes the audit log |
| 4 Draft review | `GET /api/runs/{run_id}/audit` | Audit trail |
| 5 Sign-off | `GET /api/runs/{run_id}/publish` | Counts and report preview |
| 5 Sign-off | `POST /api/runs/{run_id}/sign-off` | Staff sign-off (rule 5) |
| 5 Sign-off | `GET /api/runs/{run_id}/export?format=` | `pdf`, `docx`, `audit_csv`, `newsletter` |
| Settings | `GET /api/settings`, `PUT /api/settings` | Schedule, topics, thresholds; policies read-only |

### Shapes

Source (registry row). `status` is one of `approved`, `pending`, `url_changed`,
`paused`. `format` is one of `PDF`, `Web`, `Dataset`.

```json
{ "id": "youth-employment-outlook-2026", "name": "Youth Employment Outlook 2026",
  "url": "https://abs.gov.au/...", "owner": "J. Ferreira", "topic": "Employment",
  "format": "PDF", "update_frequency": "Annual", "status": "approved",
  "last_checked": "2026-09-13T09:00:00+10:00" }
```

Run. Per-source `state` is one of `queued`, `scanning`, `complete`, `attention`,
`skipped`. `detail` carries the prototype's wording: "3 changes found", "No change
since last scan", "Login wall, skipped after 1 retry", "Page moved, followed
redirect. Confirm new URL", "PDF layout changed, re-extracted with OCR",
"New edition published, 4 changes found".

```json
{ "run_id": "r_2026_09_16_01", "state": "scanning",
  "sources": [ { "source_id": "...", "name": "...", "format": "PDF",
                 "state": "complete", "detail": "3 changes found" } ] }
```

Claim. `type` is one of `Statistic`, `Finding`, `Caveat`, `Date`, `Method`.
`status` is one of `updated`, `unchanged`, `new`, `cannot`. `page` is
`updater.citations.render_anchor()` output. `conf` is 0 to 100. `note` is the
comparison rationale and is never empty.

```json
{ "id": 1, "type": "Statistic", "status": "updated",
  "title": "Youth unemployment rate in Western Sydney",
  "src": "Youth Employment Outlook 2026", "page": "p. 14",
  "before": "11.4%", "after": "13.1%", "conf": 92,
  "note": "A real change. Shifts under 0.3 points are treated as noise.",
  "section": "Employment", "lastYear": "2025 report" }
```

Draft item. `passage` is sliced from the source at the verified anchor offsets.
It is never text the model wrote.

```json
{ "claim_id": 1, "sent": "Youth unemployment in Western Sydney rose to 13.1%...",
  "passage": "youth unemployment across Western Sydney LGAs reached 13.1 per cent...",
  "prev": "Youth unemployment in Western Sydney sat at 11.4% in 2025..." }
```

Review and audit. `verdict` is one of `approved`, `edited`, `flagged`, `rejected`.

```json
{ "verdict": "edited", "text": "replacement sentence if edited", "note": "optional" }
{ "t": "2026-09-16T10:24:00+10:00", "who": "Kathleen D.", "action": "approved", "claim": 1 }
```

---

## Phase 0: Contract-first mock API

The fastest win. It unblocks the frontend team before any hard backend work lands.

```
Build a FastAPI app in src/updater/api/ that serves every endpoint in the API
contract in BACKEND_PLAN.md, backed by fixture data.

- Do not retype the fixtures. Extract them programmatically from
  docs/frontend/asil-dashboard-DRAFT-FINAL.html: the `claims`, `draftText` and
  `outcomes` arrays in its <script>, and the source registry rows in its markup.
  Save them as JSON under src/updater/api/fixtures/ and commit the extraction
  script, so the fixtures can be regenerated if the prototype changes.
- Field names in responses must match the prototype exactly.
- Scan progress: POST /api/runs returns immediately, and GET /api/runs/{id}
  advances one source per poll, so the frontend can build its progress view.
- Reviews and decisions are held in memory for now. Every review writes an
  audit entry.
- Enforce the policies even in the mock: POST /api/sources always creates
  status "pending", a run refuses any source that is not "approved", and
  sign-off is refused while any claim is "flagged". Read buildPublish in the
  prototype for its exact unresolved rule and match it.
- Settings: the two policy flags are read-only; PUT must reject changes to them.
- Enable CORS for local development. Add an `api` optional extra
  (fastapi, uvicorn) and an `updater serve` CLI command.
- Tests with FastAPI's TestClient: every endpoint returns the prototype's
  shapes, a pending source cannot be scanned, sign-off with a flagged claim
  is refused, and policy settings cannot be changed.
```

**Check:** `uv run updater serve`, open `/docs`, and call each endpoint. Compare one
claim's JSON to the prototype's `claims[0]` field by field.

---

## Phase 1: Persistence and registry lifecycle

```
Replace the mock's in-memory state with SQLite, in the same database file the
index already uses, behind a store module with its own schema version.

- Tables: runs, run_sources, prior_claims, claims, decisions, reviews, audit,
  settings.
- Extend the registry with the fields the prototype shows: status
  (approved / pending / url_changed / paused), last_checked, update_frequency,
  format. registry.yaml stays the reviewed source of truth for approved
  sources. Status changes made through the API persist in SQLite and never
  silently rewrite the YAML.
- The audit table is append-only: no update or delete path exists in code.
- Keep the API responses byte-identical to phase 0, and keep phase 0's tests
  passing unchanged. That is the acceptance test for this phase.
```

**Check:** restart the server mid-review and confirm decisions and audit entries survive.

---

## Phase 2: Hybrid retrieval

```
Add sparse retrieval and fuse it with the existing dense retriever.

- SQLite FTS5 as an external-content table in the same database file,
  populated in the same transaction that writes each vector.
- The default unicode61 tokenizer splits "109,860" at the comma. Send numeric
  queries as an FTS5 phrase, and quote every user token, since FTS5 reserves
  AND, OR, NOT, NEAR, *, ^ and :.
- Fuse with reciprocal rank fusion, k = 60, over each retriever's top 50.
  Do not normalise and add scores.
- Keep RetrievalResult's shape, and record both input ranks on each result.
- Tests: "109,860" returns a chunk containing that figure at rank 1 (this
  currently fails), a prose query is not regressed, and a query containing
  reserved words does not raise.
```

**Check:** `uv run updater search "109,860"` ranks the right chunk first.

---

## Phase 3: Fetching web sources and OCR

Most of the prototype's sources are URLs, and nothing fetches yet. OCR is SCRUM-21.

```
Implement the fetch stage from BUILD_SPEC.md for sources whose url_or_path is a
URL, under a `fetch` optional extra (httpx).

- Fetch only approved registry sources. Respect robots.txt, send a descriptive
  user agent, rate-limit per domain, and set timeouts.
- Change detection hashes the normalised extracted text, not the raw bytes, so
  a re-exported but identical PDF reads as unchanged.
- Map outcomes to the prototype's per-source states and detail wording:
  unchanged; changed (with a count of changed passages); login wall or
  401/403 -> attention, one retry if settings allow; a redirect to a new
  location -> set status url_changed and never follow it silently on later
  runs until a person confirms; new edition detected -> changed.
- Snapshots: keep the last fetched copy, so a source that later fails can be
  reported as "last saved copy is N days old", which the prototype shows.
- OCR (SCRUM-21), under an `ocr` optional extra: when a PDF page has no
  text layer, OCR it, keep the PageLocator, and record in provenance that the
  text came from OCR. The existing quality gate still decides whether the
  result is usable.
- Wire this into POST /api/runs so the scan screen shows real progress, and
  update last_checked.
- Tests with a local HTTP fixture server: unchanged, changed, 403, a redirect,
  and a scanned PDF. No test may touch the real internet.
```

**Check:** register one real public source, run a scan twice, and confirm the second
run says "No change since last scan".

---

## Phase 4: Loading last year's report

Every claim compares against last year's report, and nothing loads it yet. This is the
home for SCRUM-22's drag-and-drop. The frontend team needs to add a drop zone for it
at "Start research update".

```
Implement POST /api/reports/prior: upload last year's report (DOCX or PDF).

- Validate it with the existing content sniffing, and read it with the
  existing adapters, so every prior claim keeps an anchor into the old report.
- Split it into prior claims, each with the section heading it sits under
  (from DOCX heading styles, or PDF outline or font size). Section becomes the
  claim's `section`; the report title becomes `lastYear`.
- The prior report is the document being updated, not evidence. Store it
  separately from the registry, and never let it be cited as a source for a
  new finding.
- Tests: section mapping from a DOCX fixture with headings, and an attempt to
  cite the prior report as evidence is refused.
```

---

## Phase 5: Extraction with verification

```
Implement src/updater/extract/ to the contract in its stub docstring.

- Put the model behind a small provider interface. Implement Gemini first
  (the group's existing parser uses it, via LlamaIndex), configured from .env,
  and raise a clear error if the key is missing; never return an empty result
  for a configuration error. The model id is configuration, not code.
- The model may author only: the finding text, a type, a quoted span, the
  id of the passage it quoted, and nullable statistic, date, caveat and method
  fields. It may never author a source, a citation or a page reference.
- Verification is a pure function in its own module that does not import the
  provider: locate the quoted span in the named passage (exact match, then
  whitespace-flexible only), and return locator-relative anchor offsets.
  Anchor offsets index into the locator unit, not the passage: a hit at index
  i in passage.text sits at passage.anchor.char_start + i. Pin this with a
  test whose passage has a non-zero char_start.
- A finding whose quote does not verify is discarded and counted. The count
  must reach the API.
- Tests, all runnable with no API key: a fabricated quote is discarded, a
  one-digit near miss is discarded, the displayed passage equals the document's
  characters, a missing statistic stays null, and the offset test above.
```

**Check:** run it once against a real ASIL document and read every quote against
the source yourself. This is the phase where you find out whether the project works.

---

## Phase 6: Comparison

```
Implement src/updater/compare/ to the contract in its stub docstring, producing
the prototype's claim objects.

- Match each prior claim to new evidence through hybrid retrieval, then
  classify it as updated, unchanged, new or cannot. `cannot` is the default
  whenever evidence is thin, never a fallback after guessing.
- Numeric changes below a noise threshold count as unchanged. Make the
  threshold a named constant, and put its reasoning in the rationale, matching
  the prototype's note wording.
- Every claim carries a non-empty `note` rationale, unchanged included.
- `conf` comes from the verified evidence, and claims below the settings
  threshold ("Flag findings below") are marked for attention.
- A source that is paused, or whose last copy is stale, yields `cannot`, with a
  note saying why (the prototype's claim 4 is the model).
- Tests: a figure that moves within the noise threshold reads unchanged, thin
  retrieval yields cannot, and every claim has a rationale.
```

---

## Phase 7: Draft, review and audit

```
Implement src/updater/draft/ and connect it to the review endpoints.

- For each included claim: a draft sentence (`sent`), the verified source
  passage (`passage`), and last year's sentence (`prev`, from the prior
  report).
- A citation linter rejects any draft sentence containing a figure that does
  not appear in its verified passage. Write its definition of a material
  sentence in a docstring.
- Review verdicts write append-only audit entries that record who acted and
  when. An edited sentence is re-linted.
- Sign-off is refused while the prototype's unresolved condition holds.
  Nothing is ever published automatically.
- Tests: an uncited figure fails the linter, an edit is re-linted, and
  sign-off with an unresolved item is refused.
```

---

## Phase 8: Exports

```
Implement GET /api/runs/{run_id}/export for docx and audit_csv first, then pdf,
then newsletter.

- The report contains approved and edited claims only, grouped by section, with
  numbered citations as in the prototype's publish preview (buildPublish).
- Exports are only available after sign-off, except the audit CSV, which is
  always available.
- The newsletter summary is generated from approved text only, and every figure
  in it must also appear in the signed-off report.
- Tests: an export before sign-off is refused, a rejected claim never appears,
  and the citation numbers match the preview.
```

---

## Phase 9: The real pipeline behind the API

```
Replace the remaining fixture-backed endpoints with the real pipeline, one
endpoint at a time, keeping phase 0's contract tests passing throughout.

- A run executes as a background job: fetch -> ingest -> index -> retrieve ->
  extract and verify -> compare. Per-source progress is visible through
  GET /api/runs/{id}, and a crash in one source marks it attention without
  failing the run.
- Keep the fixtures as a demo mode (`updater serve --demo`) for presentations.
- End-to-end test: two local fixture sources, one prior report and a stubbed
  model produce claims, a draft and a docx export.
```

---

## Phase 10: Dashboard, settings, scheduler, notifications

```
Implement /api/summary, /api/activity, /api/notifications and the settings
behaviour.

- Read the dashboard markup in the prototype for the exact meaning of each of
  the four metric cards before computing them. Do not invent a metric.
- Activity and notifications derive from runs and the audit log, not from a
  separate store that can drift.
- Scheduler: honour the scan schedule setting (weekly, fortnightly, monthly,
  manual), plus "skip unchanged sources" and "retry blocked sources".
```

---

## Phase 11: Evaluation and QA (SCRUM-23)

```
Build src/updater/eval/ to the contract in its stub, and harden the suite.

- Citation fidelity sampler: 30 draft claims to a CSV for manual verdicts.
  The target is above 95%.
- Abstention test: remove a source from the registry, re-run, and assert that
  every dependent claim becomes `cannot`. This runs on every change.
- Comparison accuracy on 20 hand-labelled claims.
- Fix tests/test_registry.py::test_path_matching_ignores_case_as_windows_does
  so it is skipped off Windows: it asserts Windows path semantics and fails,
  correctly, on Linux.
- Add CI that runs the suite on Linux and Windows.
```

---

## Working notes

- If one phase produces more than about 400 lines in a single change, revert and
  split it.
- Record every decision in the decisions log in DESIGN.md, including ones later
  reversed.
- After every phase run the full suite, not just the new tests. The provenance
  invariants catch breakage three layers away.
