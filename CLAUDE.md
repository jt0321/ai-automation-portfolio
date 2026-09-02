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
python build_relations.py                                # propose span_relations from stored data (no .krn needed)
python build_relations.py --work-id 197 --dry-run        # tune thresholds without writing
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
- `analysis/analyzer.py` — score loading plus all `music21`-based feature extraction: measure encoding, texture candidates, and `build_span_candidates` (span analysis at meter changes, notated directions, structural barlines — does not infer form/theme labels). Harmony is delegated to `analysis/harmony.py` as a second pass over the finished canonical layer.
  **Always load scores through `load_score()`, never `converter.parse()` directly.** music21's Humdrum reader silently truncates scores containing nested spine splits (Op. 14 No. 1/i parsed 12 of its 162 measures), so `load_score` checks the parse against the `=N` barlines the source notates and falls back to Verovio's humlib importer via MEI when it comes up short — 4 of the 103 movements need this. That MEI in turn carries key/meter in elements music21's MEI reader ignores, so `humdrum_signatures()` reads them from the raw `.krn` and they are stamped onto the opening measure; without that the recovered scores lose the key-signature anchor entirely. `ingest_scores.py` re-checks the same barline count and refuses to store a short parse, because a truncated score looks perfectly healthy downstream.
- `analysis/harmony.py` — key trajectory and chord/Roman-numeral analysis. A *pure function of `score_measures.symbolic_data`*, not of a music21 parse, so a harmonic pass is reproducible from the DB alone and re-runnable without the source `.krn` — preserve that seam. Key estimation is windowed + Viterbi-smoothed and anchored to two pieces of notated evidence: the staff key signature (`SIGNATURE_BONUS`) and the bass of the final sounding measure (`HOME_KEY_BONUS`), which is what separates a key from its relative. Chords are fitted to beat-sized windows by template matching, so output tracks harmonic rhythm rather than note onsets, and spans below `MIN_CHORD_CONFIDENCE` are deliberately left unlabelled. Two rules there are load-bearing and each has a ground-truth test: a window with fewer than `MIN_DISTINCT_PITCH_CLASSES` cannot determine a triad and borrows pitch content from the *narrowest* surrounding context that can (an arpeggiated measure is one chord, not one per note); and the bass is the lowest *sustained* pitch in the window (`BASS_MIN_DURATION_SHARE`), which is neither the lowest note to occur — a figure dipping below the bass would invert the chord — nor the note on the beat, since an accompaniment often enters under a held melody. Reported `correlation` and `confidence` deliberately exclude the bonus/bass weightings used to *select* an answer — keep selection and reporting separate, since these values reach the LLM as evidence. Tuning constants are pinned by ground-truth tests in `tests/test_harmony.py`; re-run those after touching them.
- `db/models.py`, `db/schema.sql`, `db/store.py`, `db/session.py` — SQLAlchemy models, raw schema (source of truth for the DB, applied via docker-compose init and `db/migrations/*.sql` for existing DBs), persistence/query helpers (e.g. `get_measure_evidence`).
- `pipeline/retrieval.py` — pgvector cosine similarity search over `score_segments`/text sources.
- `pipeline/embedder.py` — embedding generation via the configured `EMBEDDING_PROVIDER`.
- `pipeline/providers.py` — env-driven selection of chat/embedding backends (`CHAT_PROVIDER`/`EMBEDDING_PROVIDER`: openai/anthropic/ollama/gemini) via LangChain, so no code is hardcoded to OpenAI. Anthropic has no embeddings API. Switching `EMBEDDING_PROVIDER` to a model with a different output dimension than the schema's `vector(1536)` columns requires a schema migration + full re-embedding.
- `pipeline/chat.py` — RAG chain (prompt | chat model | parser). Builds LLM context from retrieved segments, capping unique measures per response (`MAX_SYMBOLIC_CONTEXT_MEASURES`) and injecting `symbolic_evidence` JSON per measure so answers are grounded and measure-cited.
- `pipeline/mei_converter.py` — MusicXML → MEI via Verovio bindings, for exact SVG notation rendering in-browser. `mei_to_svg` takes **printed** measure numbers and translates them through `measure_ordinals()`; see the measure numbering note below.
- `frontend/index.html`, `frontend/score_viewer.html` — HTML/JS client; renders notation slices client-side via Verovio WASM.
- `server.py` — API + static file server backing the HTML/JS client.
- `scorechat_app.py` — alternate Streamlit-based chat client.

Measure numbering — two numbering schemes exist and confusing them fails silently (the viewer just shows neighbouring bars):
- **Printed/engraved** (`score_measures.measure_number`) — what a performer reads, what a user types, and what the LLM must cite. Bar 1 is the first *complete* measure; an anacrusis is not counted and is stored as `0`, as is any measure music21 could not number (so `0` is not unique — never use it as a range bound).
- **Internal** (`score_measures.measure_index`) — 0-based position including unnumbered measures. All span/relation work (`span_analyses.measure_start_index`, `analysis/span_relations.py`) keys on this.

Verovio agrees with printed numbering in its MEI (`@n` absent on a pickup), but its `select({"measureRange": ...})` counts **ordinal positions from 1**, in which the pickup *is* position 1 — so printed bar N is ordinal N+1 wherever an anacrusis exists. Translate via `measure_ordinals()` (Python) or `measureInfo()` (`frontend/score_viewer.html`); `frontend/index.html` sidesteps it by resolving `@n` to `xml:id` and navigating with `getPageWithElement`. Note the older `select` *option* is silently unsupported in Verovio 6 and renders the whole movement — use the `select()` **method**, with a dict, before `loadData`.

- `analysis/span_relations.py` — symbolic comparison between spans, and the relations pass (`build_span_relations`, driven by `build_relations.py`). Depends only on the database, never on the source `.krn`, so it is re-runnable and re-tunable without re-ingesting. Three things carry the quality of the output and each has tests: reference spans come from a **uniform tiling** (`REFERENCE_WINDOW_LENGTHS`) as well as from `span_analyses`, because boundary candidates break at every notated direction and so are not a thematic index (two thirds are 1–2 measures, while a movement with no directions collapses to one span — relying on them alone left a quarter of the corpus with no relations); overlapping matches of one return collapse to the strongest (`_select_distinct_matches`); and relations sharing a target-minus-source *offset* with touching source ranges merge into one (`_merge_by_offset`), taking the weakest part's confidence, while ranges separated by a gap stay apart since that gap was never compared. `corroborate_key_match` compares the windowed `local_key`, not `global_key` — the latter is now one value per movement and would corroborate everything.

Span/relation review lifecycle: `span_analyses` and `span_relations` rows carry a status of `proposed`, `accepted`, or `rejected`. Every row the pipeline creates is `proposed` — there is no UI yet for accept/reject. `span_relations` now carries `repeats`/`varies` proposals from the pass above; the other relation types (`inverts`, `diminishes`, `augments`, `changes_meter_from`, `contrasts_with`) still have no analyser. Relations are evidence, not form labels: a target range with no span of its own gets one created as a `candidate`, never as a `theme` or `variation`, and `returns_in_same_key` is recorded alongside the confidence rather than folded into it, because distinguishing a tonic recapitulation from a transposed restatement is exactly what downstream form analysis needs.

## Environment

Copy `.env.example` to `.env`. Required: `OPENAI_API_KEY` (default provider), `DATABASE_URL` (matches `docker-compose.yml` defaults for local dev). Optional provider keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_BASE_URL`) only needed if `CHAT_PROVIDER`/`EMBEDDING_PROVIDER` are switched — install the matching extra first, e.g. `uv pip install -e ".[anthropic]"`.
