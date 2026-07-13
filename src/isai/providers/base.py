"""Provider-neutral types and the ReviewProvider interface."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from isai.errors import ErrorCategory
from isai.models import ReviewResult
from isai.prompting import ReviewTask


class ProviderName(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class AuthState(StrEnum):
    SUBSCRIPTION = "subscription"
    API_BILLED = "api_billed"
    MISSING = "missing"
    UNKNOWN = "unknown"


class ProviderSettings(BaseModel):
    """Per-provider invocation configuration.

    ``command_prefix`` is the injectable executable prefix — tests substitute
    ``[sys.executable, "-m", "tests.fake_cli", "claude"]`` here; production uses
    ``["claude"]`` / ``["codex"]``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_prefix: list[str]
    model: str | None = None
    effort: str | None = None
    timeout_seconds: int = 300
    max_retries: int = 1  # schema-repair retries (0 or 1)
    allow_api_billed: bool = False
    debug: bool = False


class PreflightStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    installed: bool
    version: str | None = None
    auth_state: AuthState = AuthState.UNKNOWN
    capabilities_ok: bool = False
    missing_capabilities: list[str] = []
    billing_env_vars: list[str] = []  # names only, never values
    usable: bool = False
    message: str = ""  # log-safe

    def blocking_category(self) -> ErrorCategory | None:
        if not self.installed:
            return ErrorCategory.CONFIGURATION
        if not self.capabilities_ok:
            return ErrorCategory.CONFIGURATION
        if self.auth_state is AuthState.MISSING:
            return ErrorCategory.AUTHENTICATION
        if not self.usable:
            return ErrorCategory.BILLING_MODE
        return None


class AttemptRecord(BaseModel):
    """One provider subprocess attempt, recorded log-safely."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int  # 1-based
    requested_model: str | None
    cli_version: str | None
    duration_seconds: float
    exit_code: int | None
    timed_out: bool
    raw_response_sha256: str | None  # hash of raw stdout; raw text is never stored
    status: str  # "ok" | "invalid" | "failed"
    error_category: ErrorCategory | None = None
    error_message: str = ""  # sanitized, log-safe (our words, never provider prose)


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName
    result: ReviewResult | None = None
    error_category: ErrorCategory | None = None
    error_message: str = ""
    attempts: list[AttemptRecord] = []

    @property
    def ok(self) -> bool:
        return self.result is not None


@runtime_checkable
class ReviewProvider(Protocol):
    name: ProviderName

    def preflight(self) -> PreflightStatus: ...

    def review(self, task: ReviewTask) -> ReviewOutcome: ...
