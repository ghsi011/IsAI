"""Shared builders for test data. Synthetic content only — never real documents."""

from __future__ import annotations

from typing import Any

from isai.models import ReviewResult, Scope, StyleSignal

LIMITATIONS = "Stylistic observation only; authorship cannot be determined from style alone."


def make_result(**overrides: Any) -> ReviewResult:
    """A minimal valid ReviewResult; override any field via kwargs."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope": Scope.PARAGRAPH,
        "style_signal": StyleSignal.MILD,
        "assessment_confidence": "medium",
        "review_priority": "low",
        "summary": "The paragraph reads naturally with specific detail.",
        "indicators": [],
        "counter_indicators": [],
        "quality_issues": [],
        "citation_observations": [],
        "manual_checks": [],
        "revision_suggestions": [],
        "needs_second_opinion": False,
        "limitations_note": LIMITATIONS,
    }
    payload.update(overrides)
    return ReviewResult.model_validate(payload)


def result_payload(**overrides: Any) -> dict[str, Any]:
    """The same minimal result as a plain dict (for schema-violation tests)."""
    return make_result(**overrides).model_dump(mode="json")
