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

from db.store import (
    extract_ordered_pitch_classes, extract_ordered_rhythm, get_measure_total_durations,
    get_max_measure_index, get_global_key, get_tempo_markings, get_theme_repeat_open_index,
)

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


def search_for_matches(
    reference_span: dict,
    search_start_index: int | None = None,
    search_end_index: int | None = None,
    min_confidence: float = 0.5,
) -> list[dict]:
    """Slide a window the same length as reference_span across
    [search_start_index, search_end_index] of the same work (default: the
    whole work) and score every non-overlapping position with check_repeats
    and check_varies. This finds thematic returns (e.g. a transposed
    recapitulation) without needing pre-identified section boundaries or
    repeat signs — the reference span is the only thing that must be known
    up front.

    Returns candidates sorted by best(repeats, varies) confidence
    descending, each entry: {measure_start_index, measure_end_index,
    repeats_confidence, repeats_evidence, varies_confidence, varies_evidence}.
    """
    work_id = reference_span["work_id"]
    length = _span_length(reference_span)

    if search_start_index is None:
        search_start_index = 0
    if search_end_index is None:
        max_index = get_max_measure_index(work_id)
        if max_index is None:
            return []
        search_end_index = max_index

    ref_start = reference_span["measure_start_index"]
    ref_end = reference_span["measure_end_index"]

    results = []
    for start in range(search_start_index, search_end_index - length + 2):
        end = start + length - 1
        if start <= ref_end and end >= ref_start:
            continue  # skip windows overlapping the reference span itself

        candidate = {"work_id": work_id, "measure_start_index": start, "measure_end_index": end}
        repeats_confidence, repeats_evidence = check_repeats(reference_span, candidate)
        varies_confidence, varies_evidence = check_varies(reference_span, candidate)

        if max(repeats_confidence, varies_confidence) >= min_confidence:
            results.append({
                "measure_start_index": start,
                "measure_end_index": end,
                "repeats_confidence": repeats_confidence,
                "repeats_evidence": repeats_evidence,
                "varies_confidence": varies_confidence,
                "varies_evidence": varies_evidence,
            })

    results.sort(key=lambda r: max(r["repeats_confidence"], r["varies_confidence"]), reverse=True)
    return results


def detect_intro_end_index(work_id: int) -> int:
    """Measure_index where the movement's main theme begins, excluding a
    slow introduction, if one exists.

    Primary signal: when an introduction exists, the notated theme
    conventionally begins exactly at the first repeat-open barline ("|:")
    in the source — precise, since it's a literal encoded marking.
    Fallback: a change in tempo marking (e.g. Maestoso -> Allegro con
    brio) from the movement's opening tempo — less precise (can be a
    measure or two off) but catches introductions that aren't followed by
    a repeat sign. If neither signal fires, there is no detectable
    introduction and 0 is returned.
    """
    repeat_open_index = get_theme_repeat_open_index(work_id)
    if repeat_open_index is not None and repeat_open_index > 0:
        return repeat_open_index

    markings = get_tempo_markings(work_id)
    if not markings:
        return 0

    opening_tempo = next((tempo for _, tempo in markings if tempo is not None), None)
    if opening_tempo is None:
        return 0

    for measure_index, tempo in markings:
        if tempo is not None and tempo != opening_tempo:
            return measure_index
    return 0


def corroborate_key_match(work_id: int, reference_span: dict, candidate: dict) -> bool:
    """Does the candidate span open in the same global key as the
    reference span? A cheap independent check on a repeats/varies match:
    a genuine primary-theme recapitulation should return in the tonic, the
    same key the movement (and its reference span) opened in.

    Returns False (not corroborated) if either measure has no stored key,
    since an unknown key can't confirm a match.
    """
    reference_key = get_global_key(reference_span["work_id"], reference_span["measure_start_index"])
    candidate_key = get_global_key(work_id, candidate["measure_start_index"])
    if reference_key is None or candidate_key is None:
        return False
    return reference_key == candidate_key
