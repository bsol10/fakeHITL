# data/sources — documents to ingest

Put the real files here: PDFs, `.docx`, saved `.html`. Anything in this
directory other than this README is gitignored, because it is client material
and the repo is not where it lives.

Then look at what came out:

```sh
uv run python scripts/dump_passages.py data/sources
```

That prints, for every passage of every file, the locator, the character
offsets, the citation as a human would read it, and the raw extracted text.

```sh
# one file, nothing truncated
uv run python scripts/dump_passages.py data/sources/report.pdf --limit 0 --chars 0

# first 2 passages per file, short excerpts
uv run python scripts/dump_passages.py --limit 2 --chars 200
```

## What to check when you read the output

- **The locator points where the text actually is.** Open the PDF at the page
  the anchor names and confirm the text is on it. For DOCX, `paragraph_index`
  counts empty paragraphs, so it addresses the real document.
- **`offsets` says `whole unit`.** At ingest a passage *is* the locator unit, so
  the anchor spans all of it. `PARTIAL` here means something narrowed an anchor,
  which nothing should be doing before chunking exists.
- **The raw text is prose, not garbage.** Ligature soup or run-together words
  means the text layer is bad and the file needs OCR — the quality gate catches
  the worst of it, but it is tuned to reject, not to judge readability.
- **No duplicate-locator warning.** Two passages claiming one location makes
  both citations uncheckable.

## Failure modes it will tell you about

| Output | Meaning |
|---|---|
| `NEEDS OCR` | Scanned PDF, text layer under 100 chars/page. OCR is not built yet. |
| `UNSUPPORTED` | Sniffed to a type with no adapter. PDF, HTML and DOCX are wired; XLSX, CSV, PPTX, TXT/MD are not. |
| `EXTRACTION FAILED` | The quality gate rejected a passage, so the whole document fails. The message names each one. |

The script builds a throwaway `Source` from the filename, because the
approved-source registry does not exist yet. That is fine for looking at
extraction output and **not** fine for anything that produces a draft — design
rule 3 means the pipeline takes its sources from the registry, not from a
directory listing.
