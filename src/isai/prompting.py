"""Prompt loading and review-task serialization.

Prompts are versioned text files (``prompts/reviewer_v1.txt`` in the repo, shipped
inside the wheel as ``isai/_prompts``). Document text enters the prompt only as
values inside a JSON payload explicitly framed as untrusted quoted data.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from isai.errors import ErrorCategory, IsaiError
from isai.models import Scope, review_result_json_schema

REVIEWER_PROMPT_NAME = "reviewer_v1.txt"
REPAIR_PROMPT_NAME = "repair_v1.txt"

#: The reviewer prompt version recorded in job fingerprints.
PROMPT_VERSION = "v1"


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """Load a prompt by file name from the wheel or the repo checkout."""
    candidates = (
        Path(__file__).parent / "_prompts" / name,  # installed wheel
        Path(__file__).resolve().parents[2] / "prompts" / name,  # editable / repo
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise IsaiError(
        ErrorCategory.CONFIGURATION,
        f"prompt file '{name}' not found; the installation is incomplete",
    )


class ContextParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    position: Literal["before", "after"]
    text: str


class ReviewTask(BaseModel):
    """Everything a provider needs to review one paragraph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    display_number: int
    location: str
    style_name: str
    nearest_heading: str | None
    word_count: int
    min_words: int
    scope: Scope
    text: str
    context_before: list[ContextParagraph] = []
    context_after: list[ContextParagraph] = []


def task_payload(task: ReviewTask) -> dict[str, object]:
    """The untrusted-data JSON block appended to the reviewer prompt."""
    return {
        "schema_version": "1.0",
        "scope": task.scope.value,
        "min_words": task.min_words,
        "result_json_schema": review_result_json_schema(),
        "target": {
            "element_id": task.element_id,
            "display_number": task.display_number,
            "location": task.location,
            "style": task.style_name,
            "nearest_heading": task.nearest_heading,
            "word_count": task.word_count,
            "text": task.text,
        },
        "context_before": [
            {"element_id": c.element_id, "text": c.text} for c in task.context_before
        ],
        "context_after": [{"element_id": c.element_id, "text": c.text} for c in task.context_after],
    }


def build_review_prompt(task: ReviewTask) -> str:
    payload = json.dumps(task_payload(task), ensure_ascii=False, indent=2)
    return load_prompt(REVIEWER_PROMPT_NAME) + "\n" + payload + "\n"


def build_repair_prompt(task: ReviewTask, previous_output: str, issues_text: str) -> str:
    """The single-retry correction prompt; quotes the invalid output as data."""
    template = load_prompt(REPAIR_PROMPT_NAME)
    filled = template.format(issues=issues_text, previous_output=previous_output)
    payload = json.dumps(task_payload(task), ensure_ascii=False, indent=2)
    return filled + "\n" + payload + "\n"
