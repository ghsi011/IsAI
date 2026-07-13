"""Classification of provider CLI failures into the shared error taxonomy.

Patterns match provider *diagnostics*, never document content. The classified
message we log is always our own wording — raw CLI output is never propagated
into logs or reports.
"""

from __future__ import annotations

import re

from isai.errors import ErrorCategory

_USAGE_LIMIT_PATTERNS = (
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"hit your usage", re.IGNORECASE),
    re.compile(r"limit reached", re.IGNORECASE),
    re.compile(r"out of (?:usage|quota)", re.IGNORECASE),
)

_RATE_LIMIT_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
)

_AUTH_PATTERNS = (
    re.compile(r"not logged in", re.IGNORECASE),
    re.compile(r"login required", re.IGNORECASE),
    re.compile(r"please (?:log ?in|sign ?in|authenticate)", re.IGNORECASE),
    re.compile(r"\b401\b"),
    re.compile(r"unauthorized", re.IGNORECASE),
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"authentication[_ ]error", re.IGNORECASE),
)

_COMPAT_PATTERNS = (
    re.compile(r"unknown option", re.IGNORECASE),
    re.compile(r"unexpected argument", re.IGNORECASE),
    re.compile(r"unrecognized (?:option|argument)", re.IGNORECASE),
)

_TRANSIENT_PATTERNS = (
    re.compile(r"\b5\d\d\b"),
    re.compile(r"timed? ?out", re.IGNORECASE),
    re.compile(r"connection (?:reset|refused|error)", re.IGNORECASE),
    re.compile(r"network", re.IGNORECASE),
    re.compile(r"stream error", re.IGNORECASE),
    re.compile(r"temporarily unavailable", re.IGNORECASE),
)


_CLASSIFICATION_ORDER: tuple[tuple[tuple[re.Pattern[str], ...], ErrorCategory], ...] = (
    (_USAGE_LIMIT_PATTERNS, ErrorCategory.USAGE_LIMIT),
    (_RATE_LIMIT_PATTERNS, ErrorCategory.RATE_LIMIT),
    (_AUTH_PATTERNS, ErrorCategory.AUTHENTICATION),
    (_COMPAT_PATTERNS, ErrorCategory.CONFIGURATION),
    (_TRANSIENT_PATTERNS, ErrorCategory.PROVIDER_TRANSIENT),
)


def classify_cli_failure(stdout: str, stderr: str, exit_code: int | None) -> ErrorCategory:
    """Best-effort classification of a failed CLI invocation."""
    haystack = f"{stderr}\n{stdout}"
    for patterns, category in _CLASSIFICATION_ORDER:
        if any(p.search(haystack) for p in patterns):
            return category
    if exit_code == 2:  # conventional usage/argument error
        return ErrorCategory.CONFIGURATION
    return ErrorCategory.PROVIDER_TRANSIENT
