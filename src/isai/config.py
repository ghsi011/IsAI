"""Job configuration and its fingerprint (resume-safety key)."""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from isai.docxio import ExtractionConfig
from isai.errors import ErrorCategory, IsaiError


def command_override(env_name: str, default: list[str]) -> list[str]:
    """Provider executable override as a JSON array via ISAI_CLAUDE_COMMAND /
    ISAI_CODEX_COMMAND (nonstandard install paths; the executable mocks in tests)."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IsaiError(
            ErrorCategory.CONFIGURATION,
            f"{env_name} must be a JSON array of command parts",
        ) from exc
    if not isinstance(value, list) or not value or not all(isinstance(p, str) for p in value):
        raise IsaiError(
            ErrorCategory.CONFIGURATION,
            f"{env_name} must be a non-empty JSON array of strings",
        )
    return value


class ProviderMode(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    AUTO = "auto"
    CONSENSUS = "consensus"


class ReviewConfig(BaseModel):
    """Everything that affects review results; hashed into the config fingerprint.

    ``claude_command`` / ``codex_command`` are the injectable command prefixes
    (tests point them at the executable mocks). They are part of the fingerprint
    deliberately: results produced by a different executable are not resumable
    into the same journal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_mode: ProviderMode = ProviderMode.CLAUDE
    primary_provider: str | None = None
    second_opinion_provider: str | None = None
    fallback_provider: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    codex_model: str | None = None
    claude_command: list[str] = ["claude"]
    codex_command: list[str] = ["codex"]
    min_words: int = Field(default=50, ge=1)
    context_assisted: bool = True
    context_before: int = Field(default=1, ge=0, le=5)
    context_after: int = Field(default=1, ge=0, le=5)
    include_tables: bool = True
    timeout_seconds: int = Field(default=300, ge=10)
    max_retries: int = Field(default=1, ge=0, le=1)
    audit_percent: int = Field(default=5, ge=0, le=100)
    start_paragraph: int | None = Field(default=None, ge=1)
    end_paragraph: int | None = Field(default=None, ge=1)
    max_paragraphs: int | None = Field(default=None, ge=1)
    allow_api_billed: bool = False
    debug: bool = False

    def extraction_config(self) -> ExtractionConfig:
        return ExtractionConfig(include_tables=self.include_tables)

    def fingerprint(self) -> str:
        # debug affects logging only, never results; exclude it.
        payload = self.model_dump(mode="json", exclude={"debug"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
