"""Strict review-result schema (Pydantic models + exported JSON Schema).

This is the contract between IsAI and the provider CLIs. Every model forbids extra
fields (``additionalProperties: false`` in the exported schema), every list is capped
at :data:`MAX_LIST_ITEMS`, and the language constraints of the product framing
(stylistic screening, never authorship determination) are enforced separately in
:mod:`isai.validation`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
MAX_LIST_ITEMS = 5

#: Occurrence indices are 1-based: ``occurrence_index=1`` is the first occurrence of
#: the quoted text within the target paragraph. ``null``/omitted means "first / only".
OCCURRENCE_INDEX_BASE = 1


class Scope(StrEnum):
    PARAGRAPH = "paragraph"
    CONTEXT_WINDOW = "context_window"


class StyleSignal(StrEnum):
    NONE = "none"
    MILD = "mild"
    MODERATE = "moderate"
    STRONG = "strong"
    INDETERMINATE = "indeterminate"


class Level(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IndicatorCategory(StrEnum):
    FORMULAIC_TRANSITION = "formulaic_transition"
    UNIFORM_SENTENCE_STRUCTURE = "uniform_sentence_structure"
    REPETITIVE_RESTATEMENT = "repetitive_restatement"
    GENERIC_ABSTRACTION = "generic_abstraction"
    CONTENT_LIGHT_ELABORATION = "content_light_elaboration"
    TEMPLATE_LIKE_STRUCTURE = "template_like_structure"
    ABRUPT_STYLE_SHIFT = "abrupt_style_shift"
    EXCESSIVE_SYMMETRY = "excessive_symmetry"
    UNSUPPORTED_SYNTHESIS = "unsupported_synthesis"
    GENERIC_CITATION_FRAMING = "generic_citation_framing"
    VAGUE_IMPLICATION = "vague_implication"
    OVERLOADED_SENTENCE = "overloaded_sentence"
    OTHER = "other"


class QualityCategory(StrEnum):
    REPETITION = "repetition"
    CLARITY = "clarity"
    STRUCTURE = "structure"
    SPECIFICITY = "specificity"
    CITATION_ALIGNMENT = "citation_alignment"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    WORDINESS = "wordiness"
    TRANSITION = "transition"
    OTHER = "other"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


OccurrenceIndex = Annotated[int, Field(ge=1, le=999)]
ShortText = Annotated[str, Field(max_length=2000)]
LongText = Annotated[str, Field(max_length=4000)]


class Indicator(_StrictModel):
    """One observed stylistic feature, with exact quoted evidence."""

    category: IndicatorCategory
    evidence: ShortText = Field(
        description="Exact quotation from the target paragraph (may be empty only for "
        "whole-paragraph observations such as overall structure)."
    )
    occurrence_index: OccurrenceIndex | None = Field(
        default=None,
        description="1-based occurrence of `evidence` within the target paragraph when "
        "the quoted text appears more than once; null means first/only.",
    )
    explanation: LongText


class QualityIssue(_StrictModel):
    category: QualityCategory
    target_text: ShortText = Field(
        description="Exact quotation the issue refers to; empty only for "
        "whole-paragraph organizational issues."
    )
    occurrence_index: OccurrenceIndex | None = None
    description: LongText


class CitationObservation(_StrictModel):
    observation: LongText
    target_text: ShortText = Field(
        default="",
        description="Exact quotation of the citation-bearing text, when applicable.",
    )
    occurrence_index: OccurrenceIndex | None = None
    requires_source_check: bool = Field(
        description="True when a human must verify the cited source itself."
    )


class RevisionSuggestion(_StrictModel):
    target_text: ShortText = Field(
        description="Exact quotation of the wording to revise; empty only for "
        "whole-paragraph organizational suggestions."
    )
    occurrence_index: OccurrenceIndex | None = None
    issue: LongText
    recommended_change: LongText
    proposed_replacement: LongText | None = Field(
        default=None,
        description="Optional replacement limited to the targeted wording; must "
        "preserve factual meaning and citation attribution.",
    )
    reason: LongText
    requires_source_check: bool = False


class ReviewResult(_StrictModel):
    """The complete structured result for one reviewed paragraph."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    scope: Scope
    style_signal: StyleSignal
    assessment_confidence: Level = Field(
        description="Confidence in the *observations*, never in authorship."
    )
    review_priority: Level
    summary: LongText
    indicators: list[Indicator] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    counter_indicators: list[Indicator] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    quality_issues: list[QualityIssue] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    citation_observations: list[CitationObservation] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    manual_checks: list[LongText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    revision_suggestions: list[RevisionSuggestion] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    needs_second_opinion: bool = False
    limitations_note: LongText = Field(
        description="Mandatory statement of what this stylistic assessment cannot "
        "determine (authorship, intent, tool use)."
    )


def review_result_json_schema() -> dict[str, Any]:
    """JSON Schema handed to provider CLIs (``--json-schema`` / ``--output-schema``)."""
    return ReviewResult.model_json_schema()
