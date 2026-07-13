"""Structural schema tests: strictness, enums, list caps, JSON Schema export."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from isai.models import (
    MAX_LIST_ITEMS,
    ReviewResult,
    review_result_json_schema,
)
from tests.helpers import make_result, result_payload


def test_minimal_result_validates() -> None:
    result = make_result()
    assert result.schema_version == "1.0"
    assert result.revision_suggestions == []


def test_extra_fields_rejected_at_every_level() -> None:
    payload = result_payload()
    payload["authorship_verdict"] = "ai"
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_nested_extra_field_rejected() -> None:
    payload = result_payload()
    payload["indicators"] = [
        {
            "category": "formulaic_transition",
            "evidence": "Moreover",
            "explanation": "Stock transition.",
            "sneaky": True,
        }
    ]
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_unknown_enum_value_rejected() -> None:
    payload = result_payload()
    payload["style_signal"] = "definitely_ai"
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_wrong_schema_version_rejected() -> None:
    payload = result_payload()
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_list_cap_enforced() -> None:
    item = {
        "category": "other",
        "evidence": "",
        "explanation": "x",
    }
    payload = result_payload()
    payload["indicators"] = [item] * (MAX_LIST_ITEMS + 1)
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_occurrence_index_must_be_positive() -> None:
    payload = result_payload()
    payload["indicators"] = [
        {
            "category": "other",
            "evidence": "text",
            "occurrence_index": 0,
            "explanation": "x",
        }
    ]
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(payload)


def test_json_schema_export_is_strict() -> None:
    schema = review_result_json_schema()
    assert schema["additionalProperties"] is False
    for name, definition in schema.get("$defs", {}).items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
    # Enum surfaces in the exported schema.
    signal = schema["$defs"]["StyleSignal"]["enum"]
    assert set(signal) == {"none", "mild", "moderate", "strong", "indeterminate"}


def test_results_are_immutable() -> None:
    result = make_result()
    with pytest.raises(ValidationError):
        result.summary = "changed"  # type: ignore[misc]
