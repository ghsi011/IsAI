"""Shared review-attempt loop for CLI-backed providers.

Adapters supply argv construction and stdout/file parsing; this module owns the
invariant flow: isolated workdir → stdin-only prompt delivery → parse → validate →
exactly one schema-repair retry → classified outcome. Process-level failures
(timeout, non-zero exit) are never retried here; only repairable output problems
(malformed JSON, schema violation, content-rule violation) trigger the retry.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import ValidationError

from isai.errors import ErrorCategory
from isai.models import ReviewResult
from isai.prompting import ReviewTask, build_repair_prompt, build_review_prompt
from isai.providers.base import (
    AttemptRecord,
    PreflightStatus,
    ProviderName,
    ProviderSettings,
    ReviewOutcome,
)
from isai.providers.classify import classify_cli_failure
from isai.providers.runner import (
    ProcessResult,
    isolated_workdir,
    run_process,
    scrubbed_child_env,
)
from isai.validation import repair_instruction, validate_result

#: How much of an invalid response is quoted back (as data) in the repair prompt.
_REPAIR_QUOTE_MAX_CHARS = 8000

_PREFLIGHT_TIMEOUT_SECONDS = 60


class OutputParseError(Exception):
    """Raised by adapters when provider output cannot yield a result document."""

    def __init__(self, reason: str) -> None:  # reason is log-safe
        super().__init__(reason)
        self.reason = reason


def check_capabilities(help_text: str, required_flags: tuple[str, ...]) -> list[str]:
    """Return the required flags that are absent from ``--help`` output."""
    return [flag for flag in required_flags if flag not in help_text]


class CliReviewAdapter(ABC):
    """Template for claude/codex adapters. Subclasses stay free of flow logic."""

    name: ProviderName

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings
        self._cli_version: str | None = None

    # -- subclass surface ---------------------------------------------------

    @abstractmethod
    def preflight(self) -> PreflightStatus: ...

    @abstractmethod
    def _review_argv(self, workdir: Path) -> list[str]:
        """Full argv for one review invocation. Never contains document text."""

    @abstractmethod
    def _extract_result_json(self, proc: ProcessResult, workdir: Path) -> str:
        """The result-document JSON text from a zero-exit invocation.

        Raises :class:`OutputParseError` when the output shape is unusable.
        """

    # -- shared helpers -----------------------------------------------------

    def _run(self, argv: list[str], stdin_text: str | None, cwd: Path) -> ProcessResult:
        return run_process(
            argv,
            stdin_text=stdin_text,
            timeout_seconds=self.settings.timeout_seconds,
            cwd=cwd,
            env=scrubbed_child_env(),
        )

    def _run_quick(self, extra_argv: list[str]) -> ProcessResult:
        """Short-timeout invocation for preflight commands (no document text)."""
        with isolated_workdir(prefix="isai-preflight-") as wd:
            return run_process(
                self.settings.command_prefix + extra_argv,
                stdin_text=None,
                timeout_seconds=_PREFLIGHT_TIMEOUT_SECONDS,
                cwd=wd,
                env=scrubbed_child_env(),
            )

    # -- the invariant review flow -------------------------------------------

    def review(self, task: ReviewTask) -> ReviewOutcome:
        attempts: list[AttemptRecord] = []
        prompt = build_review_prompt(task)
        max_attempts = 1 + max(0, min(1, self.settings.max_retries))

        for attempt_no in range(1, max_attempts + 1):
            with isolated_workdir() as workdir:
                argv = self._review_argv(workdir)
                try:
                    proc = self._run(argv, prompt, workdir)
                except FileNotFoundError:
                    return self._failure(
                        attempts,
                        ErrorCategory.CONFIGURATION,
                        f"{self.name.value} executable not found on PATH",
                    )
                process_failure = self._process_failure(proc, attempts, attempt_no)
                if process_failure is not None:
                    return process_failure

                raw_json, parse_error = self._try_extract(proc, workdir)

            if parse_error is None and raw_json is not None:
                result, problems = self._validate(raw_json, task)
                if result is not None:
                    attempts.append(self._record(proc, attempt_no, "ok"))
                    return ReviewOutcome(provider=self.name, result=result, attempts=attempts)
                parse_error = problems

            # Repairable output problem.
            attempts.append(
                self._record(
                    proc,
                    attempt_no,
                    "invalid",
                    ErrorCategory.VALIDATION,
                    parse_error or "invalid provider output",
                )
            )
            if attempt_no < max_attempts:
                previous = (proc.stdout or "")[:_REPAIR_QUOTE_MAX_CHARS]
                prompt = build_repair_prompt(
                    task, previous, parse_error or "output was not valid JSON"
                )

        return ReviewOutcome(
            provider=self.name,
            error_category=ErrorCategory.VALIDATION,
            error_message="provider output failed validation"
            + (" after one repair retry" if max_attempts > 1 else " (repair retry disabled)"),
            attempts=attempts,
        )

    # -- internals ------------------------------------------------------------

    def _try_extract(self, proc: ProcessResult, workdir: Path) -> tuple[str | None, str | None]:
        try:
            return self._extract_result_json(proc, workdir), None
        except OutputParseError as exc:
            return None, exc.reason

    def _validate(self, raw_json: str, task: ReviewTask) -> tuple[ReviewResult | None, str]:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            return None, "response was not valid JSON"
        try:
            result = ReviewResult.model_validate(parsed)
        except ValidationError as exc:
            names = sorted({str(err["loc"][0]) if err["loc"] else "(root)" for err in exc.errors()})
            return None, "schema validation failed for fields: " + ", ".join(names)
        issues = validate_result(
            result,
            target_text=task.text,
            requested_scope=task.scope,
            min_words=task.min_words,
        )
        if issues:
            return None, repair_instruction(issues)
        return result, ""

    def _process_failure(
        self, proc: ProcessResult, attempts: list[AttemptRecord], attempt_no: int
    ) -> ReviewOutcome | None:
        if proc.timed_out:
            attempts.append(
                self._record(
                    proc,
                    attempt_no,
                    "failed",
                    ErrorCategory.TIMEOUT,
                    f"provider timed out after {self.settings.timeout_seconds}s "
                    "(process tree terminated)",
                )
            )
            return ReviewOutcome(
                provider=self.name,
                error_category=ErrorCategory.TIMEOUT,
                error_message="provider invocation timed out",
                attempts=attempts,
            )
        if proc.exit_code != 0:
            category = classify_cli_failure(proc.stdout, proc.stderr, proc.exit_code)
            message = f"provider exited with code {proc.exit_code} ({category.value})"
            attempts.append(self._record(proc, attempt_no, "failed", category, message))
            return ReviewOutcome(
                provider=self.name,
                error_category=category,
                error_message=message,
                attempts=attempts,
            )
        return None

    def _record(
        self,
        proc: ProcessResult,
        attempt_no: int,
        status: str,
        category: ErrorCategory | None = None,
        message: str = "",
    ) -> AttemptRecord:
        return AttemptRecord(
            attempt=attempt_no,
            requested_model=self.settings.model,
            cli_version=self._cli_version,
            duration_seconds=round(proc.duration_seconds, 3),
            exit_code=proc.exit_code,
            timed_out=proc.timed_out,
            raw_response_sha256=(
                hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest() if proc.stdout else None
            ),
            status=status,
            error_category=category,
            error_message=message,
        )

    def _failure(
        self, attempts: list[AttemptRecord], category: ErrorCategory, message: str
    ) -> ReviewOutcome:
        return ReviewOutcome(
            provider=self.name,
            error_category=category,
            error_message=message,
            attempts=attempts,
        )
