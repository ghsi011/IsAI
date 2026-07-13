"""Shared scenario logic for the mock provider CLIs (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCENARIOS = frozenset(
    {
        "success",
        "malformed_json",
        "malformed_then_success",
        "schema_violation",
        "schema_violation_then_success",
        "usage_limit",
        "rate_limit",
        "timeout",
        "auth_subscription",
        "auth_api_billed",
        "auth_missing",
        "unsupported_flag",
        "delayed_completion",
        "spawn_child",
    }
)

LIMITATIONS_NOTE = "Stylistic observation only; authorship cannot be determined from style alone."


def scenario() -> str:
    value = os.environ.get("MOCK_LLM_SCENARIO", "success")
    if value not in SCENARIOS:
        print(f"fake_cli: unknown MOCK_LLM_SCENARIO {value!r}", file=sys.stderr)
        raise SystemExit(64)
    return value


def log_invocation(
    tool: str, argv: list[str], stdin_data: bytes, exit_code: int, output_path: str | None
) -> None:
    """Append one safe record; never any document or prompt text."""
    log_path = os.environ.get("MOCK_LLM_LOG")
    if not log_path:
        return
    record = {
        "tool": tool,
        "argv": argv,
        "stdin_sha256": hashlib.sha256(stdin_data).hexdigest(),
        "stdin_bytes": len(stdin_data),
        "scenario": os.environ.get("MOCK_LLM_SCENARIO", "success"),
        "exit_code": exit_code,
        "output_path": output_path,
        "pid": os.getpid(),
    }
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_stdin_bytes() -> bytes:
    return sys.stdin.buffer.read()


def parse_task_from_prompt(prompt: str) -> dict[str, Any]:
    """Recover the task payload: the JSON object appended at the prompt's end."""
    idx = prompt.rfind("\n{")
    if idx == -1:
        raise ValueError("no task payload found in prompt")
    return json.loads(prompt[idx + 1 :])


def _first_words(text: str, count: int) -> str:
    """An exact-substring prefix covering the first ``count`` words."""
    words = text.split()
    if len(words) < count:
        return text.strip()
    prefix = " ".join(words[:count])
    pos = text.find(words[0])
    end = text.find(words[count - 1], pos) + len(words[count - 1])
    exact = text[pos:end]
    return exact if exact.strip() else prefix


def build_success_result(task: dict[str, Any]) -> dict[str, Any]:
    """A schema- and content-valid ReviewResult derived from the actual target."""
    target = task["target"]
    text: str = target["text"]
    word_count: int = target["word_count"]
    min_words: int = task["min_words"]
    scope: str = task["scope"]

    if word_count < min_words:
        return {
            "schema_version": "1.0",
            "scope": scope,
            "style_signal": "indeterminate",
            "assessment_confidence": "low",
            "review_priority": "low",
            "summary": "Insufficient text for reliable stylistic assessment.",
            "indicators": [],
            "counter_indicators": [],
            "quality_issues": [],
            "citation_observations": [],
            "manual_checks": [],
            "revision_suggestions": [],
            "needs_second_opinion": False,
            "limitations_note": LIMITATIONS_NOTE,
        }

    evidence = _first_words(text, 4)
    return {
        "schema_version": "1.0",
        "scope": scope,
        "style_signal": "mild",
        "assessment_confidence": "medium",
        "review_priority": "medium",
        "summary": (
            "The paragraph shows mildly uniform constructions; manual review "
            "recommended for the quoted passage."
        ),
        "indicators": [
            {
                "category": "formulaic_transition",
                "evidence": evidence,
                "occurrence_index": 1,
                "explanation": "The opening follows a template-like pattern.",
            }
        ],
        "counter_indicators": [],
        "quality_issues": [
            {
                "category": "specificity",
                "target_text": evidence,
                "occurrence_index": 1,
                "description": "The opening could name the concrete subject sooner.",
            }
        ],
        "citation_observations": [],
        "manual_checks": [],
        "revision_suggestions": [
            {
                "target_text": evidence,
                "occurrence_index": 1,
                "issue": "Generic opening.",
                "recommended_change": "Start with the specific finding or population.",
                "proposed_replacement": None,
                "reason": "Specific openings connect claims to evidence.",
                "requires_source_check": False,
            }
        ],
        "needs_second_opinion": False,
        "limitations_note": LIMITATIONS_NOTE,
    }


def build_schema_violation_result(task: dict[str, Any]) -> dict[str, Any]:
    """Valid JSON that violates content rules (fabricated evidence + authorship claim)."""
    result = build_success_result(task)
    result["summary"] = "This paragraph was written by ChatGPT."
    result["indicators"] = [
        {
            "category": "generic_abstraction",
            "evidence": "totally fabricated quotation that is not in the text",
            "occurrence_index": 1,
            "explanation": "Fabricated for testing.",
        }
    ]
    return result


def invocation_count(key: str) -> int:
    """Per-key invocation counter backed by MOCK_LLM_STATE_DIR (1-based)."""
    state_dir = os.environ.get("MOCK_LLM_STATE_DIR")
    if not state_dir:
        return 1
    path = Path(state_dir) / f"count-{key}.txt"
    count = 1
    if path.is_file():
        count = int(path.read_text(encoding="ascii")) + 1
    path.write_text(str(count), encoding="ascii")
    return count


def sleep_hang() -> None:
    time.sleep(float(os.environ.get("MOCK_LLM_HANG_SECONDS", "120")))


def sleep_delay() -> None:
    time.sleep(float(os.environ.get("MOCK_LLM_DELAY_SECONDS", "1.5")))


def spawn_grandchild() -> int:
    """Start a long-sleeping grandchild; returns its PID (for tree-kill tests)."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    return child.pid
