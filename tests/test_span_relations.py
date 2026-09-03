"""
tests/test_span_relations.py
Symbolic span comparison and the relations pass.

The scoring functions are exercised against synthetic WorkFeatures so they run
without a database; the pass's musical behaviour is checked against a movement
with a published recapitulation point, and skips when no database is present.
"""
import os
import pytest

from analysis.span_relations import (
    WorkFeatures, _merge_by_offset, _select_distinct_matches, _worth_comparing,
    check_repeats, check_varies, search_for_matches,
)


def _measure(index, pitch_classes, rhythm=None, key="C major"):
    rhythm = rhythm if rhythm is not None else [1.0] * len(pitch_classes)
    return {
        "measure_index": index, "measure_number": index + 1,
        "pitch_classes": list(pitch_classes), "rhythm": list(rhythm),
        "total_duration": sum(rhythm), "local_key": key,
    }


def _theme_work():
    """A four-measure theme, restated literally, then transposed up a tone."""
    theme = [[0, 4, 7, 4], [5, 4, 2, 0], [7, 11, 2, 11], [0, 4, 0, 7]]
    filler = [[1, 6, 1, 6], [3, 8, 3, 8], [10, 5, 10, 5], [6, 1, 6, 1]]
    transposed = [[(pc + 2) % 12 for pc in bar] for bar in theme]
    measures = []
    for i, bar in enumerate(theme + filler + theme + transposed):
        key = "D major" if i >= 12 else "C major"
        measures.append(_measure(i, bar, key=key))
    return WorkFeatures(1, measures)


def _span(start, end):
    return {"work_id": 1, "measure_start_index": start, "measure_end_index": end}


# --- scoring ---------------------------------------------------------------

def test_literal_restatement_scores_as_a_repeat():
    features = _theme_work()
    confidence, evidence = check_repeats(_span(0, 3), _span(8, 11), features)
    assert confidence == 1.0
    assert evidence["pitch_match_ratio"] == 1.0


def test_transposition_is_not_a_repeat_but_is_a_variation():
    """Transposed material shares contour, not pitch: `varies` recognises it
    and must win over `repeats`, which is what decides the relation type.

    Note `repeats` averages a pitch ratio with a rhythm ratio, so a
    transposition keeping its rhythm caps at 0.5 on that scorer -- carried
    entirely by rhythm, and well under MIN_RELATION_CONFIDENCE."""
    features = _theme_work()
    repeats, _ = check_repeats(_span(0, 3), _span(12, 15), features)
    varies, evidence = check_varies(_span(0, 3), _span(12, 15), features)
    assert repeats <= 0.5
    assert varies > 0.9
    assert varies > repeats
    assert "pitch" in evidence["what_varied"]


def test_spans_of_unequal_length_are_not_compared():
    features = _theme_work()
    confidence, evidence = check_repeats(_span(0, 3), _span(8, 10), features)
    assert confidence == 0.0
    assert evidence["reason"] == "unequal_measure_count"


def test_cached_features_agree_with_no_features_signature():
    """The features argument is an optimisation, not a behaviour change."""
    features = _theme_work()
    assert check_repeats(_span(0, 3), _span(8, 11), features)[0] == 1.0


# --- prefilter -------------------------------------------------------------

def test_prefilter_rejects_windows_that_cannot_reach_the_threshold():
    features = WorkFeatures(1, [_measure(0, [0]*20), _measure(1, [0]*2)])
    # 2 events against 20: best possible score is (0.1 + 1)/2 = 0.55
    assert not _worth_comparing(_span(0, 0), _span(1, 1), features, 0.75)
    assert _worth_comparing(_span(0, 0), _span(1, 1), features, 0.5)


def test_prefilter_keeps_equal_length_windows():
    features = _theme_work()
    assert _worth_comparing(_span(0, 3), _span(8, 11), features, 0.9)


# --- match selection and merging ------------------------------------------

def _match(start, end, confidence):
    return {"measure_start_index": start, "measure_end_index": end,
            "repeats_confidence": confidence, "varies_confidence": 0.0,
            "repeats_evidence": {}, "varies_evidence": {}}


def test_overlapping_matches_collapse_to_the_strongest():
    """A sliding window scores one real return at several adjacent offsets."""
    matches = [_match(10, 13, 0.95), _match(11, 14, 0.90), _match(40, 43, 0.85)]
    chosen = _select_distinct_matches(matches, limit=4)
    assert [(m["measure_start_index"]) for m in chosen] == [10, 40]


def test_match_selection_respects_its_limit():
    matches = [_match(i * 10, i * 10 + 3, 0.9 - i / 100) for i in range(8)]
    assert len(_select_distinct_matches(matches, limit=3)) == 3


def _relation(s0, s1, t0, t1, confidence=0.8, same_key=True):
    return {"relation_type": "repeats", "confidence": confidence,
            "source_start_index": s0, "source_end_index": s1,
            "source_start": s0 + 1, "source_end": s1 + 1,
            "target_start_index": t0, "target_end_index": t1,
            "target_start": t0 + 1, "target_end": t1 + 1,
            "evidence": {"returns_in_same_key": same_key}}


def test_contiguous_windows_at_one_offset_merge_into_one_relation():
    merged = _merge_by_offset([
        _relation(0, 3, 100, 103), _relation(2, 5, 102, 105), _relation(4, 7, 104, 107),
    ])
    assert len(merged) == 1
    assert (merged[0]["source_start_index"], merged[0]["source_end_index"]) == (0, 7)
    assert (merged[0]["target_start_index"], merged[0]["target_end_index"]) == (100, 107)
    assert merged[0]["evidence"]["merged_windows"] == 3


def test_separated_ranges_at_one_offset_stay_separate():
    """The gap between them was never compared, so joining them would claim a
    correspondence that was not verified."""
    merged = _merge_by_offset([_relation(0, 3, 100, 103), _relation(40, 43, 140, 143)])
    assert len(merged) == 2


def test_different_offsets_never_merge():
    merged = _merge_by_offset([_relation(0, 3, 100, 103), _relation(2, 5, 60, 63)])
    assert len(merged) == 2


def test_merged_confidence_is_the_weakest_part():
    merged = _merge_by_offset([_relation(0, 3, 10, 13, 0.95), _relation(2, 5, 12, 15, 0.78)])
    assert merged[0]["confidence"] == 0.78


def test_merged_key_corroboration_requires_every_part():
    merged = _merge_by_offset([
        _relation(0, 3, 10, 13, same_key=True), _relation(2, 5, 12, 15, same_key=False),
    ])
    assert merged[0]["evidence"]["returns_in_same_key"] is False


# --- search ----------------------------------------------------------------

def test_search_finds_the_restatement_and_skips_the_reference_itself():
    features = _theme_work()
    matches = search_for_matches(_span(0, 3), min_confidence=0.9, features=features)
    starts = [m["measure_start_index"] for m in matches]
    assert 8 in starts
    assert not any(m["measure_start_index"] <= 3 for m in matches)


# --- against a real movement ----------------------------------------------

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="no DATABASE_URL configured"
)


@requires_db
def test_op2no1_primary_theme_is_found_returning_in_the_tonic():
    """Op. 2 No. 1/i recapitulates its primary theme at m. 101. The pass must
    relate the opening statement to a return there, in the same key."""
    from analysis.span_relations import build_span_relations
    from db.store import list_works
    work = next((w for w in list_works()
                 if "No. 1 in F minor" in w["title"] and w.get("movement_number") == 1), None)
    if work is None:
        pytest.skip("Op. 2 No. 1/i is not ingested")
    relations = build_span_relations(work["id"])
    returns = [
        r for r in relations
        if r["source_start"] <= 5 and 95 <= r["target_start"] <= 110
        and r["evidence"]["returns_in_same_key"]
    ]
    assert returns, "no tonic return of the opening material near the recapitulation"


# --- transposition ---------------------------------------------------------

def test_transposition_interval_reports_the_offset_and_its_consistency():
    from analysis.span_relations import transposition_interval
    theme = [0, 4, 7, 11]
    up_a_fourth = [(pc + 5) % 12 for pc in theme]
    assert transposition_interval(theme, up_a_fourth) == (5, 1.0)
    assert transposition_interval(theme, theme) == (0, 1.0)
    assert transposition_interval([], [0]) == (None, 0.0)


def test_transposition_consistency_falls_when_no_single_interval_explains_it():
    from analysis.span_relations import transposition_interval
    interval, consistency = transposition_interval([0, 1, 2, 3], [5, 9, 1, 6])
    assert consistency < 0.5  # no offset accounts for most positions


def test_merged_relation_drops_a_transposition_its_parts_disagree_on():
    merged = _merge_by_offset([
        {**_relation(0, 3, 10, 13), "evidence": {"returns_in_same_key": True,
         "transposed_semitones": 5, "transposition_consistency": 0.9}},
        {**_relation(2, 5, 12, 15), "evidence": {"returns_in_same_key": True,
         "transposed_semitones": 0, "transposition_consistency": 0.8}},
    ])
    assert merged[0]["evidence"]["transposed_semitones"] is None


@requires_db
def test_moonlight_finale_recapitulates_its_theme_at_pitch_and_its_second_group_up_a_fourth():
    """Op. 27 No. 2/iii. The textbook shape of a minor-key sonata recapitulation:
    the main theme returns in the tonic at pitch, while the second group -- in
    the minor dominant (g# minor) in the exposition -- is brought home to c#
    minor, which is a transposition up a perfect fourth."""
    from analysis.span_relations import build_span_relations
    from db.store import list_works
    work = next((w for w in list_works()
                 if "No. 14" in w["title"] and w.get("movement_number") == 3), None)
    if work is None:
        pytest.skip("Op. 27 No. 2/iii is not ingested")
    relations = build_span_relations(work["id"])

    theme = [r for r in relations if r["source_start"] <= 2 and r["target_start"] > 90]
    assert theme, "the opening theme's return was not found"
    assert theme[0]["evidence"]["transposed_semitones"] == 0
    assert theme[0]["evidence"]["transposition_consistency"] > 0.8
    assert theme[0]["evidence"]["returns_in_same_key"]

    # Second-group material, returned a fourth higher, at high consistency.
    fourths = [r for r in relations
               if r["evidence"]["transposed_semitones"] == 5
               and r["evidence"]["transposition_consistency"] > 0.9
               and r["source_start"] > 20]
    assert fourths, "no exact fourth-transposed return of second-group material"
