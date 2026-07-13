"""Job orchestration: prepare → sequential review loop → summary.

Implements the crash-consistent per-paragraph protocol from the spec:

1. mark the task active in SQLite (commit) and publish ``paragraph_started``;
2. build the task and invoke the provider (document text via stdin only);
3. parse/validate (the adapter retries once on repairable output);
4. resolve highlights locally;
5. commit the result to SQLite;
6. append the uniquely-marked Markdown section;
7. flush + fsync + close the report handle;
8. commit the ``markdown_written`` flag;
9. publish the completion event.

A crash between (5) and (8) is repaired by :func:`reconcile` on resume without
duplicating anything. Paragraph errors are recorded and the run continues;
authentication loss, billing-mode problems, and usage exhaustion pause the whole
job with state preserved.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from isai.config import ProviderMode, ReviewConfig
from isai.docxio import DocElement, extract_document, validate_docx_container
from isai.errors import JOB_PAUSING_CATEGORIES, ErrorCategory, IsaiError
from isai.highlights import Highlight, resolve_highlights
from isai.models import Level, ReviewResult, Scope, StyleSignal
from isai.persistence import JobMeta, Journal, ReportWriter, TaskRole, TaskStatus
from isai.persistence.db import JobStatus, utc_now_iso
from isai.persistence.render import render_header, render_summary, render_task_section
from isai.prompting import PROMPT_VERSION, ContextParagraph, ReviewTask
from isai.providers.base import AttemptRecord, ProviderSettings, ReviewProvider
from isai.providers.claude import ClaudeAdapter

EventCallback = Callable[[str, dict[str, object]], None]
StopCheck = Callable[[], bool]

LOCAL_PROVIDER = "local"  # synthesized results (short standalone paragraphs)

SHORT_STANDALONE_LIMITATIONS = (
    "Below the minimum word threshold: too little text for any reliable stylistic "
    "assessment. Authorship cannot be determined from style alone."
)


class ResumeMode(StrEnum):
    AUTO = "auto"  # resume when matching state exists, else start new
    NO_RESUME = "no_resume"  # refuse to touch existing state
    RESTART = "restart"  # discard existing state and start over
    FORCE_NEW_REPORT = "force_new_report"  # keep journal, regenerate the report file


class JobPaths:
    def __init__(self, report: Path) -> None:
        self.report = report
        self.journal = report.with_suffix(".sqlite3")


def make_provider(name: str, config: ReviewConfig) -> ReviewProvider:
    if name == "claude":
        return ClaudeAdapter(
            ProviderSettings(
                command_prefix=list(config.claude_command),
                model=config.claude_model,
                effort=config.claude_effort,
                timeout_seconds=config.timeout_seconds,
                allow_api_billed=config.allow_api_billed,
                debug=config.debug,
            )
        )
    if name == "codex":
        from isai.providers.codex import (  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
            CodexAdapter,
        )

        return CodexAdapter(
            ProviderSettings(
                command_prefix=list(config.codex_command),
                model=config.codex_model,
                timeout_seconds=config.timeout_seconds,
                allow_api_billed=config.allow_api_billed,
                debug=config.debug,
            )
        )
    raise IsaiError(ErrorCategory.CONFIGURATION, f"unknown provider '{name}'")


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reviewable(element: DocElement) -> bool:
    return bool(element.normalized_text) and not element.is_heading


def _in_range(element: DocElement, config: ReviewConfig) -> bool:
    if config.start_paragraph and element.display_number < config.start_paragraph:
        return False
    return not (config.end_paragraph and element.display_number > config.end_paragraph)


class PreparedJob:
    def __init__(
        self, journal: Journal, report: ReportWriter, resumed: bool, paths: JobPaths
    ) -> None:
        self.journal = journal
        self.report = report
        self.resumed = resumed
        self.paths = paths


def prepare_job(
    input_docx: Path,
    output_report: Path,
    config: ReviewConfig,
    resume_mode: ResumeMode = ResumeMode.AUTO,
) -> PreparedJob:
    """Validate, extract, and create or resume the journal + report pair."""
    paths = JobPaths(output_report)
    validate_docx_container(input_docx)
    src_hash = source_sha256(input_docx)

    if resume_mode is ResumeMode.RESTART:
        paths.journal.unlink(missing_ok=True)
        paths.report.unlink(missing_ok=True)

    state_exists = paths.journal.is_file()
    if state_exists and resume_mode is ResumeMode.NO_RESUME:
        raise IsaiError(
            ErrorCategory.CONFIGURATION,
            f"existing job state found ({paths.journal.name}) and --no-resume given; "
            "use --resume, --restart, or a different --output",
        )

    extraction = extract_document(input_docx, config.extraction_config())

    if state_exists:
        journal = Journal.open(paths.journal)
        _verify_resume_safe(journal, src_hash, extraction.extraction_fingerprint, config)
        report = ReportWriter(paths.report)
        if resume_mode is ResumeMode.FORCE_NEW_REPORT or not paths.report.is_file():
            paths.report.unlink(missing_ok=True)
            _write_report_from_journal(journal, report, include_summary=False)
        reconcile(journal, report)
        return PreparedJob(journal, report, resumed=True, paths=paths)

    if paths.report.exists():
        raise IsaiError(
            ErrorCategory.FILESYSTEM,
            f"report file already exists without a journal: {paths.report.name}; "
            "use --restart to overwrite or choose a different --output",
        )

    meta = JobMeta(
        job_id=uuid.uuid4().hex[:12],
        source_filename=input_docx.name,
        source_sha256=src_hash,
        extraction_fingerprint=extraction.extraction_fingerprint,
        config_fingerprint=config.fingerprint(),
        config_json=config.model_dump_json(),
        prompt_version=PROMPT_VERSION,
        provider_mode=config.provider_mode.value,
        created_at=utc_now_iso(),
    )
    roles = [TaskRole.PRIMARY]
    if config.provider_mode is ProviderMode.CONSENSUS:
        roles.append(TaskRole.SECOND_OPINION)
    journal = Journal.create(paths.journal, meta, extraction.elements, roles)

    # The report header must be durably on disk before any provider call.
    report = ReportWriter(paths.report)
    reviewable = sum(1 for e in extraction.elements if _reviewable(e))
    report.create(render_header(meta, extraction.total_count, reviewable))
    return PreparedJob(journal, report, resumed=False, paths=paths)


def _verify_resume_safe(
    journal: Journal, src_hash: str, extraction_fingerprint: str, config: ReviewConfig
) -> None:
    meta = journal.meta()
    problems: list[str] = []
    if meta.source_sha256 != src_hash:
        problems.append("source document changed (SHA-256 mismatch)")
    if meta.extraction_fingerprint != extraction_fingerprint:
        problems.append("extraction configuration changed")
    if meta.config_fingerprint != config.fingerprint():
        problems.append("review configuration changed")
    if problems:
        raise IsaiError(
            ErrorCategory.CONFIGURATION,
            "refusing to resume: " + "; ".join(problems) + ". Use --restart to start "
            "over (existing results will be discarded) or a different --output.",
        )


def reconcile(journal: Journal, report: ReportWriter) -> None:
    """Repair SQLite↔Markdown drift after a crash; never duplicates a section."""
    markers = report.existing_markers()
    elements = {e.element_id: e for e in journal.elements()}
    for task in journal.tasks():
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.ERROR):
            continue
        key = (task.element_id, task.role.value)
        if key in markers:
            if not task.markdown_written:  # crash between fsync and flag commit
                journal.set_markdown_written(task.element_id, task.role)
        else:  # committed to SQLite but the section never reached the file
            report.append_section(render_task_section(elements[task.element_id], task))
            journal.set_markdown_written(task.element_id, task.role)


def _write_report_from_journal(
    journal: Journal, report: ReportWriter, *, include_summary: bool
) -> None:
    meta = journal.meta()
    elements = journal.elements()
    reviewable = sum(1 for e in elements if _reviewable(e))
    report.create(render_header(meta, len(elements), reviewable))
    by_id = {e.element_id: e for e in elements}
    for task in journal.tasks():
        if task.status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
            report.append_section(render_task_section(by_id[task.element_id], task))
            journal.set_markdown_written(task.element_id, task.role)
    if include_summary:
        report.append_section(render_summary(meta, journal.tasks()))


def rebuild_report(journal_path: Path, output: Path) -> None:
    """Deterministically regenerate a report from the journal alone."""
    journal = Journal.open(journal_path)
    try:
        if output.exists():
            output.unlink()
        report = ReportWriter(output)
        meta = journal.meta()
        include_summary = meta.status in (JobStatus.COMPLETED, JobStatus.FAILED)
        _write_report_from_journal(journal, report, include_summary=include_summary)
    finally:
        journal.close()


def _short_standalone_result(scope: Scope) -> ReviewResult:
    return ReviewResult(
        scope=scope,
        style_signal=StyleSignal.INDETERMINATE,
        assessment_confidence=Level.LOW,
        review_priority=Level.LOW,
        summary=(
            "Insufficient text for reliable stylistic assessment; no review was "
            "requested from a provider for this short paragraph."
        ),
        limitations_note=SHORT_STANDALONE_LIMITATIONS,
    )


def _context_chain(
    journal: Journal, element: DocElement, direction: str, count: int
) -> list[ContextParagraph]:
    chain: list[ContextParagraph] = []
    current = element
    for _ in range(count):
        neighbor_id = (
            current.prev_substantial_id if direction == "before" else current.next_substantial_id
        )
        if neighbor_id is None:
            break
        neighbor = journal.element(neighbor_id)
        chain.append(
            ContextParagraph(
                element_id=neighbor.element_id,
                position="before" if direction == "before" else "after",
                text=neighbor.text,
            )
        )
        current = neighbor
    if direction == "before":
        chain.reverse()
    return chain


def build_review_task(journal: Journal, element: DocElement, config: ReviewConfig) -> ReviewTask:
    use_context = config.context_assisted and (
        config.context_before > 0 or config.context_after > 0
    )
    before = (
        _context_chain(journal, element, "before", config.context_before) if use_context else []
    )
    after = _context_chain(journal, element, "after", config.context_after) if use_context else []
    scope = Scope.CONTEXT_WINDOW if (before or after) else Scope.PARAGRAPH
    return ReviewTask(
        element_id=element.element_id,
        display_number=element.display_number,
        location=element.location,
        style_name=element.style_name,
        nearest_heading=element.nearest_heading,
        word_count=element.word_count,
        min_words=config.min_words,
        scope=scope,
        text=element.text,
        context_before=before,
        context_after=after,
    )


def _audit_sampled(element_id: str, audit_percent: int) -> bool:
    """Deterministic audit sampling: stable per element, no randomness."""
    if audit_percent <= 0:
        return False
    bucket = int(hashlib.sha256(element_id.encode("ascii")).hexdigest()[:8], 16) % 100
    return bucket < audit_percent


_SIGNAL_ORDER = {
    StyleSignal.NONE: 0,
    StyleSignal.MILD: 1,
    StyleSignal.MODERATE: 2,
    StyleSignal.STRONG: 3,
}


def classify_agreement(primary: StyleSignal, second: StyleSignal) -> str:
    """agreement | partial_agreement | disagreement — never averaged."""
    if primary is second:
        return "agreement"
    if StyleSignal.INDETERMINATE in (primary, second):
        return "partial_agreement"
    return (
        "partial_agreement"
        if abs(_SIGNAL_ORDER[primary] - _SIGNAL_ORDER[second]) == 1
        else "disagreement"
    )


class JobRunner:
    """Sequential executor for one prepared job (one provider subprocess at a time)."""

    def __init__(
        self,
        prepared: PreparedJob,
        config: ReviewConfig,
        *,
        on_event: EventCallback | None = None,
        should_stop: StopCheck | None = None,
    ) -> None:
        self.journal = prepared.journal
        self.report = prepared.report
        self.config = config
        self._on_event = on_event or (lambda _kind, _payload: None)
        self._should_stop = should_stop or (lambda: False)
        self._reviewed_count = 0
        self._consensus = config.provider_mode is ProviderMode.CONSENSUS
        self._primary: ReviewProvider | None = None
        self._second: ReviewProvider | None = None

    def emit(self, kind: str, **payload: object) -> None:
        self._on_event(kind, payload)

    def run(self) -> JobStatus:
        self._setup_providers()
        assert self._primary is not None

        self.journal.set_status(JobStatus.IN_PROGRESS)
        self.emit("job_started", job_id=self.journal.meta().job_id)

        roles = (
            [TaskRole.PRIMARY, TaskRole.SECOND_OPINION] if self._consensus else [TaskRole.PRIMARY]
        )
        while (item := self.journal.next_pending_any(roles)) is not None:
            element_id, role = item
            if self._should_stop():
                return self._pause(ErrorCategory.INTERRUPTED, "stopped by user")
            element = self.journal.element(element_id)
            status = (
                self._process_primary(element)
                if role is TaskRole.PRIMARY
                else self._process_second_opinion(element)
            )
            if status is not None:
                return status

        self.report.append_section(render_summary(self.journal.meta(), self.journal.tasks()))
        self.journal.set_status(JobStatus.COMPLETED)
        self.emit("job_completed", job_id=self.journal.meta().job_id)
        return JobStatus.COMPLETED

    # -- provider resolution ------------------------------------------------------

    def _setup_providers(self) -> None:
        mode = self.config.provider_mode
        if mode in (ProviderMode.CLAUDE, ProviderMode.CODEX):
            self._primary = self._require_usable(mode.value)
            return
        if mode is ProviderMode.AUTO:
            self._primary = self._resolve_auto()
            return
        # consensus
        primary_name = self.config.primary_provider or "claude"
        second_name = self.config.second_opinion_provider or (
            "codex" if primary_name == "claude" else "claude"
        )
        self._primary = self._require_usable(primary_name)
        self._second = self._optional_provider(second_name)
        if self._second is None and self.config.fallback_provider:
            self._second = self._optional_provider(self.config.fallback_provider)
        if self._second is None:
            self.emit(
                "consensus_degraded",
                message=f"second-opinion provider '{second_name}' unavailable; "
                "consensus results will be reported as single-provider",
            )

    def _require_usable(self, name: str) -> ReviewProvider:
        provider = make_provider(name, self.config)
        status = provider.preflight()
        blocking = status.blocking_category()
        if blocking is not None:
            self.journal.set_status(JobStatus.PAUSED, blocking.value)
            self.emit("job_paused", reason=blocking.value, message=status.message)
            raise IsaiError(blocking, status.message)
        return provider

    def _optional_provider(self, name: str) -> ReviewProvider | None:
        try:
            provider = make_provider(name, self.config)
            return provider if provider.preflight().usable else None
        except (IsaiError, FileNotFoundError):
            return None

    def _resolve_auto(self) -> ReviewProvider:
        messages: list[str] = []
        order = ("claude", "codex")
        if self.config.primary_provider:
            order = (
                self.config.primary_provider,
                *[n for n in ("claude", "codex") if n != self.config.primary_provider],
            )
        for name in order:
            provider = make_provider(name, self.config)
            status = provider.preflight()
            if status.usable:
                return provider
            messages.append(f"{name}: {status.message}")
        summary = (
            "no usable provider for --provider auto — " + " | ".join(messages) + ". "
            "Install and sign in to Claude Code (claude.ai subscription) or "
            "Codex CLI (ChatGPT), then re-run `isai doctor`."
        )
        self.journal.set_status(JobStatus.PAUSED, ErrorCategory.CONFIGURATION.value)
        self.emit("job_paused", reason=ErrorCategory.CONFIGURATION.value, message=summary)
        raise IsaiError(ErrorCategory.CONFIGURATION, summary)

    # -- primary review -------------------------------------------------------------

    def _skip(self, element: DocElement) -> None:
        self.journal.mark_skipped(element.element_id, TaskRole.PRIMARY)
        if self._consensus:
            self.journal.mark_skipped(element.element_id, TaskRole.SECOND_OPINION)

    def _process_primary(self, element: DocElement) -> JobStatus | None:
        """Run the 9-step protocol for one element. Non-None return ends the run."""
        assert self._primary is not None
        role = TaskRole.PRIMARY

        if not _reviewable(element) or not _in_range(element, self.config):
            self._skip(element)
            return None
        if (
            self.config.max_paragraphs is not None
            and self._reviewed_count >= self.config.max_paragraphs
        ):
            self._skip(element)
            return None

        # (1) active + event
        self.journal.mark_active(element.element_id, role, self._primary.name.value)
        self.emit(
            "paragraph_started",
            element_id=element.element_id,
            display_number=element.display_number,
        )

        # (2)-(3) build task, invoke, validate
        task = build_review_task(self.journal, element, self.config)
        if task.scope is Scope.PARAGRAPH and element.word_count < self.config.min_words:
            result = _short_standalone_result(Scope.PARAGRAPH)
            self._commit_success(element, role, LOCAL_PROVIDER, result, [])
            return None

        outcome = self._primary.review(task)

        if outcome.ok and outcome.result is not None:
            # (4) local highlight resolution
            highlights = resolve_highlights(outcome.result, element.text)
            self._commit_success(
                element,
                role,
                self._primary.name.value,
                outcome.result,
                highlights,
                attempts=outcome.attempts,
            )
            return None

        category = outcome.error_category or ErrorCategory.UNKNOWN
        if category in JOB_PAUSING_CATEGORIES:
            self._reset_to_pending(element.element_id, role)
            return self._pause(category, outcome.error_message)

        # Recorded per-paragraph error; the run continues.
        self.journal.record_error(
            element.element_id,
            role,
            provider=self._primary.name.value,
            category=category,
            message=outcome.error_message,
            attempts=outcome.attempts,
        )
        if self._consensus:
            self.journal.mark_skipped(element.element_id, TaskRole.SECOND_OPINION)
        self._append_markdown(element, role)
        self.emit(
            "paragraph_error",
            element_id=element.element_id,
            category=category.value,
        )
        return None

    # -- second opinion ----------------------------------------------------------------

    def _needs_second_opinion(self, element: DocElement) -> bool:
        primary_task = self.journal.task(element.element_id, TaskRole.PRIMARY)
        if primary_task.status is not TaskStatus.COMPLETED or primary_task.result is None:
            return False
        if primary_task.provider == LOCAL_PROVIDER:
            return False
        result = primary_task.result
        return (
            result.style_signal
            in (StyleSignal.MODERATE, StyleSignal.STRONG, StyleSignal.INDETERMINATE)
            or result.needs_second_opinion
            or any(a.status == "invalid" for a in primary_task.attempts)
            or _audit_sampled(element.element_id, self.config.audit_percent)
        )

    def _process_second_opinion(self, element: DocElement) -> JobStatus | None:
        role = TaskRole.SECOND_OPINION
        if not self._needs_second_opinion(element):
            self.journal.mark_skipped(element.element_id, role)
            return None
        if self._second is None:
            self.journal.mark_skipped(element.element_id, role)
            self.journal.set_agreement(element.element_id, TaskRole.PRIMARY, "single_provider")
            return None

        self.journal.mark_active(element.element_id, role, self._second.name.value)
        task = build_review_task(self.journal, element, self.config)
        outcome = self._second.review(task)

        if outcome.ok and outcome.result is not None:
            primary_task = self.journal.task(element.element_id, TaskRole.PRIMARY)
            assert primary_task.result is not None
            agreement = classify_agreement(
                primary_task.result.style_signal, outcome.result.style_signal
            )
            highlights = resolve_highlights(outcome.result, element.text)
            self.journal.record_result(
                element.element_id,
                role,
                provider=self._second.name.value,
                result=outcome.result,
                highlights=highlights,
                attempts=outcome.attempts,
                agreement=agreement,
            )
            self.journal.set_agreement(element.element_id, TaskRole.PRIMARY, agreement)
            self._append_markdown(element, role)
            self.emit(
                "second_opinion_completed",
                element_id=element.element_id,
                display_number=element.display_number,
                agreement=agreement,
                style_signal=outcome.result.style_signal.value,
            )
            return None

        category = outcome.error_category or ErrorCategory.UNKNOWN
        if category in JOB_PAUSING_CATEGORIES:
            self._reset_to_pending(element.element_id, role)
            return self._pause(category, outcome.error_message)

        self.journal.record_error(
            element.element_id,
            role,
            provider=self._second.name.value,
            category=category,
            message=outcome.error_message,
            attempts=outcome.attempts,
        )
        self.journal.set_agreement(element.element_id, TaskRole.PRIMARY, "single_provider")
        self._append_markdown(element, role)
        self.emit("paragraph_error", element_id=element.element_id, category=category.value)
        return None

    # -- shared helpers -------------------------------------------------------------------

    def _reset_to_pending(self, element_id: str, role: TaskRole) -> None:
        with self.journal.transaction() as cur:
            cur.execute(
                "UPDATE task SET status = ? WHERE element_id = ? AND role = ?",
                (TaskStatus.PENDING.value, element_id, role.value),
            )

    def _commit_success(
        self,
        element: DocElement,
        role: TaskRole,
        provider_name: str,
        result: ReviewResult,
        highlights: list[Highlight],
        attempts: list[AttemptRecord] | None = None,
    ) -> None:
        # (5) SQLite commit
        self.journal.record_result(
            element.element_id,
            role,
            provider=provider_name,
            result=result,
            highlights=highlights,
            attempts=attempts or [],
        )
        # (6)-(8) Markdown append + fsync + written flag
        self._append_markdown(element, role)
        self._reviewed_count += 1
        # (9) completion event
        self.emit(
            "primary_review_completed",
            element_id=element.element_id,
            display_number=element.display_number,
            style_signal=result.style_signal.value,
        )

    def _append_markdown(self, element: DocElement, role: TaskRole) -> None:
        task = self.journal.task(element.element_id, role)
        self.report.append_section(render_task_section(element, task))
        self.journal.set_markdown_written(element.element_id, role)

    def _pause(self, category: ErrorCategory, message: str) -> JobStatus:
        self.journal.set_status(JobStatus.PAUSED, category.value)
        self.emit("job_paused", reason=category.value, message=message)
        return JobStatus.PAUSED
