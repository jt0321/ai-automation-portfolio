"""
analysis/analyzer.py
Parses MusicXML via music21 and produces per-measure chunk dicts
suitable for embedding and storage in score_segments.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import music21
from music21 import converter, analysis, stream, roman

from analysis.harmony import HARMONY_ANALYSIS_VERSION, analyze_harmony


# Increment these whenever a derived representation changes.  Raw Humdrum is
# preserved independently, so every derived record can be regenerated.
SYMBOLIC_ENCODING_VERSION = "1.0"
MEASURE_ANALYSIS_VERSION = "2.0"
SPAN_ANALYSIS_VERSION = "1.0"


@dataclass
class MeasureChunk:
    measure_start:   int
    measure_end:     int
    part:            str = "grand_staff"
    local_key:       Optional[str] = None
    roman_numerals:  Optional[str] = None
    harmonic_rhythm: Optional[str] = None
    texture_tag:     Optional[str] = None
    formal_function: Optional[str] = None
    motif_tags:      list[str] = field(default_factory=list)
    summary_text:    Optional[str] = None
    musicxml_slice:  Optional[str] = None


@dataclass
class CanonicalMeasure:
    """JSON-safe, queryable notation facts for one measure across all parts."""
    measure_index: int
    measure_number: int
    symbolic_data: dict[str, Any]


@dataclass
class PerMeasureAnalysis:
    """Versioned facts deterministically derived from a CanonicalMeasure."""
    measure_index: int
    measure_number: int
    analysis_data: dict[str, Any]


@dataclass
class SpanCandidate:
    """A score-derived, variable-length candidate for later formal analysis."""
    measure_start: int
    measure_end: int
    measure_start_index: int
    measure_end_index: int
    evidence: dict[str, Any]
    features: dict[str, Any]


def build_span_candidates(
    measures: list[CanonicalMeasure], analyses: list[PerMeasureAnalysis],
) -> list[SpanCandidate]:
    """Segment a movement at explicit or score-derived structural changes.

    These are intentionally *candidates*, not claims that a span is a theme,
    variation, or phrase.  The evidence is retained for later validation.
    """
    if not measures:
        return []
    analyses_by_index = {item.measure_index: item.analysis_data for item in analyses}
    boundaries: dict[int, list[str]] = {0: ["movement_start"]}

    for index in range(1, len(measures)):
        previous = measures[index - 1]
        current = measures[index]
        previous_analysis = analyses_by_index.get(previous.measure_index, {})
        current_analysis = analyses_by_index.get(current.measure_index, {})
        signals = []
        if current_analysis.get("time_signature") != previous_analysis.get("time_signature"):
            signals.append("meter_change")
        if current_analysis.get("directions"):
            signals.append("notated_direction")
        previous_barlines = [
            part.get("right_barline") for part in previous.symbolic_data.get("parts", [])
        ]
        if any(barline in {"final", "repeat"} for barline in previous_barlines):
            signals.append("structural_barline")
        if signals:
            boundaries[index] = signals
    boundaries[len(measures)] = ["movement_end"]

    boundary_indexes = sorted(boundaries)
    candidates: list[SpanCandidate] = []
    for start_index, end_index in zip(boundary_indexes, boundary_indexes[1:]):
        span_measures = measures[start_index:end_index]
        if not span_measures:
            continue
        span_analyses = [analyses_by_index.get(item.measure_index, {}) for item in span_measures]
        candidates.append(SpanCandidate(
            measure_start=span_measures[0].measure_number,
            measure_end=span_measures[-1].measure_number,
            measure_start_index=span_measures[0].measure_index,
            measure_end_index=span_measures[-1].measure_index,
            evidence={
                "analysis_version": SPAN_ANALYSIS_VERSION,
                "start_boundary": boundaries[start_index],
                "end_boundary": boundaries[end_index],
            },
            features={
                "measure_count": len(span_measures),
                "time_signatures": sorted({a.get("time_signature") for a in span_analyses if a.get("time_signature")}),
                "local_key_candidates": sorted({a.get("local_key") for a in span_analyses if a.get("local_key")}),
                "texture_tags": sorted({a.get("texture_tag") for a in span_analyses if a.get("texture_tag")}),
                "pitch_classes": sorted({pc for a in span_analyses for pc in a.get("pitch_classes", [])}),
                "roman_numerals": [
                    chord["figure"]
                    for a in span_analyses for chord in a.get("chords", [])
                    if chord.get("figure")
                ],
            },
        ))
    return candidates


def _duration_data(element) -> dict[str, Any]:
    duration = element.duration
    return {
        "quarter_length": str(duration.quarterLength),
        "type": duration.type,
        "dots": duration.dots,
    }


def _pitch_data(pitch) -> dict[str, Any]:
    return {
        "name": pitch.nameWithOctave,
        "midi": pitch.midi,
        "pitch_class": pitch.pitchClass,
    }


def _event_data(element, measure: stream.Measure) -> dict[str, Any]:
    """Convert a music21 note, chord, or rest into serialisable score facts."""
    voice = element.getContextByClass(stream.Voice)
    event: dict[str, Any] = {
        "offset": str(element.getOffsetInHierarchy(measure)),
        "duration": _duration_data(element),
        "voice": str(voice.id) if voice is not None and voice.id is not None else None,
        "tie": element.tie.type if getattr(element, "tie", None) else None,
        "articulations": [a.__class__.__name__ for a in element.articulations],
        "expressions": [str(e) for e in element.expressions],
    }
    if element.isNote:
        event.update({"kind": "note", "pitch": _pitch_data(element.pitch)})
    elif element.isChord:
        event.update({"kind": "chord", "pitches": [_pitch_data(p) for p in element.pitches]})
    else:
        event.update({"kind": "rest"})
    return event


def _barline_type(barline) -> str | None:
    return getattr(barline, "type", None) if barline is not None else None


def _measure_encoding(measure: stream.Measure, part_index: int) -> dict[str, Any]:
    """Extract notation facts belonging to a single part/measure."""
    part = measure.getContextByClass(stream.Part)
    time_signature = measure.timeSignature
    key_signature = measure.keySignature
    directions = []
    for element in measure.recurse():
        name = element.__class__.__name__
        if name in {"MetronomeMark", "TextExpression", "Dynamic"}:
            directions.append({"type": name, "value": str(element)})
    return {
        "part_index": part_index,
        "part_id": str(part.id) if part is not None and part.id is not None else None,
        "events": [_event_data(e, measure) for e in measure.recurse().notesAndRests],
        "time_signature": time_signature.ratioString if time_signature else None,
        "key_signature_sharps": key_signature.sharps if key_signature else None,
        "directions": directions,
        "left_barline": _barline_type(measure.leftBarline),
        "right_barline": _barline_type(measure.rightBarline),
    }


def build_symbolic_layers(score_path: str) -> tuple[list[CanonicalMeasure], list[PerMeasureAnalysis], str]:
    """Build canonical notation and a first, measure-level analysis layer.

    No embedding or LLM is used: all results are reproducible from the score.
    The raw .krn remains the authoritative source for details not exposed by
    this convenient JSON representation.
    """
    score = converter.parse(score_path)
    parts = list(score.parts)
    if not parts:
        return [], [], "unknown"

    key_analyzer = analysis.discrete.KrumhanslSchmuckler()
    global_key_obj = key_analyzer.getSolution(score)
    global_key = str(global_key_obj) if global_key_obj else "unknown"
    part_measures = [list(part.getElementsByClass(stream.Measure)) for part in parts]
    primary_measures = part_measures[0]
    canonical_measures: list[CanonicalMeasure] = []
    measure_analyses: list[PerMeasureAnalysis] = []

    for measure_index, primary_measure in enumerate(primary_measures):
        measure_number = primary_measure.number
        notated_measures = [
            measures[measure_index]
            for measures in part_measures
            if measure_index < len(measures)
        ]
        encoded_parts = [
            _measure_encoding(measures[measure_index], part_index)
            for part_index, measures in enumerate(part_measures)
            if measure_index < len(measures)
        ]
        canonical_measures.append(CanonicalMeasure(
            measure_index,
            measure_number,
            {
                "encoding_version": SYMBOLIC_ENCODING_VERSION,
                "measure_number": measure_number,
                "parts": encoded_parts,
            },
        ))

        notes_and_rests = [
            element for measure in notated_measures for element in measure.recurse().notesAndRests
        ]
        notes = [e for e in notes_and_rests if e.isNote]
        chords = [e for e in notes_and_rests if e.isChord]
        rests = [e for e in notes_and_rests if e.isRest]
        pitches = [n.pitch for n in notes]
        for chord in chords:
            pitches.extend(chord.pitches)
        voice_ids = {
            event["voice"]
            for part in encoded_parts for event in part["events"]
            if event["voice"] is not None
        }
        measure_analyses.append(PerMeasureAnalysis(
            measure_index,
            measure_number,
            {
                "analysis_version": MEASURE_ANALYSIS_VERSION,
                "global_key": global_key,
                "time_signature": encoded_parts[0]["time_signature"] if encoded_parts else None,
                "directions": [d for part in encoded_parts for d in part["directions"]],
                "pitch_classes": sorted({pitch.pitchClass for pitch in pitches}),
                "part_count": len(encoded_parts),
                "note_count": len(notes),
                "chord_count": len(chords),
                "rest_count": len(rests),
                "voice_count": len(voice_ids) or len([part for part in encoded_parts if part["events"]]),
                "rhythm_quarter_lengths": [str(e.duration.quarterLength) for e in notes_and_rests],
                "texture_tag": detect_texture(primary_measure),
                "texture_scope": "primary_part_measure",
            },
        ))

    # Harmony is a second pass over the finished canonical layer rather than
    # per-measure work inside the loop: key estimation needs a window of
    # surrounding measures, and chord fitting needs the meter carried forward.
    trajectory, chord_spans = analyze_harmony(canonical_measures, measure_analyses)
    keys_by_index = {estimate.measure_index: estimate for estimate in trajectory}
    chords_by_index: dict[int, list[dict[str, Any]]] = {}
    for span in chord_spans:
        chords_by_index.setdefault(span.measure_index, []).append({
            "figure": span.figure,
            "root_pitch_class": span.root,
            "quality": span.quality,
            "bass_pitch_class": span.bass,
            "beat_start": span.beat_start,
            "beat_end": span.beat_end,
            "confidence": round(span.confidence, 3),
            "non_chord_tones": span.non_chord_tones,
        })

    for item in measure_analyses:
        estimate = keys_by_index.get(item.measure_index)
        item.analysis_data.update({
            "harmony_version": HARMONY_ANALYSIS_VERSION,
            "local_key": estimate.key if estimate else None,
            "local_key_scope": "windowed_viterbi",
            "local_key_correlation": round(estimate.correlation, 3) if estimate else None,
            "local_key_in_signature": estimate.in_key_signature if estimate else None,
            "chords": chords_by_index.get(item.measure_index, []),
        })

    # music21's whole-score Krumhansl estimate is subject to the same
    # dominant-bias as the per-measure one (it reads Op. 13/ii as E- major);
    # the smoothed, signature-anchored trajectory is the better answer.
    if trajectory:
        opening_key = trajectory[0].key
        for item in measure_analyses:
            item.analysis_data["global_key"] = opening_key
        global_key = opening_key

    return canonical_measures, measure_analyses, global_key


def detect_texture(measures: stream.Stream) -> str:
    """Heuristic texture detection based on note/chord density and intervals."""
    note_count = len(measures.flatten().notes)
    chord_count = len(measures.flatten().getElementsByClass("Chord"))
    if chord_count > note_count * 0.6:
        return "chordal"
    pitches = [n.pitch for n in measures.flatten().notes if hasattr(n, "pitch")]
    if len(pitches) > 1:
        intervals = [abs(pitches[i+1].midi - pitches[i].midi) for i in range(len(pitches)-1)]
        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        if avg_interval <= 2:
            return "stepwise_melody"
        if avg_interval >= 10:
            return "octaves_or_leaps"
    return "cantabile"


def analyze_score(score_path: str, window: int = 4) -> tuple[list[MeasureChunk], str]:
    """
    Parse a score file (Humdrum, MusicXML, etc.) and return a list of MeasureChunk objects,
    windowed by `window` measures (analogous to paragraph-level chunking).
    """
    score = converter.parse(score_path)
    parts = score.parts

    # Key analysis over full score
    key_analyzer = analysis.discrete.KrumhanslSchmuckler()
    key_obj = key_analyzer.getSolution(score)
    global_key = str(key_obj) if key_obj else "unknown"

    # Chordify for Roman numeral analysis
    chordified = score.chordify()

    all_measures = list(score.parts[0].getElementsByClass("Measure"))
    total = len(all_measures)
    chunks: list[MeasureChunk] = []

    for start_idx in range(0, total, window):
        end_idx = min(start_idx + window - 1, total - 1)
        m_start = all_measures[start_idx].number
        m_end   = all_measures[end_idx].number

        # Slice of chordified score for this window
        window_chords = chordified.measures(m_start, m_end)

        # Local key via window analysis
        try:
            local_key_obj = key_analyzer.getSolution(window_chords)
            local_key = str(local_key_obj)
        except Exception:
            local_key = global_key

        # Roman numerals (up to 8 per window to keep summary concise)
        rn_labels = []
        for c in window_chords.flatten().getElementsByClass("Chord"):
            try:
                rn = roman.romanNumeralFromChord(c, music21.key.Key(local_key.split()[0]))
                rn_labels.append(rn.figure)
            except Exception:
                pass
        rn_str = " ".join(rn_labels[:8]) if rn_labels else None

        # Harmonic rhythm: rough count of chord changes per measure
        changes_per_measure = len(rn_labels) / max(window, 1)
        if changes_per_measure <= 1:
            harmonic_rhythm = "slow"
        elif changes_per_measure <= 3:
            harmonic_rhythm = "moderate"
        else:
            harmonic_rhythm = "fast"

        # Texture from soprano (right hand) part
        rh_window = parts[0].measures(m_start, m_end)
        texture = detect_texture(rh_window)

        # Build summary text (this is what gets embedded)
        summary = (
            f"Measures {m_start}–{m_end} of the score. "
            f"Local key: {local_key}. "
            f"Harmonic progression: {rn_str or 'undetermined'}. "
            f"Harmonic rhythm: {harmonic_rhythm}. "
            f"Texture: {texture}."
        )

        # Extract raw MusicXML slice
        try:
            xml_slice = rh_window.write("musicxml").read_text()
        except Exception:
            xml_slice = None

        chunks.append(MeasureChunk(
            measure_start   = m_start,
            measure_end     = m_end,
            part            = "grand_staff",
            local_key       = local_key,
            roman_numerals  = rn_str,
            harmonic_rhythm = harmonic_rhythm,
            texture_tag     = texture,
            summary_text    = summary,
            musicxml_slice  = xml_slice,
        ))

    return chunks, global_key
