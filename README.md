# HITLUpdateAgent

## Overview

The Human-in-the-loop (HITL) Update Agent is an AI-driven research update assistant for the Non-Profit Org: A Start in Life. It is designed to automate the updating process by:

**1.** Re-checking preapproved sources

**2.** Detecting additions or changes

**3.** Extracting relevant findings

**4.** Generating a updated draft where every claim is strictly linked to a cited source

The backend is retrieval-first: every finding is anchored to a page, paragraph or cell range in an approved source, and when the sources do not support a claim it says so instead of filling the gap. `BUILD_SPEC.md` is the contract, `DESIGN.md` carries the five non-negotiable design rules and the decisions log, and `BACKEND_PLAN.md` is the build order.

## Tech Stack

- **Backend**: Python 3.12, the `updater` package (`src/updater`): ingestion, the approved-source registry, hybrid retrieval, evidence extraction with Claude or Gemini, and a FastAPI app for the dashboard
- **Frontend**: 
- **Database**: SQLite, with `sqlite-vec` for embeddings and FTS5 for full-text search
- **Documentation**: `BUILD_SPEC.md`, `DESIGN.md`, `BACKEND_PLAN.md`, `PROJECT_OVERVIEW.md`
- **Testing**: pytest
- **Build Tools**: uv, hatchling

## Getting Started

### Prerequisites

[uv](https://docs.astral.sh/uv/). The project pins Python 3.12; uv fetches it for you.

### Optional (Recommended)

An API key for evidence extraction: Anthropic (the default) or Gemini. Copy `.env.example` to `.env` and fill in the provider, its key and its model; `.env` is gitignored. Everything except `updater extract` and the live tests works without one.

### Installation

1. **Backend**:

   ```bash
   # every extra in one go: uv sync removes the extras it is not given
   uv sync --extra retrieve --extra api --extra gemini --extra prototype
   cp .env.example .env    # then add your API key and model
   uv run pytest
   ```

   `retrieve` is the ML stack (sentence-transformers and sqlite-vec, about 2 GB), `api` is the dashboard's HTTP API, `gemini` is the Gemini provider, and `prototype` is what the group's LlamaIndex prototype in `src/backend` needs. The embedding model downloads from the Hugging Face hub on first use. Behind a TLS-intercepting proxy that fails with a certificate error dressed up as *"couldn't connect to huggingface.co"*; `src/updater/certs.py` handles it.

2. **Frontend**:

   ```bash
   ///
   TO BE PUT
   ///
   ```

3. **Documentation**:

   ```bash
   ///
   TO BE PUT
   ///
   ```

### Development

- Backend API docs: `uv run updater serve`, then http://127.0.0.1:8000/docs
- Frontend: 
- Component library: 
- Documentation: 

### Using the backend

Sources are approved by adding them to `registry.yaml`. Nothing outside that file can be indexed or cited; `updater ingest` refuses an unregistered path.

```sh
uv run updater ingest                          # index every source in the registry
uv run updater ingest data/sources/x.docx      # ... or just this one, if approved
uv run updater search "apprenticeship starts in the North East"
uv run updater extract "Youth unemployment in Western Sydney was 11.4%"
```

`updater search` prints, for each hit, the score, the citation, the chunk id, the character offsets and the text, so a result can be checked against the document by hand. `updater extract` asks the configured model for evidence about a claim and prints every finding whose quote was found, verbatim, in its passage, with its citation, plus how many findings were discarded.

To look at extraction on a document before it is approved:

```sh
uv run python scripts/dump_passages.py data/sources --chunk
uv run python scripts/dump_passages.py data/sources --worst 10
```

The group's Gemini prototype still runs as before:

```sh
cd src/backend && uv run python query.py
```

## Project Structure

```
.
├── src/
│   ├── updater/          # the backend package
│   │   ├── registry/     # approved-source registry: nothing else can be cited
│   │   ├── ingest/       # one extractor per format, each returning anchored passages
│   │   ├── retrieve/     # chunking, embedding, SQLite storage, hybrid search
│   │   ├── extract/      # evidence extraction and quote verification (Claude or Gemini)
│   │   ├── compare/      # four-way claim comparison (not built yet)
│   │   ├── draft/        # draft assembly with citations (not built yet)
│   │   ├── eval/         # evaluation harness (not built yet)
│   │   ├── api/          # FastAPI app the dashboard runs on
│   │   └── cli.py        # updater ingest / search / extract / serve
│   └── backend/          # the group's Gemini/LlamaIndex prototype
├── tests/
├── scripts/              # passage inspector, prototype fixture extraction
├── data/sources/         # documents to ingest; gitignored except its README
├── registry.yaml         # the approved sources
├── front-end/            # to come
├── docs/                 # to come
├── .gitlab-ci.yml        # CI/CD pipeline (to come)
└── README.md
```

## Testing

- **Backend**: `uv run pytest`. The paid live tests against the model APIs are deselected by default; run them with `uv run pytest -m live`.
- **Frontend**:

## Documentation

For detailed guides on setup, development, architecture, and usage, please refer to the [full documentation](TO BE DECIDED) (run `TO BE DECIDED` to start the docs server).

## License

This project is part of UTS Software Design Development Course work. All
