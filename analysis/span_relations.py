"""
analysis/span_relations.py
Deterministic comparison of two spans (measure_index ranges) to score how
well they support a `repeats` or `varies` span_relations candidate.

v1 scope: primary part only (part_index=0), equal measure count only. A
span pair with unequal measure counts is not attempted (confidence 0.0) —
alignment across unequal-length spans is deferred to v2.
"""

from __future__ import annotations
from typing import Any

from db.store import extract_ordered_pitch_classes, extract_ordered_rhythm, get_measure_total_durations

RHYTHM_MATCH_TOLERANCE = 0.05  # quarter-length slack for measure-total duration comparisons


def _span_length(span: dict) -> int:
    return span["measure_end_index"] - span["measure_start_index"] + 1


def _sequence_match_ratio(a: list, b: list) -> float:
    """Fraction of positions that match, using the longer sequence's length
    as the denominator so extra/missing elements also count against it."""
    length = max(len(a), len(b))
    if length == 0:
        return 1.0
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / length


def _measure_duration_similarity(a: list[float], b: list[float], tolerance: float = RHYTHM_MATCH_TOLERANCE) -> float:
    length = max(len(a), len(b))
    if length == 0:
        return 1.0
    matches = sum(1 for x, y in zip(a, b) if abs(x - y) <= tolerance)
    return matches / length


def _interval_sequence(pitch_classes: list[int]) -> list[int]:
    return [(b - a) % 12 for a, b in zip(pitch_classes, pitch_classes[1:])]


def check_repeats(span_a: dict, span_b: dict) -> tuple[float, dict[str, Any]]:
    """Does span_b look like a literal repeat of span_a: near-identical
    ordered pitch-class sequence and rhythm sequence, on the primary part.

    span_a/span_b: dicts with work_id, measure_start_index, measure_end_index.
    """
    if _span_length(span_a) != _span_length(span_b):
        return 0.0, {
            "reason": "unequal_measure_count",
            "span_a_measures": _span_length(span_a),
            "span_b_measures": _span_length(span_b),
        }

    pitches_a = extract_ordered_pitch_classes(span_a["work_id"], span_a["measure_start_index"], span_a["measure_end_index"])
    pitches_b = extract_ordered_pitch_classes(span_b["work_id"], span_b["measure_start_index"], span_b["measure_end_index"])
    rhythm_a = extract_ordered_rhythm(span_a["work_id"], span_a["measure_start_index"], span_a["measure_end_index"])
    rhythm_b = extract_ordered_rhythm(span_b["work_id"], span_b["measure_start_index"], span_b["measure_end_index"])

    pitch_ratio = _sequence_match_ratio(pitches_a, pitches_b)
    rhythm_ratio = _sequence_match_ratio(rhythm_a, rhythm_b)
    confidence = (pitch_ratio + rhythm_ratio) / 2

    evidence = {
        "comparison": "repeats",
        "part_index": 0,
        "pitch_classes_a": pitches_a,
        "pitch_classes_b": pitches_b,
        "pitch_match_ratio": pitch_ratio,
        "rhythm_a": rhythm_a,
        "rhythm_b": rhythm_b,
        "rhythm_match_ratio": rhythm_ratio,
    }
    return confidence, evidence


def check_varies(span_a: dict, span_b: dict) -> tuple[float, dict[str, Any]]:
    """Does span_b look like a varied restatement of span_a: similar melodic
    contour (interval sequence mod 12) and similar rhythmic weight per
    measure, without requiring pitch identity or event-for-event rhythm.

    span_a/span_b: dicts with work_id, measure_start_index, measure_end_index.
    """
    if _span_length(span_a) != _span_length(span_b):
        return 0.0, {
            "reason": "unequal_measure_count",
            "span_a_measures": _span_length(span_a),
            "span_b_measures": _span_length(span_b),
        }

    pitches_a = extract_ordered_pitch_classes(span_a["work_id"], span_a["measure_start_index"], span_a["measure_end_index"])
    pitches_b = extract_ordered_pitch_classes(span_b["work_id"], span_b["measure_start_index"], span_b["measure_end_index"])
    intervals_a = _interval_sequence(pitches_a)
    intervals_b = _interval_sequence(pitches_b)
    interval_ratio = _sequence_match_ratio(intervals_a, intervals_b)

    durations_a = get_measure_total_durations(span_a["work_id"], span_a["measure_start_index"], span_a["measure_end_index"])
    durations_b = get_measure_total_durations(span_b["work_id"], span_b["measure_start_index"], span_b["measure_end_index"])
    rhythm_ratio = _measure_duration_similarity(durations_a, durations_b)

    confidence = (interval_ratio + rhythm_ratio) / 2

    what_varied = []
    if pitches_a != pitches_b:
        what_varied.append("pitch")
    if durations_a != durations_b:
        what_varied.append("rhythm")

    evidence = {
        "comparison": "varies",
        "part_index": 0,
        "interval_sequence_a": intervals_a,
        "interval_sequence_b": intervals_b,
        "interval_match_ratio": interval_ratio,
        "measure_durations_a": durations_a,
        "measure_durations_b": durations_b,
        "rhythm_similarity_ratio": rhythm_ratio,
        "what_varied": what_varied,
    }
    return confidence, evidence
