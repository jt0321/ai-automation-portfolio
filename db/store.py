"""
db/store.py
Persists works, score assets, score segments (with embeddings),
and text source chunks to Postgres via SQLAlchemy.
"""

from __future__ import annotations
import hashlib
from pathlib import Path
from sqlalchemy import text
from db.models import (
    Work, ScoreAsset, ScoreSegment, TextSource, ScoreSource, ScoreMeasure,
    MeasureAnalysis,
)
from db.session import session_scope
from analysis.analyzer import CanonicalMeasure, MeasureChunk, PerMeasureAnalysis
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


def store_symbolic_source(work_id: int, file_path: str, source_url: str | None = None) -> int:
    """Persist an immutable Humdrum source copy, checksum, and provenance."""
    path = Path(file_path)
    raw_content = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    with session_scope() as session:
        source = ScoreSource(
            work_id=work_id,
            format="humdrum-kern",
            file_path=str(path),
            source_url=source_url,
            sha256=digest,
            raw_content=raw_content,
        )
        session.add(source)
        session.commit()
        return source.id


def store_symbolic_layers(
    work_id: int,
    measures: list[CanonicalMeasure],
    analyses: list[PerMeasureAnalysis],
) -> None:
    """Store canonical notation and versioned measure analysis atomically."""
    analyses_by_index = {analysis.measure_index: analysis for analysis in analyses}
    with session_scope() as session:
        for encoded_measure in measures:
            measure = ScoreMeasure(
                work_id=work_id,
                measure_index=encoded_measure.measure_index,
                measure_number=encoded_measure.measure_number,
                symbolic_data=encoded_measure.symbolic_data,
            )
            session.add(measure)
            session.flush()
            analysis = analyses_by_index.get(encoded_measure.measure_index)
            if analysis is not None:
                session.add(MeasureAnalysis(
                    measure_id=measure.id,
                    analysis_version=analysis.analysis_data["analysis_version"],
                    analysis_data=analysis.analysis_data,
                ))
        session.commit()


def get_measure_evidence(work_id: int, measure_start: int, measure_end: int) -> list[dict]:
    """Return canonical notation and current analysis for an inclusive range."""
    with session_scope() as session:
        rows = (
            session.query(ScoreMeasure, MeasureAnalysis)
            .outerjoin(MeasureAnalysis, MeasureAnalysis.measure_id == ScoreMeasure.id)
            .filter(
                ScoreMeasure.work_id == work_id,
                ScoreMeasure.measure_number >= measure_start,
                ScoreMeasure.measure_number <= measure_end,
            )
            .order_by(
                ScoreMeasure.measure_index,
                MeasureAnalysis.created_at.desc(),
                # PostgreSQL NOW() is transaction-scoped, so versions written
                # in one transaction share created_at. The sequence-backed ID
                # is a deterministic insertion-order tie-breaker.
                MeasureAnalysis.id.desc(),
            )
            .all()
        )

        # A measure may gain a newer analysis version later. Keep the newest
        # inserted one (created_at, then ID) while retaining one score record.
        evidence_by_measure: dict[int, dict] = {}
        for measure, measure_analysis in rows:
            if measure.id not in evidence_by_measure:
                evidence_by_measure[measure.id] = {
                    "measure_index": measure.measure_index,
                    "measure_number": measure.measure_number,
                    "notation": measure.symbolic_data,
                    "analysis": measure_analysis.analysis_data if measure_analysis else None,
                }
        return list(evidence_by_measure.values())


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


def clear_work_symbolic_layers(work_id: int) -> None:
    """Clear only reproducible symbolic source derivatives for a work."""
    with session_scope() as session:
        measure_ids = [m.id for m in session.query(ScoreMeasure.id).filter_by(work_id=work_id)]
        if measure_ids:
            session.query(MeasureAnalysis).filter(MeasureAnalysis.measure_id.in_(measure_ids)).delete(
                synchronize_session=False
            )
        session.query(ScoreMeasure).filter_by(work_id=work_id).delete()
        session.query(ScoreSource).filter_by(work_id=work_id).delete()
        session.commit()


def clear_work_segments_and_assets(work_id: int) -> None:
    """Clear all derived records for a work to allow a complete re-ingestion."""
    clear_work_symbolic_layers(work_id)
    with session_scope() as session:
        session.query(ScoreSegment).filter_by(work_id=work_id).delete()
        # Delete text sources (like wikipedia or imslp text chunks)
        session.query(TextSource).filter_by(work_id=work_id).delete()
        # Delete all assets (PDF will be re-added by the ingestion script)
        session.query(ScoreAsset).filter_by(work_id=work_id).delete()
        session.commit()
