from pipeline import chat


def test_context_includes_bounded_symbolic_evidence(monkeypatch):
    def measure_evidence(work_id, start, end):
        return [
            {
                "measure_index": index,
                "measure_number": index + 1,
                "notation": {"parts": [{"events": [{"kind": "note", "pitch": {"name": "C4"}}]}]},
                "analysis": {"pitch_classes": [0], "local_key_candidate": "C major"},
            }
            for index in range(start - 1, end)
        ]

    monkeypatch.setattr(chat, "get_measure_evidence", measure_evidence)
    context = chat._build_context({
        "segments": [{
            "work_id": 1, "composer": "Beethoven", "title": "Example",
            "opus": "Op. 111", "measure_start": 1, "measure_end": 4,
            "summary_text": "A retrieval summary.",
        }],
        "text_sources": [],
    })

    assert "symbolic_evidence=" in context
    assert '"measure_number":1' in context
    assert '"local_key_candidate":"C major"' in context
