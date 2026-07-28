"""
db/store.py
Persists works, score assets, score segments (with embeddings),
and text source chunks to Postgres via SQLAlchemy.
"""

from __future__ import annotations
from pathlib import Path
from sqlalchemy import text
from db.models import Work, ScoreAsset, ScoreSegment, TextSource
from db.session import session_scope
from analysis.analyzer import MeasureChunk
from pipeline.embedder import embed_texts


def upsert_work(metadata: dict) -> int:
    """Insert or update a Work record. Returns work.id."""
    with session_scope() as session:
        existing = (
            session.query(Work)
            .filter_by(composer=metadata["composer"], title=metadata["title"])
            .first()
        )
        if existing:
            for k, v in metadata.items():
                setattr(existing, k, v)
            session.commit()
            return existing.id

        work = Work(**metadata)
        session.add(work)
        session.commit()
        return work.id


def store_asset(work_id: int, asset_type: str, file_path: str,
                page_number: int | None = None, omr_tool: str | None = None,
                omr_quality: str = "auto") -> int:
    with session_scope() as session:
        asset = ScoreAsset(
            work_id=work_id, asset_type=asset_type,
            file_path=str(file_path), page_number=page_number,
            omr_tool=omr_tool, omr_quality=omr_quality
        )
        session.add(asset)
        session.commit()
        return asset.id


def store_segments(work_id: int, chunks: list[MeasureChunk]) -> None:
    """Embed all chunk summaries and bulk-insert into score_segments."""
    texts = [c.summary_text for c in chunks]
    vectors = embed_texts(texts)

    with session_scope() as session:
        for chunk, vec in zip(chunks, vectors):
            seg = ScoreSegment(
                work_id         = work_id,
                part            = chunk.part,
                measure_start   = chunk.measure_start,
                measure_end     = chunk.measure_end,
                local_key       = chunk.local_key,
                roman_numerals  = chunk.roman_numerals,
                harmonic_rhythm = chunk.harmonic_rhythm,
                texture_tag     = chunk.texture_tag,
                formal_function = chunk.formal_function,
                motif_tags      = chunk.motif_tags or [],
                summary_text    = chunk.summary_text,
                musicxml_slice  = chunk.musicxml_slice,
                embedding       = vec,
            )
            session.add(seg)

        session.commit()


def store_text_chunks(work_id: int, chunks: list[dict]) -> None:
    """
    chunks: list of dicts with keys: source_type, content, url (optional)
    Embeds each chunk and inserts into text_sources.
    """
    texts = [c["content"] for c in chunks]
    vectors = embed_texts(texts)

    with session_scope() as session:
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            ts = TextSource(
                work_id     = work_id,
                source_type = chunk["source_type"],
                content     = chunk["content"],
                chunk_index = i,
                embedding   = vec,
                url         = chunk.get("url"),
            )
            session.add(ts)

        session.commit()


def list_works() -> list[dict]:
    """
    List all ingested works (id, composer, title, opus, nickname, work_number,
    movement_number, tempo_indication) for the sidebar/work picker, in natural
    sonata/movement order (No. 5-9 before No. 10, No. 32 after No. 29) rather
    than lexicographic title order.
    """
    with session_scope() as session:
        rows = session.execute(text(r"""
            SELECT id, composer, title, opus, nickname,
                   work_number, movement_number, tempo_indication
            FROM works
            ORDER BY
                composer,
                COALESCE(work_number, 0),
                COALESCE(movement_number, 0),
                title
        """)).mappings().all()
        return [dict(r) for r in rows]


def get_work_mei(work_id: int) -> str | None:
    """Return the full MEI XML content for a work's generated MEI asset, or None."""
    with session_scope() as session:
        asset = (
            session.query(ScoreAsset)
            .filter_by(work_id=work_id, asset_type="mei")
            .order_by(ScoreAsset.id.desc())
            .first()
        )
        if not asset:
            return None
        path = Path(asset.file_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")


def clear_work_segments_and_assets(work_id: int) -> None:
    """Clear segments, assets, and text sources for a work to allow clean re-ingestion."""
    with session_scope() as session:
        session.query(ScoreSegment).filter_by(work_id=work_id).delete()
        # Delete text sources (like wikipedia or imslp text chunks)
        session.query(TextSource).filter_by(work_id=work_id).delete()
        # Delete all assets (PDF will be re-added by the ingestion script)
        session.query(ScoreAsset).filter_by(work_id=work_id).delete()
        session.commit()
