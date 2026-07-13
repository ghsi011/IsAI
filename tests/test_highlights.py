"""Highlight resolution and overlap splitting."""

from __future__ import annotations

from itertools import pairwise

from isai.highlights import resolve_highlights, split_segments
from isai.textmatch import MatchTier
from tests.helpers import make_result

PARA = "The results were significant. The results were significant in every model."


def test_resolution_uses_local_offsets() -> None:
    result = make_result(
        indicators=[
            {
                "category": "repetitive_restatement",
                "evidence": "The results were significant",
                "occurrence_index": 2,
                "explanation": "Repeated sentence opening.",
            }
        ]
    )
    highlights = resolve_highlights(result, PARA)
    assert len(highlights) == 1
    h = highlights[0]
    assert h.tier is MatchTier.EXACT
    assert PARA[h.start : h.end] == "The results were significant"
    assert h.start == 30  # second occurrence, never the first


def test_unresolved_never_guesses() -> None:
    result = make_result(
        indicators=[{"category": "other", "evidence": "not present anywhere", "explanation": "x"}]
    )
    highlights = resolve_highlights(result, PARA)
    assert highlights[0].tier is MatchTier.UNRESOLVED
    assert highlights[0].start == -1


def test_all_quoted_fields_produce_highlights() -> None:
    result = make_result(
        indicators=[{"category": "other", "evidence": "significant", "explanation": "x"}],
        counter_indicators=[{"category": "other", "evidence": "every model", "explanation": "x"}],
        quality_issues=[
            {"category": "repetition", "target_text": "The results", "description": "x"}
        ],
        citation_observations=[
            {
                "observation": "No citation given.",
                "target_text": "in every model",
                "requires_source_check": True,
            }
        ],
        revision_suggestions=[
            {
                "target_text": "were significant",
                "issue": "vague",
                "recommended_change": "state the effect size",
                "reason": "specificity",
            }
        ],
    )
    highlights = resolve_highlights(result, PARA)
    kinds = {h.kind.value for h in highlights}
    assert kinds == {"indicator", "counter_indicator", "quality", "citation", "suggestion"}
    ids = [h.highlight_id for h in highlights]
    assert len(ids) == len(set(ids)), "highlight IDs must be unique"


def test_empty_quotes_are_skipped() -> None:
    result = make_result(
        revision_suggestions=[
            {
                "target_text": "",
                "issue": "structure",
                "recommended_change": "reorder",
                "reason": "flow",
            }
        ]
    )
    assert resolve_highlights(result, PARA) == []


def test_overlap_splitting_preserves_text() -> None:
    result = make_result(
        indicators=[
            {"category": "other", "evidence": "results were significant", "explanation": "x"}
        ],
        quality_issues=[
            {"category": "clarity", "target_text": "The results were", "description": "x"}
        ],
    )
    highlights = resolve_highlights(result, PARA)
    segments = split_segments(highlights, len(PARA))
    # Segments must tile the full text with no gaps or overlaps.
    assert segments[0].start == 0
    assert segments[-1].end == len(PARA)
    for a, b in pairwise(segments):
        assert a.end == b.start
    # The overlapping middle region carries both highlight IDs.
    overlap = [s for s in segments if len(s.highlight_ids) == 2]
    assert overlap, "expected a doubly-covered segment"
    covered = PARA[overlap[0].start : overlap[0].end]
    assert covered == "results were"


def test_nested_highlights_split_at_boundaries() -> None:
    result = make_result(
        indicators=[
            {
                "category": "other",
                "evidence": "The results were significant.",
                "explanation": "outer",
            }
        ],
        quality_issues=[
            {"category": "wordiness", "target_text": "results", "description": "inner"}
        ],
    )
    highlights = resolve_highlights(result, PARA)
    segments = split_segments(highlights, len(PARA))
    inner = [s for s in segments if len(s.highlight_ids) == 2]
    assert len(inner) == 1
    assert PARA[inner[0].start : inner[0].end] == "results"


def test_unresolved_highlights_do_not_split() -> None:
    result = make_result(
        indicators=[{"category": "other", "evidence": "absent text", "explanation": "x"}]
    )
    highlights = resolve_highlights(result, PARA)
    segments = split_segments(highlights, len(PARA))
    assert len(segments) == 1
    assert segments[0].highlight_ids == []
