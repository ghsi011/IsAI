"""Content-rule validation: fabricated evidence, forbidden language, scope rules."""

from __future__ import annotations

from isai.models import ReviewResult, Scope
from isai.validation import IssueCode, ValidationIssue, repair_instruction, validate_result
from tests.helpers import make_result

PARA = (
    "The retrospective cohort included 412 patients treated between 2015 and 2019. "
    "Median follow-up was 4.2 years, and outcomes were assessed with the Kaplan-Meier "
    "method. Moreover, it is important to note that the results were significant. "
    "Moreover, it is important to note that adherence varied across centers."
)


def codes(issues: list[ValidationIssue]) -> set[IssueCode]:
    return {i.code for i in issues}


def validate(
    result: ReviewResult, *, scope: Scope = Scope.PARAGRAPH, min_words: int = 5
) -> list[ValidationIssue]:
    return validate_result(result, target_text=PARA, requested_scope=scope, min_words=min_words)


def test_clean_result_passes() -> None:
    result = make_result(
        indicators=[
            {
                "category": "formulaic_transition",
                "evidence": "Moreover, it is important to note",
                "occurrence_index": 2,
                "explanation": "The same stock transition opens consecutive sentences.",
            }
        ]
    )
    assert validate(result) == []


def test_fabricated_evidence_rejected() -> None:
    result = make_result(
        indicators=[
            {
                "category": "generic_abstraction",
                "evidence": "this text never says this",
                "explanation": "x",
            }
        ]
    )
    assert IssueCode.FABRICATED_EVIDENCE in codes(validate(result))


def test_out_of_range_occurrence_index_rejected() -> None:
    result = make_result(
        indicators=[
            {
                "category": "formulaic_transition",
                "evidence": "Moreover",
                "occurrence_index": 5,
                "explanation": "x",
            }
        ]
    )
    assert IssueCode.BAD_OCCURRENCE_INDEX in codes(validate(result))


def test_authorship_claim_rejected() -> None:
    result = make_result(summary="This paragraph was likely written by ChatGPT.")
    assert IssueCode.AUTHORSHIP_CLAIM in codes(validate(result))


def test_authorship_probability_rejected() -> None:
    result = make_result(summary="There is an 85% probability of AI authorship.")
    assert IssueCode.AUTHORSHIP_CLAIM in codes(validate(result))


def test_human_authorship_claim_also_rejected() -> None:
    result = make_result(summary="This was clearly written by a human author.")
    assert IssueCode.AUTHORSHIP_CLAIM in codes(validate(result))


def test_permitted_register_not_flagged() -> None:
    result = make_result(
        summary=(
            "The paragraph contains strong AI-associated stylistic patterns, including "
            "unusually uniform constructions. Manual review recommended; authorship "
            "cannot be determined from style alone."
        )
    )
    assert validate(result) == []


def test_evasion_advice_rejected() -> None:
    result = make_result(
        revision_suggestions=[
            {
                "target_text": "the results were significant",
                "issue": "Wording is generic.",
                "recommended_change": "Vary the sentence openings to avoid detection tools.",
                "reason": "x",
            }
        ]
    )
    assert IssueCode.EVASION_ADVICE in codes(validate(result))


def test_intentional_errors_advice_rejected() -> None:
    result = make_result(manual_checks=["Consider adding a few typos to make it look human."])
    issues = codes(validate(result))
    assert IssueCode.EVASION_ADVICE in issues


def test_short_paragraph_strong_rejected() -> None:
    result = make_result(style_signal="strong")
    issues = validate_result(
        result,
        target_text="Only a few words here.",
        requested_scope=Scope.PARAGRAPH,
        min_words=50,
    )
    assert IssueCode.SHORT_PARAGRAPH_SIGNAL in codes(issues)


def test_short_paragraph_indeterminate_allowed() -> None:
    result = make_result(style_signal="indeterminate")
    issues = validate_result(
        result,
        target_text="Only a few words here.",
        requested_scope=Scope.PARAGRAPH,
        min_words=50,
    )
    assert issues == []


def test_scope_mismatch_rejected() -> None:
    result = make_result(scope=Scope.PARAGRAPH)
    issues = validate(result, scope=Scope.CONTEXT_WINDOW)
    assert IssueCode.SCOPE_MISMATCH in codes(issues)


def test_missing_limitations_note_rejected() -> None:
    result = make_result(limitations_note="   ")
    assert IssueCode.MISSING_LIMITATIONS_NOTE in codes(validate(result))


def test_replacement_without_target_rejected() -> None:
    result = make_result(
        revision_suggestions=[
            {
                "target_text": "",
                "issue": "Overall flow.",
                "recommended_change": "Reorder the paragraph.",
                "proposed_replacement": "A full rewrite.",
                "reason": "x",
            }
        ]
    )
    assert IssueCode.REPLACEMENT_WITHOUT_TARGET in codes(validate(result))


def test_empty_target_without_replacement_allowed() -> None:
    result = make_result(
        revision_suggestions=[
            {
                "target_text": "",
                "issue": "Overall organization could foreground the cohort size.",
                "recommended_change": "Lead with the patient count.",
                "reason": "Improves specificity.",
            }
        ]
    )
    assert validate(result) == []


def test_evidence_with_smart_quotes_matches_straight_quoted_document() -> None:
    result = make_result(
        indicators=[
            {
                "category": "other",
                "evidence": "Kaplan–Meier",  # en dash; document has hyphen
                "explanation": "x",
            }
        ]
    )
    assert validate(result) == []


def test_repair_instruction_is_log_safe_and_names_codes() -> None:
    result = make_result(summary="This paragraph was written by an AI.")
    issues = validate(result)
    text = repair_instruction(issues)
    assert "authorship_claim" in text
    assert "written by an AI" not in text  # never echoes provider prose
