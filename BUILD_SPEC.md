# Annual Research Update Agent — Build Spec

Client: A Start in Life (ASIL). Retrieval-first agent that re-checks approved
sources each year, detects what changed, and drafts updated findings where every
claim is anchored to a source passage.

This document is the contract. Build it phase by phase, not all at once.

---

## 1. Non-negotiable design rules

These exist because breaking them destroys the project's core value proposition.

1. **Provenance is never lost.** Every piece of text in the system carries a
   `SourceAnchor` from the moment it is extracted until it appears in the final
   draft. There is no stage where text becomes a bare string.
2. **Abstention over confabulation.** If evidence for a claim is not retrieved,
   the system emits `cannot-verify` and says so in the draft. It never fills a
   gap with plausible-sounding text.
3. **Approved sources only.** No open web search, no model world-knowledge in
   the draft. If it isn't in the registry, it isn't evidence.
4. **Every material claim carries a citation.** A draft sentence without an
   anchor is a bug, and the test suite treats it as one.
5. **Human sign-off is mandatory.** The system produces drafts, never published
   output.

---

## 2. Data model

Define these first, in `models.py`, before writing any pipeline code. Everything
else depends on them. Use dataclasses or Pydantic.

```
Source
  id, title, url_or_path, owner, topic, year, update_frequency,
  media_type, last_checked, last_content_hash

SourceAnchor          # the citation primitive — format-specific location
  source_id
  locator             # discriminated union, see §3
  char_start, char_end
  extracted_at

Passage
  id, source_id, text, anchor: SourceAnchor, token_count

Evidence              # structured extraction target
  finding             # str
  statistic           # str | None   — null, never guessed
  method              # str | None
  as_of_date          # date | None
  caveat              # str | None
  anchor: SourceAnchor
  confidence          # float

Claim                 # a unit of the prior-year report
  id, text, prior_anchor

ComparisonResult
  claim_id
  status              # unchanged | updated | new | cannot-verify
  current_evidence    # list[Evidence]
  rationale           # str, why this status
```

`Evidence.statistic` being nullable is load-bearing. A model that must emit a
number will invent one. Make null the easy path.

---

## 3. Ingestion — the multi-format layer

This is the part you asked about. The design is an **adapter registry**: one
extractor per media type, all returning the same `list[Passage]`, each with a
format-appropriate locator.

### Locator types

```
PageLocator     { page: int }                        # pdf
BlockLocator    { paragraph_index: int, style: str } # docx, md, txt
CellLocator     { sheet: str, cell_range: str }      # xlsx, csv
SlideLocator    { slide: int, shape_index: int }     # pptx
DomLocator      { css_path: str, heading_trail: [] } # html
TimeLocator     { start_sec: float, end_sec: float } # audio/video transcript
```

Adding a format means adding one adapter and one locator variant. Nothing else
in the pipeline changes. That is the whole point of the design.

### Adapter interface

```python
class Extractor(Protocol):
    media_types: set[str]
    def extract(self, path: Path, source: Source) -> list[Passage]: ...
```

Register them in a dict keyed by media type. Dispatch on sniffed type, not file
extension — use `python-magic` or `filetype`, because ASIL staff will upload a
`.txt` that is actually a PDF.

### Adapters to build, in priority order

| Format | Library | Notes |
|---|---|---|
| PDF (text layer) | `pypdf` or `pdfplumber` | `pdfplumber` if you need tables |
| PDF (scanned) | `ocrmypdf` → then above | Detect: text layer under ~100 chars/page |
| HTML | `trafilatura` | Strips nav/boilerplate; keep heading trail |
| DOCX | `python-docx` | Paragraph index is your locator |
| XLSX / CSV | `pandas` + `openpyxl` | Serialise rows as text, keep cell range |
| PPTX | `python-pptx` | Per-shape text, slide number as locator |
| TXT / MD | stdlib | Paragraph split |
| Images | `pytesseract` | Treat as single-page scanned doc |

Build PDF, HTML, DOCX first. Those cover the overwhelming majority of nonprofit
research sources. Everything else is a bonus.

### Extraction quality gate

Every adapter runs its output through a check before the passages are accepted:

- Non-empty text
- Ratio of alphanumeric to total characters above ~0.6 (catches OCR garbage)
- No single "word" longer than 50 characters (catches ligature/encoding failure)

Failures are logged against the source and surfaced in the UI as
`extraction-failed`. They do not silently become empty evidence.

---

## 4. Pipeline stages

```
register → fetch → extract → chunk → embed → retrieve → extract_evidence
         → compare → draft → review
```

**fetch** — Pull each source. Compute SHA-256 of normalised extracted text (not
raw bytes — a PDF re-export with a new timestamp changes bytes but not content).
Store a snapshot. Compare against `last_content_hash`.

**chunk** — ~400–600 tokens with ~15% overlap. **Chunk within locator
boundaries, never across them.** A chunk must not span two PDF pages, or its
anchor is ambiguous. This is the rule that keeps citations honest.

**embed** — Any local model is fine (`sentence-transformers`,
`all-MiniLM-L6-v2`). Store in SQLite with `sqlite-vec`, or Chroma. Do not stand
up a separate vector database service for a proof of concept.

**retrieve** — For each `Claim` from the prior report, retrieve top-k passages.
Hybrid is worth it: BM25 (`rank_bm25`) plus dense, merged. Statistics and
proper nouns are exactly what dense retrieval misses.

**extract_evidence** — LLM call per claim with retrieved passages. Structured
output only. Prompt must instruct: use only the supplied passages, return null
for anything not stated, quote the supporting span verbatim.

**Verify the quoted span actually appears in the passage** with a string match.
If it doesn't, discard the evidence. This is a cheap, extremely effective
hallucination guard — implement it, do not skip it.

**compare** — Classify each prior claim. `updated` requires evidence that
contradicts or supersedes; `unchanged` requires evidence that corroborates;
`cannot-verify` is the default when retrieval came back thin. Every status
carries a written rationale.

**draft** — Assemble markdown. Every material sentence gets an inline citation
referencing a `SourceAnchor`. Claims marked `cannot-verify` appear in a clearly
flagged section, not silently dropped — a missing finding is more dangerous than
a flagged one.

**review** — Reviewer sees each claim, its status, evidence, and the source
passage side by side. Approve / edit / reject per claim. Decisions persist.

---

## 5. Build order

Phase boundaries are demo boundaries. At the end of each, something runs.

**Phase 1 — Skeleton (week 5–6)**
Models. Registry as YAML. PDF + HTML + DOCX adapters. Naive retrieval. Evidence
extraction. Ugly-but-real draft with citations. Runs end to end from CLI.

**Phase 2 — Change detection (week 7–8)**
Hashing, snapshots, the four-way comparison, rationales. This is the feature
ASIL actually wants.

**Phase 3 — Quality (week 9–10)**
Hybrid retrieval. Span verification. Abstention behaviour. Evaluation set and
regression tests. Remaining adapters if time allows.

**Phase 4 — Review and handover (week 11–12)**
Review UI (Streamlit or FastAPI + minimal HTML — do not build a React app).
Documentation. Handover pack. Code freeze one week before submission.

---

## 6. Evaluation

Build this in Phase 3, not at the end. Without it you cannot claim the system
works.

- **Citation fidelity** — sample 30 draft claims, manually verify each anchor
  resolves to a passage that actually supports it. Target: above 95%.
- **Abstention** — deliberately remove a source, confirm affected claims flip to
  `cannot-verify` rather than being answered from model knowledge.
- **Comparison accuracy** — hand-label 20 claims against known-changed sources,
  measure classification agreement.
- **Regression** — the above run as a test suite on every change.

---

## 7. Do not build

Auth. Multi-tenancy. A React frontend. Async job queues. Docker orchestration.
Fine-tuning. An agent framework. A chatbot interface.

The brief explicitly says the first version is not a chatbot. Every hour spent
on infrastructure is an hour not spent on citation quality, which is the only
thing that makes this project defensible.
