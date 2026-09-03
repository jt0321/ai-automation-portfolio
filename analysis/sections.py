"""
analysis/sections.py
Notated section structure, read from Humdrum expansion records.

A Humdrum score labels its sections (`*>A`, `*>B1`) and states the order they
are played in (`*>[A,A,B]` — play A twice, then B). This is engraved
structure, not inference: it records exactly which stretch of music the
composer marked to be repeated and which endings belong to which pass.

That matters for form. A repeat of a large stretch of music is the clearest
single indicator of sonata form, and *which* stretch repeats says what kind of
movement it is: both halves repeated is the Classical norm, an unrepeated
opening section followed by a repeated one is Beethoven inverting it (Op. 57's
finale repeats its development and recapitulation, not its exposition), and a
short unrepeated section before a repeated one is usually a slow introduction
standing outside the form (Op. 111's Maestoso).

`music21` discards these records entirely, so they reach us only from the raw
`.krn` — which `score_sources.raw_content` preserves, making this pass
reproducible from the database alone.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any

SECTION_ANALYSIS_VERSION = "1.0"

_EXPANSION = re.compile(r"^\*>\[([^\]]*)\]")
_NOREP_EXPANSION = re.compile(r"^\*>norep\[([^\]]*)\]")
_SECTION_LABEL = re.compile(r"^\*>([A-Za-z][A-Za-z0-9]*)$")
_BARLINE = re.compile(r"^=(\d+)")


@dataclass
class NotatedSection:
    """One labelled section of a score, with how often it is played."""
    label: str
    measure_start: int
    measure_end: int
    play_count: int          # times the expansion list plays it
    is_alternate_ending: bool  # a "B1"/"B2" style ending under section B
    parent_label: str | None   # "B" for "B1"; None for a top-level section


def _first_column(line: str) -> str:
    return line.split("\t")[0].rstrip("\n")


def parse_notated_sections(source_text: str) -> tuple[list[str], list[NotatedSection]]:
    """Expansion order and labelled sections with their measure ranges.

    Returns ([] , []) for a score that labels no sections — 43 of this corpus's
    103 movements carry no expansion record at all, which is itself meaningful:
    a movement with nothing marked to repeat is not built on a repeat scheme.
    """
    expansion: list[str] = []
    order: list[str] = []
    bars: dict[str, list[int]] = {}
    current: str | None = None

    for line in source_text.splitlines():
        token = _first_column(line)
        if _NOREP_EXPANSION.match(token):
            continue  # the no-repeat performance option, not the written order
        match = _EXPANSION.match(token)
        if match and not expansion:
            expansion = [part.strip() for part in match.group(1).split(",") if part.strip()]
            continue
        match = _SECTION_LABEL.match(token)
        if match:
            current = match.group(1)
            if current not in order:
                order.append(current)
            continue
        match = _BARLINE.match(token)
        if match and current is not None:
            bars.setdefault(current, []).append(int(match.group(1)))

    labels = set(order)
    sections: list[NotatedSection] = []
    for label in order:
        if label not in bars:
            continue  # labelled but carrying no barline of its own
        # "B1" is an alternate ending of "B" when a section by that name exists;
        # a bare "A2" with no "A" is just a section whose name ends in a digit.
        stem = label.rstrip("0123456789")
        is_ending = bool(stem) and stem != label and stem in labels
        sections.append(NotatedSection(
            label=label,
            measure_start=min(bars[label]),
            measure_end=max(bars[label]),
            play_count=expansion.count(label),
            is_alternate_ending=is_ending,
            parent_label=stem if is_ending else None,
        ))
    return expansion, sections


def repeated_sections(sections: list[NotatedSection]) -> list[NotatedSection]:
    """Sections the expansion plays more than once, ignoring endings."""
    return [s for s in sections if s.play_count > 1 and not s.is_alternate_ending]


def section_evidence(
    expansion: list[str], sections: list[NotatedSection], section: NotatedSection
) -> dict[str, Any]:
    """Evidence for one section, recorded without interpreting it as a form part.

    `repeat_scheme` summarises the movement's shape (e.g. "A,A,B,B") and
    `repeated_span_measures` how much music the repeat covers, since a repeat
    spanning most of a movement is the sonata-form signal while a repeat of a
    few bars is a local one. Naming the section an exposition would be a claim
    this evidence alone cannot support.
    """
    body = [s for s in sections if not s.is_alternate_ending]
    return {
        "analysis_version": SECTION_ANALYSIS_VERSION,
        "label": section.label,
        "play_count": section.play_count,
        "is_alternate_ending": section.is_alternate_ending,
        "parent_label": section.parent_label,
        "repeat_scheme": ",".join(expansion),
        "section_order": [s.label for s in body],
        "repeated_labels": [s.label for s in repeated_sections(sections)],
        "repeated_span_measures": section.measure_end - section.measure_start + 1,
        "is_repeated": section.play_count > 1 and not section.is_alternate_ending,
    }
