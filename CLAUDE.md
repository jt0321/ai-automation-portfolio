# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ScoreChat is a symbolic-score RAG system for classical piano music in Humdrum (`.krn`) format. The design principle: the raw score and deterministic `music21`-derived analysis are the source of musical truth; retrieval/embeddings and the LLM only locate and explain evidence-backed passages — they never invent or override musical facts. Keep this separation in mind for any change: analytical claims must trace back to `score_measures`/`measure_analyses`/`span_analyses`, not to LLM output.

## Commands

Setup (uses `uv`, per memory prefer `uv pip install` over bare `pip`):
```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
docker compose up -d          # postgres + pgvector, schema auto-applied from db/schema.sql on first init
```

If the DB predates a schema change, apply migrations manually (not auto-applied on existing volumes):
```bash
psql "$DATABASE_URL" -f db/migrations/001_symbolic_layers.sql
psql "$DATABASE_URL" -f db/migrations/002_span_analysis.sql
```

Ingestion pipeline:
```bash
python download_beethoven_piano_sonatas.py --sonata 32   # fetch .krn from craigsapp/beethoven-piano-sonatas
python ingest_scores.py                                  # parse -> analyze -> MEI -> embed -> store
python ingest_scores.py --symbolic-only                  # skip MEI/embeddings, just rebuild symbolic layers
```

Run the app:
```bash
python server.py                    # API + static HTML/JS client at http://localhost:8000
streamlit run scorechat_app.py      # alternative Streamlit chat UI
```

Tests:
```bash
pytest                              # full suite
pytest tests/test_analyzer.py -k test_detect_texture_with_music21   # single test
```
Some `test_analyzer.py` cases are skipped unless `tests/fixtures/sample.musicxml` exists.

## Architecture

Ingestion pipeline: Humdrum `.krn` → `music21` parse → canonical measure encoding → versioned symbolic analysis → optional retrieval segments/embeddings → MEI (via Verovio) for browser rendering. See `README.md`'s Mermaid diagram and "Symbolic Score Data Model" section for the full table-by-table breakdown (`score_sources`, `score_measures`, `measure_analyses`, `analysis_runs`, `span_analyses`, `span_relations`).

Key modules:
- `analysis/analyzer.py` — all `music21`-based feature extraction: measure encoding, key/Roman-numeral/texture candidates, and `build_span_candidates` (span analysis at meter changes, notated directions, structural barlines — does not infer form/theme labels).
- `db/models.py`, `db/schema.sql`, `db/store.py`, `db/session.py` — SQLAlchemy models, raw schema (source of truth for the DB, applied via docker-compose init and `db/migrations/*.sql` for existing DBs), persistence/query helpers (e.g. `get_measure_evidence`).
- `pipeline/retrieval.py` — pgvector cosine similarity search over `score_segments`/text sources.
- `pipeline/embedder.py` — embedding generation via the configured `EMBEDDING_PROVIDER`.
- `pipeline/providers.py` — env-driven selection of chat/embedding backends (`CHAT_PROVIDER`/`EMBEDDING_PROVIDER`: openai/anthropic/ollama/gemini) via LangChain, so no code is hardcoded to OpenAI. Anthropic has no embeddings API. Switching `EMBEDDING_PROVIDER` to a model with a different output dimension than the schema's `vector(1536)` columns requires a schema migration + full re-embedding.
- `pipeline/chat.py` — RAG chain (prompt | chat model | parser). Builds LLM context from retrieved segments, capping unique measures per response (`MAX_SYMBOLIC_CONTEXT_MEASURES`) and injecting `symbolic_evidence` JSON per measure so answers are grounded and measure-cited.
- `pipeline/mei_converter.py` — MusicXML → MEI via Verovio bindings, for exact SVG notation rendering in-browser.
- `frontend/index.html`, `frontend/score_viewer.html` — HTML/JS client; renders notation slices client-side via Verovio WASM.
- `server.py` — API + static file server backing the HTML/JS client.
- `scorechat_app.py` — alternate Streamlit-based chat client.

Span/relation review lifecycle: `span_analyses` and `span_relations` rows carry a status of `proposed`, `accepted`, or `rejected`. The current pipeline only ever creates `proposed` rows — there is no UI yet for accept/reject, and no analyser yet generates `span_relations` (needs a symbolic comparison analyser). Don't assume higher-level form/theme/motif claims exist; they're explicitly deferred until backed by real symbolic comparison, not inferred from embeddings.

## Environment

Copy `.env.example` to `.env`. Required: `OPENAI_API_KEY` (default provider), `DATABASE_URL` (matches `docker-compose.yml` defaults for local dev). Optional provider keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_BASE_URL`) only needed if `CHAT_PROVIDER`/`EMBEDDING_PROVIDER` are switched — install the matching extra first, e.g. `uv pip install -e ".[anthropic]"`.
