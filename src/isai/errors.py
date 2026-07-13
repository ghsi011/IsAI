"""Error taxonomy shared across the application.

Every failure surfaced to the journal, the report, the GUI, or the CLI is classified
into exactly one :class:`ErrorCategory` so behavior (retry, pause, abort, continue)
is decided by category, never by string matching.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    DOCUMENT = "document"
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    BILLING_MODE = "billing_mode"
    USAGE_LIMIT = "usage_limit"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    VALIDATION = "validation"
    FILESYSTEM = "filesystem"
    DATABASE = "database"
    WEB_SECURITY = "web_security"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


#: Categories that pause the whole job (state preserved, resumable later) instead of
#: recording a per-paragraph error and continuing.
JOB_PAUSING_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.BILLING_MODE,
        ErrorCategory.USAGE_LIMIT,
        ErrorCategory.INTERRUPTED,
    }
)


class IsaiError(Exception):
    """Application error carrying a category and a log-safe message.

    ``message`` must never contain document text, raw provider output, or secrets —
    it flows into logs and reports unconditionally.
    """

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IsaiError({self.category.value}: {self.message})"
