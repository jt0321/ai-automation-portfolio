"""
pipeline/retrieval.py
Hybrid retrieval: structured metadata filters + pgvector cosine similarity,
exposed as a LangChain BaseRetriever so it composes with LCEL chains.
Returns ranked score segment and text source chunks for a natural-language query.
"""

from __future__ import annotations
import re
from typing import Optional
from sqlalchemy import text
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from db.session import session_scope
from pipeline.embedder import embed_single

# Full-text search over work identity metadata (composer/title/opus/nickname),
# matched against the raw query text — kept entirely separate from the
# segment embeddings, which only encode harmonic/texture analysis text and
# have no way to "know" a nickname like "Moonlight" or an opus number.
_WORKS_METADATA_TSVECTOR = (
    "to_tsvector('english', coalesce(composer,'') || ' ' || coalesce(title,'') "
    "|| ' ' || coalesce(opus,'') || ' ' || coalesce(nickname,''))"
)

# Words that appear in nearly every work's metadata in this corpus (every
# title is "Piano Sonata No. N in <key>", every composer is the same person)
# — matching on these alone would "match" the whole corpus and defeat the
# purpose of narrowing by work identity, so they're excluded from the
# per-word lookup below.
_GENERIC_METADATA_WORDS = {
    "piano", "sonata", "sonatas", "no", "op", "in", "major", "minor",
    "ludwig", "van", "beethoven",
}


def _match_work_ids_by_metadata(session, query: str) -> list[int]:
    """
    Work ids whose composer/title/opus/nickname match a *distinctive* word
    from the query (e.g. "Moonlight", "111", "Waldstein") — generic words
    that show up in every work's metadata (see _GENERIC_METADATA_WORDS) are
    ignored so they don't cause a no-op match against the entire corpus.
    """
    words = [w for w in re.findall(r"[A-Za-z0-9']+", query) if w.lower() not in _GENERIC_METADATA_WORDS]
    if not words:
        return []

    rows = session.execute(
        text(f"""
            SELECT w.id FROM works w
            WHERE EXISTS (
                SELECT 1 FROM unnest(:words) AS word
                WHERE plainto_tsquery('english', word) @@ {_WORKS_METADATA_TSVECTOR}
            )
        """),
        {"words": words},
    ).fetchall()
    return [r[0] for r in rows]


class HybridScoreRetriever(BaseRetriever):
    """
    Retrieves score_segments + text_sources via pgvector cosine similarity,
    optionally narrowed by structured filters on the score segments.
    """

    composer: Optional[str] = None
    local_key: Optional[str] = None
    formal_function: Optional[str] = None
    texture_tag: Optional[str] = None
    top_k: int = 8
    model: Optional[str] = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        with session_scope() as session:
            query_vec = embed_single(query, model=self.model)

            segment_filters = ["1=1"]
            text_filters = ["1=1"]
            params: dict = {"vec": str(query_vec), "k": self.top_k}

            if self.composer:
                segment_filters.append("w.composer ILIKE :composer")
                text_filters.append("w.composer ILIKE :composer")
                params["composer"] = f"%{self.composer}%"
            if self.local_key:
                segment_filters.append("ss.local_key ILIKE :local_key")
                params["local_key"] = f"%{self.local_key}%"
            if self.formal_function:
                segment_filters.append("ss.formal_function = :formal_function")
                params["formal_function"] = self.formal_function
            if self.texture_tag:
                segment_filters.append("ss.texture_tag = :texture_tag")
                params["texture_tag"] = self.texture_tag

            # If the query names a work by title/opus/nickname (e.g. "the
            # Moonlight sonata", "Op. 111"), narrow both searches to those
            # works. This is a metadata lookup only — it doesn't touch the
            # segment/text-source embeddings at all.
            matched_work_ids = _match_work_ids_by_metadata(session, query)
            if matched_work_ids:
                segment_filters.append("ss.work_id = ANY(:work_ids)")
                text_filters.append("ts.work_id = ANY(:work_ids)")
                params["work_ids"] = matched_work_ids

            where_clause = " AND ".join(segment_filters)

            segment_sql = text(f"""
                SELECT
                    ss.id,
                    ss.work_id,
                    w.composer,
                    w.title,
                    w.opus,
                    ss.measure_start,
                    ss.measure_end,
                    ss.local_key,
                    ss.roman_numerals,
                    ss.harmonic_rhythm,
                    ss.texture_tag,
                    ss.formal_function,
                    ss.summary_text,
                    ss.musicxml_slice,
                    1 - (ss.embedding <=> CAST(:vec AS vector)) AS cosine_similarity
                FROM score_segments ss
                JOIN works w ON w.id = ss.work_id
                WHERE {where_clause}
                ORDER BY ss.embedding <=> CAST(:vec AS vector)
                LIMIT :k
            """)
            seg_rows = session.execute(segment_sql, params).mappings().all()

            # --- Text sources: vector search, narrowed by the same work match ---
            text_where_clause = " AND ".join(text_filters)
            text_sql = text(f"""
                SELECT
                    ts.id,
                    w.composer,
                    w.title,
                    ts.source_type,
                    ts.content,
                    ts.url,
                    1 - (ts.embedding <=> CAST(:vec AS vector)) AS cosine_similarity
                FROM text_sources ts
                JOIN works w ON w.id = ts.work_id
                WHERE {text_where_clause}
                ORDER BY ts.embedding <=> CAST(:vec AS vector)
                LIMIT :k
            """)
            text_rows = session.execute(text_sql, params).mappings().all()

            documents = []
            for r in seg_rows:
                row = dict(r)
                documents.append(
                    Document(page_content=row.get("summary_text") or "", metadata={**row, "kind": "segment"})
                )
            for r in text_rows:
                row = dict(r)
                documents.append(
                    Document(page_content=row.get("content") or "", metadata={**row, "kind": "text_source"})
                )
            return documents


def retrieve(
    query: str,
    composer: Optional[str] = None,
    local_key: Optional[str] = None,
    formal_function: Optional[str] = None,
    texture_tag: Optional[str] = None,
    top_k: int = 8,
    model: Optional[str] = None,
) -> dict:
    """
    Hybrid retrieval over score_segments and text_sources, via HybridScoreRetriever.

    Returns:
        {
          "segments": [...],   # list of score_segment rows
          "text_sources": [...] # list of text_source rows
        }
    """
    retriever = HybridScoreRetriever(
        composer=composer,
        local_key=local_key,
        formal_function=formal_function,
        texture_tag=texture_tag,
        top_k=top_k,
        model=model,
    )
    documents = retriever.invoke(query)

    segments = []
    text_sources = []
    for doc in documents:
        row = {k: v for k, v in doc.metadata.items() if k != "kind"}
        if doc.metadata.get("kind") == "segment":
            segments.append(row)
        else:
            text_sources.append(row)

    return {"segments": segments, "text_sources": text_sources}
