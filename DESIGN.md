# DESIGN.md — ASIL Annual Research Update Agent

`BUILD_SPEC.md` is the contract; read the relevant section before changing
anything structural. **`BACKEND_PLAN.md` is the build order** — the API the
approved dashboard runs on, with the remaining steps folded into its phases
(hybrid retrieval is phase 2, extraction phase 5). One phase at a time, commit
between phases. Keep the decisions log at the bottom of this file current: it
is the project's memory.

## The five non-negotiable design rules

From BUILD_SPEC.md section 1. These are not tradeoffs to be balanced against
convenience — breaking one destroys the project's core value proposition. Treat
a violation as a bug.

1. **Provenance is never lost.** Every piece of text carries a `SourceAnchor`
   from the moment it is extracted until it appears in the final draft. There is
   no stage where text becomes a bare string. If you are about to pass a `str`
   where a `Passage` belongs, stop and thread the anchor through instead.

2. **Abstention over confabulation.** If evidence for a claim is not retrieved,
   emit `cannot-verify` and say so in the draft. Never fill a gap with
   plausible-sounding text. Nullable fields stay null — `Evidence.statistic`
   being nullable is load-bearing, because a model that must emit a number will
   invent one.

3. **Approved sources only.** No open web search, no model world-knowledge in
   the draft. If it isn't in the registry, it isn't evidence.

4. **Every material claim carries a citation.** A draft sentence without an
   anchor is a bug, and the test suite treats it as one.

5. **Human sign-off is mandatory.** The system produces drafts, never published
   output.

### What this rules out in practice

- Generating a plausible statistic when retrieval returned nothing.
- Dropping a `cannot-verify` claim from the draft to make it read better. A
  missing finding is more dangerous than a flagged one.
- Letting a chunk span two PDF pages, two slides, or two cell ranges. The
  anchor becomes ambiguous, which makes the citation uncheckable.
- Skipping the verbatim-span string match on extracted evidence.
- Reaching for the web, or for training knowledge, to fill a gap in the registry.

## Do not build

Auth. Multi-tenancy. A React frontend. Async job queues. Docker orchestration.
Fine-tuning. An agent framework. A chatbot interface. (BUILD_SPEC.md section 7 —
the brief explicitly says the first version is not a chatbot.)

Every hour spent on infrastructure is an hour not spent on citation quality,
which is the only thing that makes this project defensible.

## Layout

```
src/updater/
  models.py    Source, SourceAnchor, Passage, Evidence, Claim,
               ComparisonResult, the locator union, and the enums
  serde.py     locator round trips through JSON and SQLite
  citations.py locator to human-readable text; step 5 builds the draft half
  certs.py     OS trust store, for the TLS-intercepting proxy on this network
  cli.py       updater ingest / search / extract / serve
  state.py     what people and runs create: runs, decisions, reviews, the
               append-only audit log, settings, the registry's lifecycle
  registry/    approved-source registry (YAML) — rule 3 lives here
  ingest/      one extractor per media type, all returning list[Passage]
  retrieve/    tokenize, chunk, embed, store (sqlite-vec, FTS5), search, index
  extract/     evidence extraction + verbatim-span verification
    evidence.py the stage: prompt, schema, call, kept and discarded findings
    verify.py  quote to locator-relative anchor; pure, imports no provider
    model.py   the provider interface, ModelUnavailable, ModelCallFailed
    provider.py UPDATER_MODEL_PROVIDER to a model
    claude.py  Claude; the only module importing anthropic
    gemini.py  Gemini; the only module importing google-genai (extra: gemini)
  compare/     unchanged / updated / new / cannot-verify, each with a rationale
  draft/       markdown assembly with inline citations + the citation linter
  eval/        citation fidelity, abstention, comparison accuracy
  api/         FastAPI app the dashboard runs on; Pydantic stays in here
    schemas.py the wire contract, field names verbatim from the prototype
    policy.py  the rules the API enforces, as pure functions
    mock.py    the prototype's content, over the state in updater.state
    fixtures/  JSON extracted from the prototype by scripts/
front-end/     the dashboard: the approved prototype wired to the API, served
               at / by updater serve
docs/frontend/ the approved prototype itself, source of truth for the fixtures
src/backend/   the group's Gemini/LlamaIndex prototype (extra: prototype);
               not ours to edit, and excluded from ruff and mypy
tests/
```

Pipeline order: `register → fetch → extract → chunk → embed → retrieve →
extract_evidence → compare → draft → review`.

## Commands

```sh
uv sync                 # create/refresh .venv
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type check (strict; paths come from pyproject.toml)

uv sync --extra retrieve            # the ML stack: torch, sqlite-vec (~2 GB)
uv run updater ingest               # index every source in registry.yaml
uv run updater ingest --rebuild     # ... after changing the model, chunk size or schema
uv run updater search "<query>"     # hybrid retrieval, printing anchors and both ranks
uv run updater search "<query>" --retriever dense   # or sparse: one input alone

uv sync --extra retrieve --extra api --extra gemini --extra prototype
                                       # every extra: run this after any uv add/remove
uv run updater serve                   # dashboard at http://127.0.0.1:8000/, the API at
                                       # /api, docs at /docs (--no-frontend: API only);
                                       # state in the --db file, beside the index
uv run updater extract "<claim>"       # retrieve, extract, print each verified quote
                                       # and the discard count; model from .env
uv run pytest -m live                  # the paid live tests; skipped per provider
                                       # without its key

cd src/backend && uv run --extra prototype python query.py   # the group prototype
uv run python scripts/extract_prototype_fixtures.py   # after the prototype changes
```

## Status

Steps 1 and 2 complete: ingestion, the approved-source registry, chunking,
embedding, storage and hybrid retrieval. `updater ingest` takes every source in
`registry.yaml` from disk to a searchable index; `updater search "<query>"`
returns chunks with their anchors. On ASIL's current material that is 740
chunks over seven documents in about 30 seconds.

- **Registry** — `registry.yaml` at the repository root, or
  `$UPDATER_REGISTRY_PATH`. `Registry.source_for_path` and `Registry.get` are
  the rule 3 boundary: nothing else in the pipeline may mint a `Source`.
- **Ingest** — PDF, HTML, DOCX (paragraphs and tables), XLSX and CSV. **PPTX is
  the one coverage gap on ASIL's own material** and is listed in the registry
  anyway, so it reports `UnsupportedMediaType` rather than being invisible.
- **Chunk** — 400-600 word pieces, 15% overlap, never across a locator
  boundary, `require_derives_within` called on every chunk.
- **Embed and store** — `all-MiniLM-L6-v2` into SQLite with `sqlite-vec`, behind
  `uv sync --extra retrieve`. A chunk longer than the model's 256-token window
  is embedded as several windows, never truncated.
- **Retrieve** — hybrid: full-text BM25 (SQLite FTS5, same file) and dense,
  fused by reciprocal rank, ties to the better lexical rank. Dense alone cannot
  find a number; see the 2026-10-02 entries on what `109,860` returns.

Real documents go in `data/sources/` (gitignored). Two manual checks on real
files rather than on generated fixtures:

```sh
uv run python scripts/dump_passages.py data/sources --chunk   # anchors, coverage
uv run python scripts/dump_passages.py data/sources --worst 10  # the smoke alarm
```

**API (BACKEND_PLAN.md phases 0, 1 and 2 complete).** `updater serve` runs every
endpoint in the plan's contract table, behind `uv sync --extra api`. The
claims, sources and dashboard content are fixtures extracted from the approved
prototype. What people do with them is kept in SQLite (`updater.state`), in the
same file as the index, and survives a restart: proposed sources and status
changes, runs, decisions, reviews, the audit log, settings. The policies are
enforced: a proposed source starts pending, a scan refuses unapproved sources,
an unverified claim cannot enter the draft, every decision, review and source
change is audited, sign-off follows the prototype's rule, the policy settings
are read-only.

**Extraction (BACKEND_PLAN.md phase 5's core).** `updater.extract` takes a
claim and its retrieved passages to verified evidence: one call per claim,
JSON constrained by a schema, every quote located in the passage it names and
anchored there, everything else discarded and counted. Two providers behind one
interface, Claude (the default) and Gemini (`--extra gemini`), chosen by
`UPDATER_MODEL_PROVIDER`. `updater extract "<claim>"` runs it end to end. Not
yet: phase 5's check against a real ASIL document with a real key, the discard
count reaching the API (phase 9 wires the pipeline in), and neighbourhood
assembly of adjacent passages.

**Dashboard.** The approved prototype runs on the API: `front-end/index.html`,
served by `updater serve` at `/`. Every screen in the workflow reads and writes
the API; the screens with nothing behind them in the API (profile, team,
integrations and so on) are still the prototype's demo.

Next: BACKEND_PLAN.md phase 3, fetching web sources and OCR, and phase 5's
check with a real key.
`tests/test_api.py` and `tests/test_api_policies.py` still pass unchanged and
should stay that way. Still worth doing at some point: the PPTX adapter.

## Decisions log

Append to this as decisions get made. Each entry is a thing a future reader
would otherwise have to re-derive or would get wrong.

**2026-08-20 — Tooling.** uv, not poetry. Python pinned to 3.12 via
`.python-version` even though `requires-python = ">=3.11"`: the only interpreter
on this machine is 3.14, and the stack the later phases need
(`sentence-transformers` → torch, `sqlite-vec`, `ocrmypdf`) has patchier 3.14
wheel coverage. uv fetches 3.12 itself, so nothing depends on the system Python.

**2026-08-20 — Dataclasses, not Pydantic.** The spec allows either. Stdlib
dataclasses keep the model layer dependency-free. If LLM structured output later
wants Pydantic, add it at the extraction boundary rather than converting
`models.py`.

**2026-08-20 — Provenance records are frozen; `Source` is not.** An anchor that
can be edited after the fact is not provenance, so `SourceAnchor`, `Passage`,
`Evidence`, `Claim` and `ComparisonResult` are `frozen=True`. `Source` stays
mutable because the fetch stage writes `last_checked` and `last_content_hash`
back to it.

**2026-08-20 — Records are keyword-only.** Costs a few characters per call site,
buys immunity from silently passing `char_start`/`char_end` the wrong way round.
It also lets the nullable `Evidence` fields default to `None` while keeping the
spec's field order, which is what makes rule 2 the path of least resistance.

**2026-08-20 — Anchor offsets are locator-relative.** `SourceAnchor.char_start`
and `char_end` index into the extracted text of the unit the locator names (the
page, the paragraph, the cell range), not into the whole document. That is what
lets the extract stage verify a quoted span by plain string match against the
passage, and it keeps anchors valid when unrelated parts of a document shift.

**2026-08-20 — Locators carry a literal `kind` tag.** The union has to survive a
round trip through YAML, JSON and SQLite, where `isinstance` is unavailable.
`DomLocator.heading_trail` is a `tuple`, not a `list`, so frozen anchors stay
hashable.

**2026-08-20 — No `quote` field on `Evidence`.** The supporting span is
`passage.text[anchor.char_start:anchor.char_end]`. Deriving it means the quote
cannot drift out of sync with the anchor that justifies it.

**2026-08-20 — Heavy dependencies live in extras.** `ingest`, `retrieve` and
`ui` are declared but empty; each build step fills in its own with
`uv add --optional <extra> <pkg>`. A fresh clone does not pull torch.

**2026-08-20 — Default model is `claude-opus-5`** (`.env.example`). Evidence
extraction and claim comparison are accuracy-critical and low-volume. Do not
downgrade to save cost without re-running the citation-fidelity evaluation.

**2026-08-20 — `system-certs = true` under `[tool.uv]`.** This network
intercepts TLS, so uv's bundled roots reject GitHub and PyPI with
`invalid peer certificate: UnknownIssuer`. Reading the OS certificate store
picks up the proxy's CA. If a *different* tool starts failing on certificates,
this is the cause and the fix is the same knob, not a new one — `pip` wants
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`, `httpx` wants `verify=`.

**2026-08-20 — Ruff excludes `*.md`.** Ruff formats Python blocks inside
Markdown, and it wanted to reformat the `Extractor` protocol in
`BUILD_SPEC.md`. The spec is the contract; it is not ours to rewrite.

**2026-08-21 — Locators stay in `models.py`.** BUILD_SPEC.md documents them
under section 3 (ingestion), but `SourceAnchor` needs them, so they live with
the rest of the data model and `updater.ingest` imports them. Defining a second
copy in the ingest package would give the pipeline two incompatible notions of
where a citation points.

**2026-08-21 — The registry owns sniffing and the quality gate, not the
adapters.** An adapter's whole job is format to passages. Centralising means a
new adapter cannot forget to run the gate, which is the mistake that would show
up months later as a source quietly contributing nothing.

**2026-08-21 — `Extractor.media_types` is a `ClassVar`.** The spec writes
`media_types: set[str]`. It is genuinely class-level state, and mypy and ruff
(RUF012) both insist on saying so. The contract is unchanged.

**2026-08-21 — A quality-gate failure fails the whole extraction.** Dropping
just the bad passage is what BUILD_SPEC.md section 3 rules out: the missing text
reappears downstream as an absence of evidence, and the compare stage reads that
as a real finding about the world. The error names each failing passage so a
reviewer can go and look.

**2026-08-21 — The alphanumeric-ratio check is skipped below 30 characters.**
On a short fragment the ratio is dominated by one space or bracket: "(p. 12)"
scores 43% and is perfectly good text. **Known tuning risk:** a PDF page that is
mostly a numeric table can score under the 0.6 floor and fail a whole document.
If that starts happening on real ASIL sources, the fix is to exclude whitespace
from the denominator in `ingest/quality.py`, not to delete the check.

**2026-08-21 — `DomLocator.css_path` points into trafilatura's extracted tree,
not the live page.** The original DOM does not survive boilerplate removal, so
there is nothing honest to put there. It is a stable within-document ordinal
(`doc > main > p:nth-of-type(2)`), unique per passage. **The heading trail is
the durable half of a web citation** — it still reads correctly after the
publisher restyles the page, which is what usually happens between two annual
runs. Cite the trail to humans. A heading includes itself in its own trail.

**2026-08-21 — `BlockLocator.paragraph_index` counts empty paragraphs.** The
DOCX adapter skips paragraphs with no text but does not renumber, so the index
addresses the real document and a reviewer opening the file lands on the right
block. Dense renumbering still produces citations; they just point at the wrong
paragraph. There is a test pinning this.

**2026-08-21 — PDF: one passage per page, blank pages omitted.** A blank page
has nothing citable. A document that is *mostly* blank is a different thing and
raises `NeedsOCR` (mean under 100 characters per page). Page numbers are never
renumbered around the gaps.

**2026-08-21 — Extension is consulted in exactly one place.** Markdown and plain
text are byte-identical formats that no content sniffing can separate, so
`sniff.py` falls back to the suffix for that one tiebreak — after every binary
format has already been ruled out on content. Everywhere else, content wins;
there is a test for the `.txt` that is really a PDF.

**2026-08-21 — `sniff_media_type` returns a plain `str`.** `MediaType` members
are a convenience for writing the module. Letting one escape puts
`<MediaType.TXT: 'text/plain'>` into an error message meant for ASIL staff.

**2026-08-21 — `Passage.token_count` is a 4-characters-per-token estimate.**
Deliberately crude so nobody mistakes it for real counting. The chunking stage
owns that, because it is the stage with a budget to respect. Replace it there
rather than adding a tokenizer dependency to ingest.

**2026-08-21 — Ingestion dependencies are core, not an extra.** `pypdf`,
`trafilatura`, `python-docx` and `filetype` moved into `[project] dependencies`;
the project does nothing without them. Only the heavy ML stack stays behind
`--extra retrieve`. `reportlab` is a dev dependency used to generate PDF
fixtures at test time, so no binary blobs are committed.

**2026-08-21 — Tests import helpers from `tests/conftest.py`.** pytest puts the
tests directory on `sys.path`, so `from conftest import make_source` works and
mypy resolves it. The `page_locators` / `block_locators` / `dom_locators`
helpers assert the locator variant before returning it, so a test reading
`.page` cannot pass against the wrong locator type.

**2026-08-21 — The chunk-boundary rule has a callable form: `updater.provenance`.**
BUILD_SPEC.md section 4 says "chunk within locator boundaries, never across
them", and nothing in the type system can enforce it — a `Passage` built from
two pages is a well-formed `Passage` carrying a citation that is a lie.
`require_derives_within(parent, child)` is the predicate form. **The chunking
stage must call it.** Written before chunking exists on purpose: an invariant
added after the code that violates it is an invariant that gets relaxed to fit.

Ingest satisfies it by construction — `adapters/pdf.py` binds each page's own
`extract_text()` to its own page number in one comprehension and never
concatenates. `test_a_page_passage_holds_no_text_from_its_neighbours` pins it
with per-page sentinel markers, asserting *absence* of the neighbours' text.
Positive containment alone is not enough: a mutant that bled adjacent pages
together passed `test_pdf_passages_are_page_anchored` and failed only the
sentinel test.

**2026-08-23 — `data/sources/` is where documents to ingest live, and it is
gitignored.** The directory survives a clone via a committed README; the
documents do not. ASIL material is client material and does not belong in
version control. `data/` also holds the SQLite index and snapshots the later
stages will write. The ignore rule is `data/*` rather than `data/` on purpose:
git never descends into an excluded *directory*, so `!data/sources/README.md`
under a plain `data/` is silently dead. Excluding the contents leaves the
directory walkable and the negation live.

**2026-08-23 — `scripts/dump_passages.py` fabricates a `Source`, and that is
the one place allowed to.** The approved-source registry does not exist yet, so
the inspector builds a throwaway entry from the filename with `owner` and
`topic` reading "unknown (not from the registry)" rather than guessed. This is
not a rule 3 loophole: nothing the script prints becomes evidence. **When the
registry lands, the pipeline takes its `Source` from there** — do not import
`provisional_source` into anything that retrieves, extracts evidence or drafts.
The script also carries a throwaway `render_locator`; step 5 owns the real
citation renderer, and this one gets deleted rather than imported.

**2026-08-23 — The dump prints `whole unit` or `PARTIAL` per passage.** At
ingest a passage *is* the unit its locator names, so the anchor spans all of its
text. The first thing that legitimately prints `PARTIAL` will be a chunk, which
makes this the cheapest place to notice chunking writing anchors it should not.

**2026-08-23 — Console output is forced to UTF-8.** The Windows console
defaults to cp1252 and turns every curly quote and em dash in a real PDF into a
replacement character. Reading mojibake and not knowing whether the extractor or
the terminal produced it wastes a debugging session; there is one line at the
top of the script that prevents it.

**2026-08-23 — Known gap: a `.csv` sniffs as `text/plain`.** `sniff.py` has no
CSV detection, so a spreadsheet export reports `UNSUPPORTED` under the wrong
media type. Harmless now — neither adapter exists — but fix the sniffer at the
same time as the XLSX/CSV adapter, or the adapter will be registered for a type
nothing ever dispatches to.

**2026-08-23 — XLSX and CSV moved ahead of chunking.** BUILD_SPEC.md section 3
puts them in the bonus round behind PDF/HTML/DOCX. ASIL's real material turned
out to be mostly sector skills workbooks, so building retrieval before the
adapter that reads the primary source would have meant tuning chunking against
formats the client barely uses. The spec's *ordering* is a judgement about
typical nonprofit sources; the adapter design it prescribes is unchanged.

**2026-08-23 — A tabular passage is one row, and it carries its column
headers.** The decision the whole tabular layer turns on, so the reasoning is
here in full. One passage per *cell* was rejected: a lone `11845` anchored at
`B7` is a number, not evidence, and nothing can retrieve it. One passage per
*sheet* was rejected for the reason a chunk may not span two PDF pages — an
anchor naming a whole sheet is unfalsifiable, and a reviewer told "Sheet2" has
to search it, which is the work citations exist to remove. One passage per row
is what a skills workbook is actually made of, and `Sheet2!B4:F4` lands a
reviewer on the exact cells.

Serialisation is `{sheet}. {Header}: {value}; {Header}: {value}`. Three
arguments for repeating the header in every row, in ascending order of how hard
they are to argue with:

1. `North East; 12340; 11845` embeds close to nothing. The numbers have no
   meaning without their columns, and a query about apprenticeship starts has
   no lexical or semantic overlap with any of it.
2. BM25 and dense retrieval both need the *column* term present, because that
   is where the subject matter lives — the body of the row is digits.
3. **Measured:** the quality gate. A bare `5223; 1,204; 0.34; -2.1%` scores
   0.54 alphanumeric, under the 0.6 floor, and fails the *whole workbook*.
   Paired with its headers the same row scores 0.69. The tuning risk flagged in
   the 2026-08-21 entry about numeric tables is therefore already handled for
   tabular sources by the serialisation, and the whitespace-denominator fix
   named there is still the right move if a headerless numeric sheet trips it.

The cost is real and accepted: every row repeats the headers, which inflates
the index and flattens BM25's IDF for those terms within a sheet.

**2026-08-23 — The sheet name is carried into each row; the title row is not.**
The line between them is whether the text is part of the *address* or part of
the *content*. `CellLocator.sheet` already names the sheet, so repeating it in
the text asserts nothing the anchor does not — and it is often the only place
the table's subject is written down. A title in `A1` is content with its own
anchor. Splicing it into a passage anchored at `B47:F47` would make the passage
text stop corresponding to the cells it cites, which is the first step of
exactly the rot rule 1 exists to prevent. Preamble and note rows become their
own passages instead, so nothing is lost. A header is the one exception and is
different in kind: it is part of what the cell *means*, not context about it.

**2026-08-23 — The header row is found by span, not by looking for text.** Real
workbooks open with a title, a blank, a note and another blank, so the header is
rarely row 1. It cannot be found by "the row of words above the row of numbers"
either, because year columns ("2023", "2024") are numeric and region names in
the body are not. What holds is width: preamble and footnotes are narrow, the
table is wide. The header is the first row spanning at least the sheet's modal
span, with all-distinct values, and not entirely numeric.

Each of those three conditions is load-bearing, and each was added because the
one before it failed a case:

- *Span, not populated count.* A header whose third label is blank has three
  populated cells but spans four columns. Counting cells makes it look narrower
  than its own data rows, and the first data row wins the search instead.
- *All-distinct values.* A merged title propagated across the table's full width
  otherwise looks exactly as wide as the header. Copies of one string give it
  away; real column headers are never duplicates.
- *Not entirely numeric.* Otherwise a sheet with no header at all always crowns
  its first row, and row 2 gets paired against row 1 to produce `12340: 31200` —
  a column name manufactured out of data, wearing a citation that looks sound.

No header found is a real answer and returns `None`. Rule 2: an unnamed column
inside a real header falls back to "Column C", and a sheet with no header at all
gets bare values. Neither invents a name.

**2026-08-23 — Merges propagate down, never across.** A merge spanning rows
means "this label applies to each of these rows" — `A2:A4` holding a provider
name describes three data rows, and without propagation rows 3 and 4 come out as
unlabelled numbers. A merge spanning *columns* is one value: copying `A1:D1`
into four cells manufactures three cells the sheet does not have, and a title as
wide as the table then gets paired against the headers. The address kept for a
horizontal merge is its top-left cell, which is what Excel itself reports.

**2026-08-23 — Cell values are stored values, except percentages.** Applying
Excel's display format is a rendering guess that can silently change a number —
12.344 shown to two places would be cited as 12.34, and a reviewer checking that
citation is worse off than one reading the stored value. The single exception is
a per-cent format, which is not a guess: Excel *defines* it as the stored value
times 100 with a sign, so a cell holding 0.04 and displaying "4%" is written
`4%`. Without that the draft would cite 0.04 against a cell that plainly shows
4%, and the citation would read as wrong to the only person who can sign it off.

**2026-08-23 — Every sheet is read, hidden ones included.** A hidden sheet is
usually workings, but "usually" is not a basis for dropping data silently, and
silent loss is what section 3 rules out. Formulas are read as cached results
(`data_only=True`); a workbook saved by something other than Excel may have no
cache, which surfaces as empty cells rather than as a wrong number.

**2026-08-23 — A `.csv` now sniffs as `text/csv`, by consistency of field
count.** This closes the gap logged earlier the same day as a dispatch bug
waiting to happen. Consistency is the whole test, and it is what separates a
table from prose: an English paragraph has commas, but not the *same number* of
them on every line. Counting goes through `csv.reader`, not `str.count`, so a
quoted value containing the delimiter does not derail it. Comma, semicolon, tab
and pipe are tested; semicolon is what Excel writes under a European locale.
Tab-separated files also report `text/csv` — there is no TSV type in the spec
and the adapter reads whichever delimiter the sniffer found, so a second type
would buy nothing but a second dispatch key pointing at the same code. **Known
limit:** prose with a rigidly repeating comma pattern (an address list) sniffs
as CSV. The failure is visible rather than silent — bare unpaired rows, not
something that looks like a clean table.

The last sampled line is dropped **only** when the read actually hit
`SNIFF_BYTES`. Dropping it unconditionally throws away one of the two lines a
small file has, and a three-line CSV sniffs as plain text. There is a test.

**2026-08-23 — `updater.ingest.tabular` owns serialisation; the adapters own
parsing.** A table exported to CSV and the same table in a workbook must produce
identical passage text, or the retrieval index depends on which button somebody
pressed when they saved it, and two annual runs stop being comparable. There is
a test asserting the two adapters agree, on text and on cell ranges both.

**2026-08-23 — The cell_range tests assert absence, and two mutants prove it.**
Same standard as the PDF page assertions. `assert_range_matches_workbook`
reopens the workbook with a deliberately dumber reader and checks both
directions: every value at the named cells is in the passage, and no value from
the rows either side of it is. Positive containment alone would pass a reader
that smeared two rows together.

Both mutants earn their place. The off-by-one mutant shifts every row number by
one. The smear mutant runs each row together with its neighbour while leaving
the anchor alone — and it caught a real bug *in the test helper*, which had been
deriving the row number by scanning "A6:D6" for digits and getting 66, a row
that does not exist, silently reducing the neighbour check to a no-op. Every
tabular test had been passing against a check that did nothing. **That is the
argument for mutation-testing a provenance assertion rather than trusting it.**
Keep both mutants passing.

**2026-08-23 — First run against real ASIL files broke header detection twice.**
Both conditions had been derived from synthetic fixtures, and both failed on the
first real workbook. Recorded because the shape of the mistake matters more than
the fix: synthetic fixtures test the cases you already thought of.

*Width to right edge.* `FYA_vs_ASIL_Comparative_Analysis.xlsx`, sheet
"Overview": the header sits in B and C with A blank, because column A holds row
labels and has no name. That makes it one column narrower than every row under
it, so the width test skipped it and crowned the first data row. Every row below
was then captioned with a data value — `Data source: Reference period`. All rows
of a table end at the same column even when they do not start at the same one,
so the test is now the modal *right edge*. `MATRIX_SHEET` in conftest pins it.

*A sheet is not a table.* Same workbook, sheet "Salary Premiums": two tables
stacked under one tab, rows 4-9 and rows 12-31, separated by a banner row, each
with its own header. The first header was applied to both, citing `$109,860`
from the *Avg Salary Low* column as `Pay Premium (AUD/year)`. `split_tables`
now cuts a sheet at rows too narrow to belong to a table, and header detection
runs per block. `TWO_TABLE_SHEET` pins it.

Both bugs produced sound anchors, plausible sentences and the wrong column
name. Neither would have been caught by any test asserting cell ranges, because
the cell ranges were right the whole time. **Residual risk for chunking:** a
one-cell data row inside a table will still split it, and the next block's first
full-width row becomes its header. Not seen in the real files; worth a look if
label quality degrades.

**2026-08-23 — The overlong-token check rejected a URL, failing a whole
document.** `ASIL_Enterprise_Skills_Report_2026.docx` paragraph 194 carries a
176-character link to a Senate committee report in its sources line, and the
50-character limit failed all 161 passages. The check exists to catch ligature
and encoding failures — text that has lost its spaces — and a URL is long but
structured. `quality.is_addressable` now exempts URLs and email addresses. The
fail-the-whole-document policy from 2026-08-21 is unchanged and still right;
what was wrong was the check, not the consequence. Rejecting a research report
for citing its sources properly is the gate working against what it protects.

**2026-08-23 — Alphanumeric ratio on real files has comfortable margin.**
Measured across all six DOCX and the workbook: minimum 0.697 (DOCX), 0.722
(XLSX), against a 0.6 floor. Medians 0.77-0.83. Not one passage came within 0.05
of failing. The 0.69 figure estimated from synthetic rows was pessimistic; the
floor is not close to binding on this client's material.

**2026-08-23 — GAP NOW CLOSED (see the CellLocator entry below): the DOCX
adapter dropped every table.**
`adapters/docx.py` iterates `document.paragraphs`, which python-docx defines as
body-level paragraphs only — text inside a table is not in it. Across the six
real ASIL documents that is **41 tables, 990 non-empty cells, 35,838 characters,
30% of all DOCX text**, and it is the 30% holding the figures: a research report
keeps its numbers in tables. Nothing raises; the document simply yields fewer
passages, which is the silent loss BUILD_SPEC.md section 3 forbids and which the
compare stage would later read as an absence of evidence.

The fix needs a locator decision that has not been made: a DOCX table cell is
addressable as a `BlockLocator` (body position) or a `CellLocator` (row and
column within the table), and the two give a reviewer different things.
**Decide this before chunking**, because a passage's locator is what chunking is
forbidden to cross.

**2026-08-23 — `BlockLocator.style` is empty on most real ASIL paragraphs.**
Four of the six documents use direct formatting throughout, so `paragraph.style`
is `None` and the locator's `style` is `""`. Headings are still identifiable in
two of them ("Heading 1"/"Heading 2"/"Heading 3"). No action: `paragraph_index`
is the load-bearing half of the locator and it is unaffected. Worth knowing
before anything downstream tries to use `style` to find section boundaries.

**2026-08-23 — DOCX tables are `CellLocator`-anchored, and `CellLocator.sheet`
is now a looser concept.** The gap logged above is closed: `adapters/docx.py`
reads tables as well as paragraphs.

*Why `CellLocator` and not `BlockLocator`.* A DOCX table cell is tabular data.
The header-carrying row serialisation already solves the bare-number embedding
problem for exactly this shape, and `BlockLocator` would discard the column
meaning that makes a figure interpretable — leaving `~55%` with a paragraph
number attached and nothing to say what it measures.

*The tradeoff, stated plainly.* `CellLocator.sheet` was "the name of a
worksheet". It now also carries a table's identity within a document —
`Table 3 (para 194)`. The field is doing a looser job than its name suggests,
and a future reader will notice. The alternatives were worse: a seventh locator
variant duplicating `cell_range` for no gain, or a `BlockLocator` that throws
away the column. What keeps this honest is that both uses answer the same
question — *which grid, and where in it* — and both render into a citation a
reviewer can follow without opening anything else. **If a third kind of grid
turns up and `sheet` starts needing to be parsed rather than read, that is the
signal to split the variant.** Do not let it become a general-purpose string.

The bracketed number is the index the *next* body paragraph will have, so the
table sits immediately above the paragraph named, and a table before any
paragraph is "(para 0)" rather than a special case. Row numbers are 1-based
*within the table*, so `B4` means four rows down that table, which is what a
reviewer counts.

*Order.* Paragraphs and tables interleave in document order, by walking the body
element rather than `document.paragraphs` then `document.tables`. Where a table
sits is part of what it means: the paragraph above it is what says what it
shows. Appending tables would put every one of them next to the wrong text, and
chunking would then build neighbourhoods that do not exist. `paragraph_index`
still counts only paragraphs, so it addresses `document.paragraphs` exactly as
before and existing anchors are unchanged.

*Merges.* python-docx repeats one `<w:tc>` across every grid column a horizontal
merge covers, so a banner row in a four-column table arrives as four identical
strings; keeping only the first occurrence per `<w:tc>` restores the single cell
it is. Vertical merges need nothing — python-docx already reports the merged
text on each row, which is the propagate-down behaviour the XLSX adapter builds
by hand. Same asymmetry as the XLSX adapter, reached from the other direction.

**2026-08-23 — Third header bug, same class as the first two: a banner inside a
table steals the header.** `ASIL_Jobs_of_the_Future.docx` table 1 has its header
on row 1 and banner rows subdividing the data below it ("Higher automation risk
industries", "Lower automation risk industries"). `split_tables` correctly cut
at the banner — and thereby cut the data away from its header, so the first data
row was crowned and eight rows read `Industry: Agriculture, Forestry & Fishing`.

Two changes, and both are needed because one real document demands each:

- **A header does not repeat itself down its own column.** "Risk Group" is named
  once; a data row's "Higher risk" appears again in every row beneath it. This
  is what tells a genuine header from the first data row of an orphaned block.
- **A block with no header of its own inherits the last one seen**, provided the
  two share a right edge. Without inheritance the orphaned block has no labels
  at all; with unconditional inheritance, `Salary Premiums` — two stacked tables
  each with its own header — captions an average salary as a pay premium.

**2026-08-23 — Fourth header bug, found the same hour the third was fixed.**
The prediction in the previous entry — assume there is a fourth — was right, and
it was found by the alphanumeric ratio rather than by any test: table passages
dropped the worst-case ratio from 0.697 to 0.607, and the tightest passage read
`Leadership: Presentation; 37.1%: 2.0%`. Wrong labels are less prose-like than
right ones, so the ratio is a usable smoke alarm for this failure class. Keep an
eye on it.

Table 2 of `ASIL_Jobs_of_the_Future.docx` is banner-split like table 1, but
every data column is a measurement, so nothing repeats down a column and the
first row of an orphaned block passes **every** structural test for a header.
Table 1 had only been saved by luck: its "Risk Group" column repeats "Higher
risk", which tripped the repeat test. Structure alone cannot separate these.

The discriminator that does work is **difference in kind**: a header is a label
sitting over a column of measurements. `Avg Salary Low (AUD)` over `$109,860`
is a header; `Leadership | 37.1% | 40.4%` over more percentages is the first row
of a table whose header is further up. `introduces_new_header` applies that test,
and only where it is needed — when a block *could* inherit a header, deciding
whether it should instead declare its own. A block with nothing to inherit still
takes whatever `find_header_row` offers, because there is nothing to compare
against and column letters are worse.

This also meant widening what counts as a number: `54.4%`, `$109,860`, `~74%`
and `+31.0pp` are all one measurement wearing decoration, while
`Higher Risk Industries (avg %)` contains a per-cent sign and is prose.

That is four header bugs from real files against zero from synthetic fixtures,
all four of the same class: **correct anchors, wrong labels**. No cell-range
assertion can see any of them, which is why the mutation test moves the labels
rather than the ranges. Each fix was a condition the previous fixtures never
exercised, so **assume there is a fifth**. Before chunking, the cheapest
insurance on a newly supplied workbook is to sort its passages by alphanumeric
ratio and read the worst ten — that is how this one surfaced.

**2026-08-23 — Cell text has internal whitespace collapsed.** A DOCX cell
containing two paragraphs arrives with an embedded newline, and XLSX headers in
the real workbook contain them too (`Enterprise Skill\\n(ASIL framework)`).
Runs of whitespace become one space, so a citation quoting a cell reads as one
line. This is self-consistent for the verbatim-span check in the extract stage,
because the passage text is what that check runs against. Measured cost across
the six ASIL documents: 4 characters, all of them a double space after a bullet
glyph.

**2026-08-23 — Test fixtures reproduce real layouts; real files are asserted
only when present.** `BANNERED_TABLE` is modelled on the automation-risk table
in `ASIL_Jobs_of_the_Future.docx`, and `test_a_known_cell_of_a_real_asil_table`
asserts the same contract against the actual document, skipping when
`data/sources` is empty. That keeps the suite runnable in a checkout with no
client material while still checking the fixture has not drifted from the thing
it stands for. Client documents stay out of the repo.


**2026-09-07 — The registry exists, and rule 3 is now a function rather than a
convention.** `registry.yaml` at the repository root (or `$UPDATER_REGISTRY_PATH`),
loaded by `updater.registry.load_registry`. The boundary is
`Registry.source_for_path` and `Registry.get`: both refuse, and refusing means
there is no `Source`, and without a `Source` nothing can be ingested, chunked or
cited. `updater ingest <unregistered file>` exits 2 before reading a byte.

`scripts/dump_passages.py` remains **the one sanctioned exception**, and its
reason has changed shape: no longer "the registry does not exist yet" but "this
script exists to look at a document *before* anyone has approved it". Nothing it
prints becomes evidence. Do not add a second exception.

Validation is deliberately unforgiving, and one choice in it is not obvious:
missing fields and unknown fields are reported **together**. A typo produces
both at once — `topics:` leaves `topic` missing — and being told only that
`topic` is missing, while `topics:` sits there in the file, makes the reader
hunt for a difference the loader has already found.

`registry.yaml` is committed with the eight real ASIL documents in it. The
filenames are already all over this log, so it discloses nothing new, and an
approved-source list that is not in version control cannot be audited — which is
the point of rule 3. **`owner`, `topic` and `year` were filled in from the
filenames and are marked in the file as needing ASIL's review.**

**2026-09-07 — The spec's chunk size does not fit the spec's embedding model,
and the failure is silent.** BUILD_SPEC.md section 4 asks for 400-600 token
chunks and names `all-MiniLM-L6-v2`. Measured: that model's `max_seq_length` is
**256 word pieces**, and sentence-transformers truncates beyond it with no
warning. A 600-token chunk would be retrievable by its first 40% only, with the
rest present in the database and absent from the index — an absence of evidence,
which the compare stage reads as a finding about the world.

Resolution, with the reasoning, because this is the largest design decision of
step 2:

- The **chunk stays the size the spec asks for**. It is the unit of provenance
  and the unit the extract stage will read; shrinking it to 254 tokens to suit
  one model's input buffer would let an implementation detail redefine what a
  citation points at.
- A chunk is **embedded as overlapping windows of at most 254 word pieces**, one
  vector per window, all of them pointing back at the chunk. Retrieval scores a
  chunk by its **best** window.
- Max over windows, not mean. Averaging is the obvious alternative and it is
  wrong for this corpus: a row stating one apprenticeship figure is exactly what
  a query is looking for, and averaging it with 500 tokens of surrounding prose
  is how it stops being findable.
- **Windows are not anchors.** A window is an artefact of this model's input
  size, not a place in a document. Minting anchors for one would put a citation
  in the draft that points at an arbitrary 254-token fragment.

`SentenceTransformerEmbedder` re-reads `max_seq_length` at load time and refuses
to run if a future release ships a smaller window than the constant assumes.

**2026-09-07 — A token is a word piece of the embedding model's tokenizer.**
The only budget that can actually be violated is that model's window, so the
budget is counted in the units the window is expressed in. Counting in any other
tokenizer's units produces a number that is nobody's constraint.

Measured on the real corpus: prose runs about 4.3 characters per word piece —
close to the ingest estimate — but a serialised table row runs about 3.5,
because digits and punctuation fragment. Tabular sources are most of ASIL's
material, so the 4-characters-per-token estimate is optimistic by roughly 20%
exactly where the corpus is densest. That is the argument for real counting, and
it is why `Passage.token_count` on a chunk is now a real count.

**Trap, invisible if you do not know it:** the `tokenizer.json` published with
all-MiniLM-L6-v2 carries **truncation and padding at 128 tokens**.
sentence-transformers overrides both at load; a bare `tokenizers.Tokenizer` does
not, so counting with it untouched silently reports 128 for everything longer,
and every chunk comes out the wrong size with nothing to show for it.
`load_tokenizer` calls `no_truncation()` and `no_padding()`. Do not remove them.

**2026-09-07 — Atoms must be no coarser than the overlap, or the overlap
silently becomes zero.** Found by a test, not by reading the code. Chunking
breaks text into atoms at paragraph, sentence and clause boundaries, then packs
atoms into chunks; the overlap is built by carrying trailing atoms into the next
chunk. If an atom is larger than the overlap budget, **no atom can ever be
carried** — and the chunks come out correctly sized, correctly tiling the text,
and with no overlap at all, so a sentence on a boundary appears whole in neither
chunk. `_atom_budget` therefore cuts no coarser than `overlap_tokens`.

The test that caught it measures the *gap* between consecutive chunks and found
-2 characters where it wanted +90. Keep it: nothing else in the suite would have
noticed, because every other property still held.

**2026-09-07 — `min_tokens` rebalances the tail; it is not a floor.** Greedy
packing leaves the remainder in the last chunk, which can be twenty tokens. A
stub is not *wrong* — its anchor is exact — but it embeds badly, so the tail of
every long page would become quietly unfindable. `_rebalance_tail` grows it
backwards until it reaches `min_tokens`, which only ever adds more overlap with
the previous chunk, from the same locator unit. It may not swallow the previous
chunk's start: two chunks covering one span are one chunk and a duplicate.

A locator unit smaller than `min_tokens` is left alone. The only text that could
pad it belongs to another paragraph or another row, and borrowing that is the
one thing this module may not do.

**2026-09-07 — Chunking is a no-op on ASIL's current corpus, and that is worth
knowing before tuning anything.** Measured over all seven readable documents:
740 passages produce 740 chunks. **Not one locator unit exceeds 600 tokens** —
the maximum is 335, the median is 48, and 29% are under 30 tokens. Every unit is
a DOCX paragraph or a table row, and all of them already fit.

Two consequences:

1. The chunker is insurance, not a workhorse. It earns its place the moment a
   government PDF or an HTML page enters the registry, where the locator unit is
   a whole page or a whole article. Today it is exercised only by fixtures — so
   the fixtures are what keep it honest, and the mutation tests matter more here
   than anywhere else in the codebase.
2. **The retrieval unit on this corpus is one paragraph or one table row.** That
   is excellent for citation precision and thin for evidence extraction: a
   40-token row rarely carries the context needed to judge a claim. Assembling a
   neighbourhood of adjacent passages — each keeping its own anchor, never
   concatenated into one — is step 3's problem, and it should be solved there
   rather than by making chunks bigger here.

**2026-09-07 — Dense retrieval cannot find a number, measured on this corpus.**
BUILD_SPEC.md says hybrid is "worth it". On this material it is not optional,
and here is the evidence rather than the assertion. Querying `109,860` returns,
at rank 1, an unrelated table header scoring 0.213 — while two chunks containing
`$109,860` sit in the index, one of them reading
`Avg Salary Low (AUD): $109,860`. The number is three word pieces with no
semantics, and the nearest neighbours of its vector are other numbers.

Prose queries work well: "which industries have the highest automation risk"
returns the right rows at 0.77. It is precisely the statistics — the thing this
project exists to re-check year on year — that dense retrieval misses. BM25
plugs in at `updater.retrieve.search.Retriever`; the fusion function is
deliberately not written yet, because weighing two scores when only one of them
exists is an untested guess about the other. **Done 2026-10-02, BACKEND_PLAN.md
phase 2: see the hybrid retrieval entries at the end of this log.**

**2026-09-07 — Norton intercepts TLS, and it broke the model download.** The
same cause as `system-certs = true` under `[tool.uv]`, reaching a third client.
`huggingface_hub` downloads over `httpx`, which verifies against `certifi`, and
the certificate chain for huggingface.co is reissued by
`Norton Web/Mail Shield Root` — present in the Windows store, absent from
certifi. The symptom is **misleading**: *"We couldn't connect to
https://huggingface.co"*, which reads as an unreachable host when the connection
is fine and only the verification failed. `urllib` works throughout, because it
reads the OS store.

`updater.certs.ensure_system_certs` exports the OS trust store to
`~/.cache/asil-updater/system-ca-bundle.pem` and points `SSL_CERT_FILE` at it.
It never overrides a bundle the operator set, and it does nothing off Windows.
**Export every certificate, not only those flagged for server authentication:**
the first attempt filtered on that purpose and dropped the Norton root, which is
the one certificate the export exists to capture. If a fourth client starts
failing on certificates, this is the cause and this is the knob. Never
`verify=False`.

**2026-09-07 — The Python floor is 3.12, and `Locator` uses the `type`
statement.** **SUPERSEDED the next day — see the 2026-09-08 entry at the end of
this log. The second half of this entry was wrong, and wrong in a way worth
leaving visible.** `requires-python` said `>=3.11` while `.python-version` pinned
3.12. The retrieve extra settled it: numpy 2.5's stubs use `type` statements and
do not type-check under `python_version = "3.11"`. All three now say 3.12, which
is what this machine has run all along. Ruff's `target-version` moved with them,
which is what turned `Locator: TypeAlias = ...` into `type Locator = ...` — a
cosmetic change forced by the bump, not a redesign of the union.

**2026-09-07 — The index records the settings it was built with and refuses to
mix them.** Model id, dimensions, schema version and chunk budget live in a
`meta` table, checked on open. Vectors from two models in one table are not
comparable, and the symptom is retrieval that is quietly *worse* rather than
obviously broken — nobody would trace that back to a change of model three weeks
earlier. `updater ingest --rebuild` is the way out. **It was not, until
2026-10-02: it opened the index before clearing it and hit the same refusal.
See the rebuild entry at the end of this log.**

Re-indexing a source **replaces** its chunks, in one transaction. Two copies of
a source, one of them stale, would both be retrievable and only one would be
true.

**`vec0` is a virtual table, so `ON DELETE CASCADE` does not reach it.** Its rows
are deleted by hand in `_delete_source_rows`, before the chunks they belong to.
Forget that and re-indexing leaves orphaned vectors whose join to `windows`
drops them silently — a re-indexed source that retrieves nothing. There is a
test, and it asserts the count rather than the absence of an exception.

**2026-09-07 — `updater.citations.render_locator` supersedes the throwaway
renderer in `dump_passages.py`.** The 2026-08-23 entry said step 5 would own the
real renderer and this one should be deleted rather than imported. The CLI made
a third copy imminent, so the shared half — locator to human-readable string —
moved into `updater/citations.py` now, and the script imports it. **Step 5 still
owns the other half**: assembling "(Source title, p. 14)" into draft sentences
and linting for material sentences that carry no citation. It builds on
`render_anchor` rather than replacing it.

**2026-09-07 — The whitespace-collapse decision was recorded for cell text but
implemented for DOCX only.** The 2026-08-23 entry says internal whitespace is
collapsed, and names the XLSX case in evidence
(`Enterprise Skill\n(ASIL framework)`). `adapters/docx.py` did it at parse time;
the XLSX and CSV path never did, so 16 of 29 rows on one real sheet carried raw
newlines into the index and their labels read as two lines.

Fixed in `tabular.serialise_row`, the one place both adapters go through — the
same argument as the 2026-08-23 entry on serialisation ownership: XLSX and CSV
must produce identical text for the same table, and one shared place is what
guarantees it. Found by the `--worst` smoke alarm, which is the third time that
ratio has surfaced something it was not written for.

**2026-09-07 — No fifth header bug, checked.** The 2026-08-23 entry predicted
one and said the cheapest insurance is to sort by alphanumeric ratio and read
the worst ten. Done, across all seven readable documents, at chunk level. Every
label in the worst-rated passages is correct — including in the two tables that
produced the third and fourth bugs. The floor is now **0.692** against a 0.6
gate (it was 0.607 while the fourth bug was live), and the whitespace fix above
was the only thing the sweep turned up. The prediction still stands for the next
new workbook, and the sweep is one command:

```sh
uv run python scripts/dump_passages.py <path> --worst 10
```

**2026-09-08 — The floor is 3.12, the source stays 3.11-parsable, and the
difference between those two is the point.** This supersedes the second half of
the 2026-09-07 entry above, which called the `type Locator` rewrite cosmetic. It
was not, and the mistake is instructive: a lint target bump changed what
interpreters can *parse* the source at all.

The chain, in the order it actually happened: numpy 2.5's stubs use PEP 695
`type` statements → mypy at `python_version = "3.11"` refuses to check → the
floor was raised to 3.12 → ruff's `target-version` was raised with it → UP040
fired → `Locator: TypeAlias = ...` became `type Locator = ...`. Only the first
link was a considered decision. The last one made the package a **SyntaxError on
3.11 at runtime**, which is a deployment constraint, arrived at as a side effect
of a linter's autofix.

What was measured, so nobody re-derives it:

- **The failure is numpy's stubs, reached through sentence-transformers.** No
  code here imports numpy; `sentence_transformers` ships `py.typed`, so mypy
  follows it into `numpy/__init__.pyi:737`.
- **No mypy ignore can suppress it.** `follow_imports = "skip"`,
  `follow_imports = "silent"` and `ignore_errors = true` on `numpy.*` were all
  tried and all leave the error. It is a *syntax* error raised while parsing,
  before per-module options apply, and it aborts the run entirely — "errors
  prevented further checking".
- **Pinning numpy does work.** numpy 2.4.6's stubs contain no `type` statements
  and mypy at 3.11 passes cleanly. So `numpy<2.5` is the price of a 3.11 floor,
  should one ever be needed.
- **No dependency requires 3.12.** torch, sentence-transformers, transformers
  and tokenizers are `>=3.10`; numpy is `>=3.11`. The stack runs on 3.11.
- **Our own source parses under 3.11** with `TypeAlias` restored, verified by
  running `compileall` over `src`, `tests` and `scripts` with a real 3.11
  interpreter. With `type Locator` it raises `SyntaxError` at line 186.

The decision: `requires-python`, ruff's `target-version` and mypy's
`python_version` all say **3.12**, numpy stays **unpinned**, and the source is
held **3.11-parsable** — `ignore = ["UP040", "UP046", "UP047"]` refuses the three
lint rules that would introduce PEP 695 syntax. Declaring 3.12 keeps the type
checker honest against the interpreter this actually runs on and keeps a
transitive dependency unpinned for a reason that was never about the runtime;
holding the source parsable keeps a 3.11 deployment target a one-line change
rather than a port.

**Revisit if a deployment target requires 3.11.** That change is: lower
`requires-python`, lower `python_version`, and pin `numpy<2.5` in the retrieve
extra. Nothing in the source has to move.

**The check that keeps this true is not automated yet.** The guard is
`compileall` under a 3.11 interpreter, and there is no CI in this repository to
run it. Until there is, the claim "the source is 3.11-parsable" is verified only
when somebody runs:

```sh
uv python install 3.11
"$(uv python find 3.11)" -m compileall -q src tests scripts
```

An unverified compatibility claim decays quietly, which is the same failure mode
as everything else in this log. Wiring it into CI, or into the test suite, is
outstanding.

**2026-10-01 — The mock API's fixtures are extracted from the prototype, never
typed.** BACKEND_PLAN.md phase 0. `scripts/extract_prototype_fixtures.py` reads
the `claims`, `draftText`, `outcomes` and `notifs` arrays out of the prototype's
`<script>`, and the registry rows, dashboard cards, activity feed and settings
out of its markup, into `src/updater/api/fixtures/*.json`. The suite runs its
`--check` mode, so a revised prototype fails the build until the fixtures are
regenerated and somebody has read the diff.

The JS parser is hand-written and deliberately narrow. JSON5 cannot read
`draftText`, whose keys are numbers, and a lenient parser would let a revised
prototype be half-extracted without anyone noticing, so it refuses anything it
does not recognise. Field sets are checked exactly, because the null transform
below is keyed by field name and a renamed field would slip past it.

**Placeholder strings become null. This is the one place the fixtures
deliberately disagree with the prototype.** The plan says the prototype wins on
shapes; here it loses to rule 2. "Not retrieved", "unknown", "Not in last
year's report" and "Not in the 2025 report." are null in the API, and the
frontend renders the label. A contract carrying those strings would oblige
phase 6 to emit them, and a missing value would then read exactly like one a
source stated. Only the listed strings, in the listed fields, are mapped.

The other transforms: display labels become the contract's enum values (an
unknown label raises, and every `<select>` option is mapped rather than only the
selected one); relative dates become timestamps counted back from the
prototype's own date, 16 September 2026, at its 9:00 am scheduled scan time —
**the time of day is the fixture's assumption, not something the prototype
states**; "Not yet" becomes null; registry hosts gain `https://`.

The prototype is committed under `docs/frontend/`. It names ASIL staff and
carries their email addresses (team settings, profile, one notification), and
the fixtures carry the owner names and that notification.

**Trap, found while writing the tests:** a backslash-u escape for the curly
apostrophe, typed into a test string, can land in the file as the literal
character, and the parser test then passes without exercising the `\u` path at
all. (It happened to this very entry on the first attempt.) The test assembles
the escape at runtime, and a mutant that breaks `\u` decoding fails it. Keep it
that way.

**2026-10-01 — Pydantic is confined to `updater.api`, and the API splits policy
from state.** The plan's rule is dataclasses inside the package, Pydantic at
the boundary; the API package *is* the boundary, so its schemas are Pydantic and
nothing outside `updater.api` imports from it. Inside it, `policy.py` holds the
rules as pure functions and `mock.py` holds the phase 0 data. Phase 1 replaces
`mock.py` with SQLite; the rules and `tests/test_api.py` carry over unchanged,
which is the phase 1 acceptance test.

The mock holds **one research update**: one claim set, one set of decisions and
reviews, one sign-off. A run id selects scan progress and nothing else, which is
also how the prototype behaves. `GET /api/runs/{id}` is deliberately not
idempotent in the mock: each poll finishes one source, so the frontend can build
its progress view against something that moves. Phase 3 replaces this with real
progress.

Every schema forbids unknown fields. On a request body that is a policy, not a
style choice: `POST /api/sources` cannot be sent `"status": "approved"`.

A refused scan is refused whole, naming every unapproved source at once. Quietly
filtering out the unapproved one and scanning the rest is how unapproved
material comes to be treated as checked.

CORS admits localhost and 127.0.0.1 on any port, and **not** `Origin: null`,
which is what a page opened from disk sends. Admitting `null` would admit every
sandboxed page on the machine. The frontend has to be served over http.

**`update_frequency` in the API is the prototype's labels** (Annual, Biennial,
Half-yearly, Quarterly), not `models.UpdateFrequency`. Phase 1 has to reconcile
them, and one is a trap: the enum's `BIANNUAL` reads as "twice a year" to some
people and "every two years" to others, while the prototype says both
"Half-yearly" and "Biennial". Map by meaning, not by spelling. **Settled
2026-10-02: `BIANNUAL` is gone; see the entry at the end of this log.**

**Tooling found on the way.** Starlette 1.7's test client wants `httpx2` and
warns on `httpx`; its type hints import `httpx2` too, so that is the dev
dependency. FastAPI 0.142 nests included routers, so `app.routes` no longer
lists the API's paths; assert against `app.openapi()["paths"]`. **`uv add` and
`uv remove` re-sync the environment without extras:** removing httpx uninstalled
uvicorn. After either, run `uv sync --extra retrieve --extra api`.

**2026-10-01 — Sign-off follows the prototype's rule, not the stricter reading
of the plan.** Phase 0 says to refuse sign-off
"while any claim is flagged" and, in the same breath, to match `buildPublish`,
which does not. `buildPublish` refuses only while a draft claim has no verdict,
or when nothing is approved or edited. A flagged claim is left out of this
edition and the checklist says so. `policy.sign_off_blockers` implements exactly
that, and `test_a_flagged_claim_does_not_block_sign_off_and_is_left_out` pins
it. Phase 7 inherits this rule. A note alone does not resolve a claim.

**Audit entries follow the plan's shape, not the prototype's values.** Field
names are the prototype's (`t`, `who`, `action`, `claim`); the values are the
plan's: `t` is an ISO timestamp to the second, `action` a lowercase verb, and
`claim` the claim's id (null for the sign-off, which covers the whole report).
The prototype stores a display time ("10:24am") and the claim's title, and an
audit CSV with neither dates nor stable ids would be no use to anyone. The
frontend formats them. Decisions on the extraction screen are audited too, as
`included`, `skipped` and `marked_for_check`, because the prototype's own copy
there says "your decision is recorded in the audit trail".

The log is append-only by construction: `MockState._log` is its only writer,
nothing removes from it, and no route can write it (a test asserts the audit
path has only GET). **Who acted is the prototype's signed-in user, "Kathleen
D.", from the fixtures.** There is no auth (BUILD_SPEC.md section 7), so in
any version identity is self-declared at best. Phase 7 has to decide what "who"
means. **Approving a source is not audited yet.** Who approved what is a rule 3
accountability question, and phase 1's registry lifecycle is the place to
settle it. **Settled 2026-10-02: every source change is audited; see the
registry lifecycle entry below.** `MockState._log` is still the only writer,
and the log is now append-only in SQLite too.

**The draft is the prototype's `buildDraft`, including its fallback:** the
included claims, or, until anything is included, every claim that can be
verified. It is safe only because sign-off then requires a verdict on every one
of them. A claim with no verified passage cannot enter the draft at all:
`MockState` refuses to start if a draftable claim lacks one, rather than falling
back as the prototype does to showing the claim's own text *as* the passage.

**A verdict leaves an earlier edit's text in place**, as the prototype's
`Object.assign` does: edit, then approve, and the report carries the edited
sentence. `text` is accepted only with `edited`, so a rewrite cannot slip in
under another verdict.

**Exports never produce a placeholder file.** `audit_csv` works at any time,
oldest first. `pdf`, `docx` and `newsletter` return 409 before sign-off and 501
after it, until phase 8. **The prior-report upload is sniffed, not trusted:** a
PDF named `.txt` is accepted and a text file named `.pdf` gets a 415. Nothing is
parsed or kept yet; that is phase 4. A settings change that touches either
policy is refused whole, so a permitted change in the same request does not
land either.

**The policies are mutation-tested.** Seven mutants, each caught by the API
tests: flagged counted as in-report, an unresolved claim not blocking, a cannot
claim includable, a policy change allowed, paused and url_changed sources
scannable, an extra status transition, a cannot claim in the fallback draft.
Re-run them after touching `policy.py`. The suite passed first time against the
real rules, which on its own shows nothing.

**For the frontend team.** Claim `note`s and notification `body`s contain HTML
(`<b>`, `<q>`), passed through verbatim because the prototype renders them with
`innerHTML`. That is fine for fixtures. From phase 6 a note is derived from
model output, and model text inserted with `innerHTML` is an injection risk, so
the frontend should insert those fields as plain text before then.

**2026-10-02 — The API's state lives in `updater.state`, in the index's file,
in tables of its own.** BACKEND_PLAN.md phase 1. `StateStore` is plain
`sqlite3` with no Pydantic and no `sqlite-vec`, at package level rather than
inside `updater.api`, because phases 3 to 9 write runs and claims from pipeline
code that must not import the API. Its schema version sits in `state_meta`.

**Trap: never write to the index's `meta` table.** `PassageStore` refuses a
`meta` that holds anything but its own keys, so a state version written there
makes the next `updater ingest` fail with `IncompatibleIndex`. A test opens the
index in a file that already holds state, and a mutant writing to `meta` fails
it. Plain `sqlite3` can share the file without `sqlite-vec` loaded, as long as
nothing touches `window_vectors` (measured on a copy of the real file).
`updater ingest --rebuild` empties only the index's tables; a test pins that it
leaves the state alone.

**The state is refused, never rebuilt, when it does not match.** The index can
be rebuilt from its sources in thirty seconds, but the audit log cannot be
rebuilt from anything. So a different schema version raises `IncompatibleState`
with the tables left as they are, and **the first schema change owes a
migration, not a reset.** Phases 3 to 6 will add tables and should be the first
to write one.

**`claims` and `prior_claims` were deferred to phases 6 and 4.**
The plan lists both for phase 1. Seeding the fixture claims would write
the prototype's invented `passage` text, with no anchor, into the database that
holds the real anchored chunks (rule 1), and a claims schema with no anchor
columns would be redesigned once claims carry verified anchors. The fixtures'
claims, draft text, outcomes and cards are read from the fixtures on every
start and never written anywhere.

**The audit log is append-only twice over.** `append_audit` is the store's only
writer, and triggers abort any `UPDATE` or `DELETE` on `audit`, so a future
method that tried would fail rather than rewrite history. A decision, review or
source change and its audit entry are one `transaction()`, and nested use joins
the outer one. **Sign-off is read from the audit log's `signed_off` entry**,
not a flag that could be edited. **The two policies are never stored**: they
come from the fixture every time, so nothing stored can switch them off.

**The state records which claims it is about.** `claim_set` is a digest of
`claims.json` and `draft.json`, since a review approves a sentence. A database
written against different fixtures is refused, because decisions and reviews
name claims by id and would attach to the wrong ones. After regenerating the
fixtures, move the database aside; re-ingesting takes thirty seconds.
**Consequence of the plan's "same file":** demo actions taken through
`updater serve` land in the real file's append-only audit log and can only be
removed by dropping the table or deleting the file. Phase 9's real claims need a
`claim_set` of their own, which the check then keeps from mixing with the demo
state. Phase 9 also has to decide whether `--demo` gets its own file.

**`MockState` with no `path` is in memory.** The acceptance tests build a fresh
`MockState(clock=...)` per test and expect a fresh state (every one expects run
`r_2026_09_16_01`), so in-memory is the default and the tests run the same SQL
the server does. Only `updater serve` opens the file. An autouse fixture in
`tests/conftest.py` points `UPDATER_DB_PATH` at a temporary file for every
test, because `test_serve_runs_the_app` would otherwise write to the developer's
real audit log, which cannot be tidied up afterwards.

**"Byte-identical to phase 0" was checked by replay, once.** The acceptance
tests compare parsed JSON, which would not notice reordered keys or changed
whitespace. One scripted session of 59 requests, covering successes, refusals,
sign-off and exports, was replayed against commit `c6514c2` and against phase 1,
and the raw response bytes matched. The replay is not in the suite.

**2026-10-02 — The registry's lifecycle is an overlay keyed by source id, over
a base list that nothing writes.** `source_status` holds status, a confirmed
URL and `last_checked`, with NULL meaning the base value stands, and
`proposed_sources` holds sources added through the API. Until phase 9 the base
list is the prototype's twelve sources from the fixtures; from phase 3,
`registry.yaml`. Nothing writes the YAML.

**The API cannot serve `registry.yaml` yet, and phase 9 inherits three
contract questions.** Byte-identical responses rule it out in phase 1: the
fixture sources and the eight real ASIL documents share no ids. The real ones
also do not fit the prototype's vocabulary:
- topics are free text, not one of the six fixed topics;
- DOCX and PPTX are neither PDF, Web nor Dataset;
- `unknown` (five of the eight) has no label among the four frequencies.

The last should become null in the API (rule 2), not a placeholder label. The
first two are contract changes for whoever wires the real registry in.

**Every source change is audited**, as `source_proposed`, `source_approved`,
`source_resumed`, `source_url_confirmed` (with the confirmed address in
`detail`) and `source_paused`, each naming the source and no claim. They share
the `audit` table with claim actions, since phase 10 derives the activity feed
from it, and are kept out of `/runs/{id}/audit` and the audit CSV, which are a
report's review trail. A test asserts `SOURCE_ACTION` has one action for every
transition `policy.py` permits.

**Mutation-tested.** Ten mutants, each caught. Four against the store: no
triggers, a transaction that never begins, state written to the index's `meta`,
and a status change that drops a confirmed URL. Six against the API: a decision
outside its transaction, a source change outside its transaction, the state
ignoring its path, the overlay dropping a confirmed URL, an unrecorded URL
confirmation, and the review trail leaking registry actions. **Trap when
writing mutation scripts on Windows:** `Path.write_text` turns LF into CRLF,
so restoring a file that way changes every line. Read and write bytes.

**Known limit.** The server and `updater ingest` share one file, and SQLite
serialises writers. A write blocked longer than the 5-second busy timeout fails
loudly rather than being lost. `BEGIN IMMEDIATE` takes the lock up front rather
than failing halfway through a unit of work.

**2026-10-02 — `UpdateFrequency.BIANNUAL` is gone, split into `BIENNIAL` and
`HALF_YEARLY`.** This settles the trap logged on 2026-10-01: "biannual" means
twice a year to some readers and every two years to others, and the prototype
uses both cadences. Each of its four labels (Annual, Biennial, Half-yearly,
Quarterly) now names exactly one member by meaning. The loader refuses
`biannual` with a message naming both replacements rather than picking one,
because a wrong guess changes when a source is re-checked. Nothing in
`registry.yaml` used it.

**2026-10-02 — Hybrid retrieval: FTS5 beside the vectors, fused by reciprocal
rank.** BACKEND_PLAN.md phase 2. `chunks_fts` is an FTS5 external-content
table over `chunks.text` in the index's file, rather than BUILD_SPEC.md's
`rank_bm25`: same file, same transaction, and no corpus to rebuild in memory
in every process. `SparseRetriever` searches it; `HybridRetriever` fuses it with
`DenseRetriever` by RRF, `k = 60`, over each retriever's top 50 whatever `top_k`
is. **Ranks, never scores:** cosine and BM25 share no scale, and normalising one
to look like the other is a guess. `RetrievalResult` keeps its shape and gains
`dense_rank` and `sparse_rank`, None when that retriever did not return the
chunk. `updater search` is hybrid; `--retriever dense|sparse` shows one input.

*The query is user text, never FTS5 syntax.* Each whitespace-separated word is
quoted and the words are OR-ed. Quoting makes AND, OR, NOT, NEAR, `*`, `^` and
`:` plain words, and because unicode61 splits `109,860` at the comma, the quoted
word is the *phrase* `109 860`: it matches `$109,860` and not a row with 860 in
one column and 109 in another. A word with no letter or digit is dropped (it
would quote to an empty phrase). **NUL ends an FTS5 string early and raised
`unterminated string`**, so it is a separator. 200,000 random hostile queries
raised nothing. **No stemming:** the default tokenizer does not match
"premium" to "Premiums"; dense covers morphology, and a test fixture tripped on
it, so do not assume otherwise.

**2026-10-02 — The rank-1 tie, and lexical first.**
Measured on the real index before writing any fusion: the row reading
`Avg Salary Low (AUD): $109,860` is **dense rank 52**, two places outside the
top 50. Dense's rank 1 is the same unrelated header as before (cosine 0.213).
So under the plan's exact RRF, the header (dense 1, not in full-text) and the
salary row (full-text 1, not in dense) both score **exactly 1/61**, and the
plan's acceptance check passes or fails on the tie-break alone. Insertion order
or chunk id would have put the header first.

The rule: equal fused scores go to the better full-text rank, then the better
dense rank, then chunk id. A lexical hit is checkable — the query's words are in
the text — and a dense rank for a number is noise. It only orders what RRF
scores equally; it never overrides a difference. **The tie is fragile in both
directions:** a corpus change that lifts the row into dense's top 50 makes it
win outright, and one that puts the header into full-text's list would break
the tie the other way. `test_a_figure_dense_ranks_outside_its_top_50_comes_first`
reproduces the shape (rank 56 of 56, exact 1/61) with a scripted embedder that
places each text at a chosen cosine, since the stub's hash geometry would make
the dense half an accident.

Prose, measured before and after: on the reference query "which industries
have the highest automation risk" all of the fused top 5 are automation-risk
rows or the methodology paragraph; dense's rank 1 moves to 2. For "employer
demand for digital literacy in job ads" the fused rank 1 is "27% of job ads
mention digital literacy", better than dense's. Fusion moves dense-only answers
down when other rows share the query's words; it cannot drop one from the
list, and a test pins that.

**2026-10-02 — The fused score is not a relevance measure. Phases 5 and 6
inherit this.** It is a function of two ranks, so whatever comes first scores
the same whether it is an exact match or the best of a bad lot: the junk header
above sits at 0.0164 beside the real figure. BUILD_SPEC.md's compare stage
defaults to `cannot-verify` when retrieval "came back thin", and **thinness
cannot be read from the fused score.** It has to come from the inputs — cosine
from `DenseRetriever`, which is comparable across queries, or the existence of a
full-text hit. Fused results carry the input ranks, not the input scores; if
the compare stage needs scores, add `dense_score` there rather than thresholding
RRF.

**2026-10-02 — The full-text index is kept in step by triggers, and `chunks`
declares its rowid.** Triggers on `chunks` write `chunks_fts` on insert, delete
and update, inside whatever transaction changed `chunks` — the transaction that
writes the vectors. The vec0 lesson (2026-09-07) was that a hand-maintained
mirror gets forgotten; a trigger cannot be. **The failure is worse than a stale
hit:** rowids are reused once the rows holding them are deleted, so a
full-text entry left behind for old text points at the *new* chunk, and a search
returns a passage that does not contain the word it was found by, with a sound
anchor. Two tests pin it, and FTS5's own `integrity-check` with `rank = 1`
compares the index against `chunks` in both.

`chunks` now has `rowid INTEGER PRIMARY KEY` and `id TEXT NOT NULL UNIQUE`, as
`windows` already did for vec0. SQLite documents that `VACUUM` may renumber the
rowids of a table without an INTEGER PRIMARY KEY, which would remap every
full-text hit to the wrong chunk. **Measured: SQLite 3.53.1 does not**, so no
behavioural test can show the failure on this machine; a schema test pins the
declaration instead of a behaviour this build happens to hide.

**2026-10-02 — `updater ingest --rebuild` never worked on the case it exists
for, and now does.** `IncompatibleIndex` says "Rebuild it: updater ingest
--rebuild", but the CLI opened the store — which raised that same error — before
clearing it. The only way out was deleting the file, and the file holds the
append-only audit log. Phase 2's schema bump (version 2) sent every existing
index down this path, so it had to be fixed here.
`PassageStore(rebuild=True)` drops the index's tables by name
(`_INDEX_TABLES`), in one transaction, and leaves every other table alone.
**Never add a state table to that tuple**: `DROP TABLE` does not fire the audit
log's triggers, so the append-only guarantee does not protect it there. The
state test catches it. `clear()` is gone; one way to empty the index. Settings
are now checked **before** any DDL runs, so a refused file is left exactly as it
was found rather than half-upgraded to the new schema. The real index was
rebuilt this way: 740 chunks, as before.

**2026-10-02 — A mutant only counts as caught by the test written for it.**
Nineteen mutants across the store and fusion, all caught. The method changed
on the way: the first run counted the missing-delete-trigger mutant as caught,
but the failure came from a *fixture* (the version-1 index builder drops that
trigger by name) and `-x` stopped there. The mutation script now names the
expected test per mutant and requires it among the failures. Keep doing it
that way; a fixture tripping over a mutation is not a test catching it.

**2026-10-08 — The backend moved into the group repo's copy, beside the group's
prototype.** `src/updater`, `tests`, `scripts`, the configs and these documents
came from `asil-updater` at `5aa024f`, unchanged except where this entry says.
The group's files stay as they were: `src/backend/` (the Gemini/LlamaIndex
prototype, whose retirement is a separate, agreed change), `tests/test_basic.py`,
`tests/test_repoagent.py` (empty, so it collects nothing), `data/readme.md`,
`src/__init__.py` and the README, which was merged by hand.

- **The prototype's dependencies had never been written down.** No
  requirements file existed, so `pydantic`, `llama-index-core` and
  `llama-index-llms-google-genai` are now the `prototype` extra. Adding it and
  `gemini` moved no version already in the lock; 41 packages were added.
- **The group's `src/__init__.py` makes `src` a package**, so mypy found every
  module twice, as `src.updater.x` and `updater.x`, and stopped. mypy now checks
  `src/updater` and `tests` with explicit package bases, and ruff excludes
  `src/backend`, so `ruff format .` cannot rewrite code that is not ours.
  The group's two test modules are exempt from the untyped-def check rather
  than annotated.
- **`!data/readme.md`** in `.gitignore`, so `data/*` does not hide the group's
  file.
- **No client email address is in this repository.** This repository is
  pushed to GitHub, so every `@astartinlife.org.au` address is
  `@example.org`, in the notification fixture and in the prototype alike
  (see the dashboard entry below). That is the one way the fixture differs
  from `asil-updater`. Staff names remain, because the API tests assert
  "Kathleen D.".
- **The baseline commit tracks three `__pycache__/*.pyc` files**, which the
  group repo does not. Running Python here rewrites them, so the checks for
  this change ran with `PYTHONDONTWRITEBYTECODE=1`. Untracking them is a
  one-line change for whoever owns the baseline.

**2026-10-08 — Extraction: one provider interface, a pure verifier, and Claude
and Gemini behind it.** BACKEND_PLAN.md phase 5, less its real-document check.

*The interface is one method.* `EvidenceModel.complete(system, prompt, schema)`
returns a `ModelReply`: the JSON text, the provider, and the model **the API
reports**, which differs from the configured id whenever that id is an alias.
Two errors, never an empty result: `ModelUnavailable` is configuration (no key,
no model id, no SDK, an unknown provider) and is raised before any request;
`ModelCallFailed` is a call that returned nothing usable, which includes a
refusal, a reply stopped at the token limit, and JSON that is not the
requested shape. An empty evidence list must only ever mean that the model read
the passages and found nothing.

*What the model authors is phase 5's list and no more.* An extra field — a
`page`, a `source` — makes the finding malformed, and `passage_id` is an enum of
the ids supplied. The anchor always comes from verification.

*Verification:* exact, then whitespace-flexible only, then discard. **Both
passes match whole tokens**: without that, `3.1%` verifies against `13.1%`
because it is literally a substring of it. The text shown is the document's
characters, never the model's quote; offsets are locator-relative, which the
non-zero `char_start` test pins; a repeated quote anchors at its first
occurrence, where the characters are identical.

*One guard beyond the plan:* a `statistic` whose numbers are not all in its own
verified quote is discarded as `statistic-not-in-quote`. That catches a figure
the model computed, rounded or brought from elsewhere, attached to a quote that
is real. It can only discard, and every discard is counted and logged.

*`Evidence.confidence` is verification strength*, 1.0 exact and 0.9
whitespace-flexible, because phase 5 does not let the model author a
confidence and the field is required. 0.9 is a label, not a measurement.
Phase 6 owns the claim's `conf` and should replace it rather than build on it.

*No passages means no call.* Asking anyway invites an answer from training
data (rule 3); the compare stage reads the empty result as `cannot-verify`.

*Providers.* `UPDATER_MODEL_PROVIDER` is `anthropic` (the default) or `gemini`.
**Gemini does not become the default until its live tests pass against a real
key**, and none was available when this was written, so they have not run.
Neither provider has a model id in code: `ANTHROPIC_MODEL` and `GEMINI_MODEL`
are required. The group code's `gemini-3.5-flash` is unverified, so it is not a
default anywhere; a live test checks that `GEMINI_MODEL` is in the API's model
list. No sampling parameters are sent to either provider. If extraction quality
argues for one, that should come from phase 11's evaluation, not a guess.

- **Claude** uses `output_config={"format": {"type": "json_schema", ...}}`, as
  `anthropic` 0.125 declares it.
- **Gemini** uses the `google-genai` SDK directly, not LlamaIndex as the
  prototype does: structured output is one config field, and the stage needs
  nothing an agent framework adds. `response_json_schema` with
  `response_mime_type="application/json"`, as SDK 2.29 requires. The key is
  `GEMINI_API_KEY`, else `GOOGLE_API_KEY` (which the prototype's `query.py`
  sets), **passed explicitly because the SDK on its own prefers
  `GOOGLE_API_KEY`**. The client is pinned to `vertexai=False`, or
  `GOOGLE_GENAI_USE_VERTEXAI` would silently move it to Vertex AI, where an API
  key does not work.
- **The schema is pinned to what both accept**: `anthropic.transform_schema`
  must leave it unchanged, and it may use only the keywords google-genai
  documents for `response_json_schema`.

*Live tests* are `uv run pytest -m live`, deselected by `addopts`, one run per
provider, skipped when that provider's key is absent. They read `.env` without
loading it into the process, so collecting them changes nothing else.

*Not done:* the discard count reaching the API (phase 9 wires the pipeline in),
assembling neighbourhoods of adjacent passages for thin table rows (each would
keep its own anchor), and phase 5's own check: run `updater extract` against a
real ASIL document and read every quote against the source.

**2026-10-08 — The dashboard runs on the API, served from the same origin.**
`front-end/index.html` is `docs/frontend/asil-dashboard-DRAFT-FINAL.html` with
every hardcoded array and static data row replaced by calls to `/api`. The
markup, CSS and behaviour are otherwise the prototype's, because the contract
kept its field names, so each screen renders the API response the way it
rendered the literal. A test fails if any of the prototype's literals come back.

*One origin.* `create_app(state, frontend=...)` mounts the directory at `/`
**after** the API's routes, so `/api/...`, `/docs` and `/openapi.json` win over
a file of the same name (there is a test). `updater serve` serves the checkout's
`front-end/` by default, takes `--frontend DIR`, and `--no-frontend` gives the
API alone. The default path is relative to the source checkout, so an
installed wheel runs the API alone rather than failing.

*The contract test* parses every `api(method, path)` call in the page and
requires each to be a route and method in the API's OpenAPI document. A typo in
the page is otherwise a 404 that just shows an empty screen.

*Which run.* There is no endpoint that lists runs, so the page remembers the run
it started in `localStorage`, and `?run=<id>` opens a given one. **`GET
/runs/{id}` advances the mock scan on every call**, so the page calls it only
while polling a scan in progress. On load it checks a remembered run through
`/claims`, which changes nothing; a 404 forgets the run.

*Escaping.* Every API value is escaped before it goes into markup. Claim notes
and notification bodies keep `<b>`, `<strong>`, `<i>`, `<em>` and `<q>`, with
no attributes, and lose everything else. That settles the 2026-10-01 note for
the frontend team: from phase 6 a note is model output, and model output is
never markup. A typed chat message is text, which the prototype got wrong.

*Copy changed where the prototype claimed something the system does not do.*
The sign-off toast and the paper footer said the report was published to the
website and Google Drive; nothing publishes anything (rule 5). The checklist
said "Two-step verification on", but there is no authentication. Two
notification buttons claimed to approve a source and confirm a URL; they now
open the registry, where those changes are made through the API. The
registry's "High priority" card had no data behind it, so it is now "Paused",
which the API can count. The approved prototype itself is unchanged apart from
its email addresses.

*Email addresses.* Every `@astartinlife.org.au` address in the prototype and
the page is `@example.org`, and the fixture's notification uses
`m.chen@example.org` to match. With the prototype in the repository, the
fixture `--check` test now runs, and it passes.

*Verified in a real browser.* Headless Chrome, driven over the DevTools
protocol against `updater serve` on a throwaway database, went through
registry, scan, extraction, draft review (approve, edit, note), sign-off, a
reload, adding and approving a source, and saving settings: 46 checks, each
confirmed against the API, with no JavaScript errors. That driver needs Chrome
and is not in the suite; the contract test is.

*Still demo-only*, with no API behind them: profile, notification preferences,
appearance, security, integrations, team, the test-suite panel, the assistant,
and notification read state. *Not yet:* the prior-report drop zone (phase 4),
and who "Kathleen D." is (the prototype's signed-in user; phase 7 decides).
