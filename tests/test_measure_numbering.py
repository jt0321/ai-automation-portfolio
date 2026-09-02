"""
tests/test_measure_numbering.py
The printed-vs-internal measure numbering contract.

Printed (engraved) numbering is what a performer reads, what the user types,
and what the LLM cites: bar 1 is the first *complete* measure, and an
anacrusis is not counted. Verovio agrees -- it emits an unnumbered pickup --
but its `measureRange` selection counts ordinal positions from 1, in which the
pickup *is* position 1. These tests pin that one-measure offset, which is
silent when wrong: the viewer simply shows the neighbouring bars.
"""
import pytest
from pathlib import Path

from pipeline.mei_converter import measure_ordinals


ANACRUSIS_MEI = Path("data/mei/sonata01-1.mei")   # Op. 2 No. 1/i, upbeat
PLAIN_MEI = Path("data/mei/sonata08-2.mei")       # Op. 13/ii, no upbeat


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path} has not been generated")
    return path.read_text(encoding="utf-8")


def test_anacrusis_shifts_printed_numbers_by_one():
    ordinals = measure_ordinals(_read(ANACRUSIS_MEI))
    assert ordinals[0] == 1   # the unnumbered pickup is ordinal 1
    assert ordinals[1] == 2   # printed bar 1 is the *second* physical measure
    assert ordinals[5] == 6


def test_score_without_anacrusis_is_unshifted():
    ordinals = measure_ordinals(_read(PLAIN_MEI))
    assert 0 not in ordinals
    assert ordinals[1] == 1
    assert ordinals[5] == 5


def test_unnumbered_measure_is_addressable_as_zero():
    """music21 and score_measures.measure_number both denote an unnumbered
    measure as 0; the renderer must use the same denotation."""
    assert measure_ordinals('<measure><x/></measure><measure n="1"></measure>') == {0: 1, 1: 2}


def test_ordinals_ignore_unparsable_numbers_without_shifting_the_rest():
    mei = '<measure n="1"></measure><measure n="1a"></measure><measure n="2"></measure>'
    assert measure_ordinals(mei) == {1: 1, 2: 3}


def test_first_occurrence_wins_for_repeated_numbers():
    """A repeated bar number (common around repeats and editorial numbering)
    must not silently retarget an earlier citation to a later measure."""
    mei = '<measure n="1"></measure><measure n="2"></measure><measure n="2"></measure>'
    assert measure_ordinals(mei)[2] == 2


@pytest.mark.parametrize("printed,expected_source_line", [(1, 25), (5, 53), (8, 80)])
def test_rendered_excerpt_matches_the_printed_bar_in_the_source(printed, expected_source_line):
    """Verovio tags each SVG measure with the MEI xml:id, which encodes the
    Humdrum line it came from -- so this checks the rendered bar really is the
    one the source numbers, not its neighbour."""
    import re
    from pipeline.mei_converter import mei_to_svg
    if not ANACRUSIS_MEI.exists():
        pytest.skip("MEI has not been generated")
    svg = mei_to_svg(str(ANACRUSIS_MEI), printed, printed)
    assert f"measure-L{expected_source_line}" in svg
    assert len(re.findall(r'id="measure-L\d+"', svg)) == 1


def test_excerpt_renders_only_the_requested_measures():
    """Regression: the old `select` *option* is unsupported in Verovio 6, so a
    request for four bars silently rendered the entire movement."""
    import re
    from pipeline.mei_converter import mei_to_svg
    if not ANACRUSIS_MEI.exists():
        pytest.skip("MEI has not been generated")
    svg = mei_to_svg(str(ANACRUSIS_MEI), 5, 8)
    assert len(re.findall(r'id="measure-L\d+"', svg)) == 4
