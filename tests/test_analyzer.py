"""
tests/test_analyzer.py
Smoke tests for the music21 analysis module.
Requires music21 installed and a sample MusicXML file.
"""
import pytest
from pathlib import Path
from analysis.analyzer import analyze_score, build_symbolic_layers, detect_texture


SAMPLE_XML = Path(__file__).parent / "fixtures" / "sample.musicxml"


@pytest.mark.skipif(not SAMPLE_XML.exists(), reason="No fixture MusicXML")
def test_analyze_produces_chunks():
    chunks, global_key = analyze_score(str(SAMPLE_XML), window=4)
    assert len(chunks) > 0
    assert global_key is not None
    for c in chunks:
        assert c.measure_start <= c.measure_end
        assert c.summary_text is not None
        assert len(c.summary_text) > 10


def test_detect_texture_with_music21():
    from music21 import stream, note
    s = stream.Stream()
    for pitch in ["C4", "D4", "E4", "F4", "G4", "A4"]:
        s.append(note.Note(pitch, quarterLength=1))
    tag = detect_texture(s)
    assert tag in {"stepwise_melody", "cantabile", "chordal", "octaves_or_leaps"}


def test_build_symbolic_layers_preserves_events_and_measure_analysis():
    score_path = Path("data/sonata32-2.krn")
    if not score_path.exists():
        pytest.skip("Op. 111 source is not available")

    measures, analyses, global_key = build_symbolic_layers(str(score_path))

    assert measures
    assert len(measures) == len(analyses)
    assert global_key != "unknown"
    first = measures[0]
    assert first.symbolic_data["encoding_version"] == "1.0"
    assert first.symbolic_data["parts"]
    assert any(part["events"] for part in first.symbolic_data["parts"])
    assert "pitch_classes" in analyses[0].analysis_data
    assert analyses[0].analysis_data["analysis_version"] == "1.0"
