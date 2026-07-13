"""Highlight resolution and overlap splitting.

Every quotation in a validated result is resolved to original-text offsets via
:mod:`isai.textmatch` (never trusting model-supplied positions). Overlapping or
nested highlights are rendered by splitting the paragraph into segments at span
boundaries; each segment knows every highlight covering it, so no text is ever
duplicated or lost.
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict

from isai.models import ReviewResult
from isai.textmatch import MatchTier, resolve_quote


class HighlightKind(StrEnum):
    INDICATOR = "indicator"
    COUNTER_INDICATOR = "counter_indicator"
    QUALITY = "quality"
    CITATION = "citation"
    SUGGESTION = "suggestion"


class Highlight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    highlight_id: str  # stable within one result, e.g. "indicator-0"
    kind: HighlightKind
    category: str
    field: str  # result field path, e.g. "indicators[0]"
    quote: str
    occurrence_index: int | None
    tier: MatchTier
    start: int  # -1 when unresolved
    end: int


class Segment(BaseModel):
    """A run of paragraph text covered by a constant set of highlights."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int
    end: int
    highlight_ids: list[str]


def resolve_highlights(result: ReviewResult, target_text: str) -> list[Highlight]:
    """Resolve every non-empty quotation in ``result`` against the target text."""
    highlights: list[Highlight] = []

    def add(
        kind: HighlightKind, category: str, field: str, quote: str, occurrence: int | None
    ) -> None:
        if not quote:
            return
        span = resolve_quote(target_text, quote, occurrence)
        highlights.append(
            Highlight(
                highlight_id=field.replace("[", "-").replace("]", ""),
                kind=kind,
                category=category,
                field=field,
                quote=quote,
                occurrence_index=occurrence,
                tier=span.tier,
                start=span.start,
                end=span.end,
            )
        )

    for i, ind in enumerate(result.indicators):
        add(
            HighlightKind.INDICATOR,
            ind.category.value,
            f"indicators[{i}]",
            ind.evidence,
            ind.occurrence_index,
        )
    for i, ind in enumerate(result.counter_indicators):
        add(
            HighlightKind.COUNTER_INDICATOR,
            ind.category.value,
            f"counter_indicators[{i}]",
            ind.evidence,
            ind.occurrence_index,
        )
    for i, qi in enumerate(result.quality_issues):
        add(
            HighlightKind.QUALITY,
            qi.category.value,
            f"quality_issues[{i}]",
            qi.target_text,
            qi.occurrence_index,
        )
    for i, co in enumerate(result.citation_observations):
        add(
            HighlightKind.CITATION,
            "citation",
            f"citation_observations[{i}]",
            co.target_text,
            co.occurrence_index,
        )
    for i, rs in enumerate(result.revision_suggestions):
        add(
            HighlightKind.SUGGESTION,
            "suggestion",
            f"revision_suggestions[{i}]",
            rs.target_text,
            rs.occurrence_index,
        )
    return highlights


def split_segments(highlights: list[Highlight], text_length: int) -> list[Segment]:
    """Split ``[0, text_length)`` at every highlight boundary.

    Returns contiguous, non-overlapping segments covering the whole text; each
    carries the IDs of all highlights active over it. Unresolved highlights
    (start == -1) never produce segments.
    """
    resolved = [h for h in highlights if h.start >= 0 and h.end > h.start]
    boundaries = {0, text_length}
    for h in resolved:
        boundaries.add(min(h.start, text_length))
        boundaries.add(min(h.end, text_length))
    ordered = sorted(boundaries)
    segments: list[Segment] = []
    for start, end in pairwise(ordered):
        if start >= end:
            continue
        covering = [h.highlight_id for h in resolved if h.start <= start and h.end >= end]
        segments.append(Segment(start=start, end=end, highlight_ids=covering))
    return segments
