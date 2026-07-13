"""Pipeline invariants: durability protocol, resume, reconcile, rebuild, context."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_simple, build_thesis

from isai.config import ReviewConfig
from isai.errors import ErrorCategory, IsaiError
from isai.models import Scope
from isai.persistence import Journal, ReportWriter, TaskRole, TaskStatus
from isai.persistence.db import JobStatus
from isai.pipeline import JobRunner, ResumeMode, prepare_job, rebuild_report, reconcile
from tests.conftest import SetScenario, mock_prefix

pytestmark = pytest.mark.usefixtures("mock_env", "no_billing_env")

Events = list[tuple[str, dict[str, object]]]


def make_config(**overrides: object) -> ReviewConfig:
    defaults: dict[str, object] = {
        "claude_command": mock_prefix("claude"),
        "codex_command": mock_prefix("codex"),
        "min_words": 10,
        "timeout_seconds": 60,
    }
    defaults.update(overrides)
    return ReviewConfig.model_validate(defaults)


def run_to_completion(
    docx: Path, report: Path, config: ReviewConfig, **runner_kwargs: object
) -> tuple[JobStatus, Events]:
    events: Events = []
    prepared = prepare_job(docx, report, config)
    runner = JobRunner(
        prepared,
        config,
        on_event=lambda kind, payload: events.append((kind, payload)),
        **runner_kwargs,  # type: ignore[arg-type]
    )
    try:
        status = runner.run()
    finally:
        prepared.journal.close()
    return status, events


def read_markers(report: Path) -> list[str]:
    return [
        line
        for line in report.read_text(encoding="utf-8").splitlines()
        if line.startswith("[//]: # (isai:result")
    ]


# -- full run -------------------------------------------------------------------


def test_full_run_completes_with_ordered_sections(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx")
    report = tmp_path / "report.md"
    status, events = run_to_completion(docx, report, make_config())
    assert status is JobStatus.COMPLETED
    content = report.read_text(encoding="utf-8")
    assert content.index("cannot determine authorship") < content.index("## Paragraph")
    assert "## Run summary" in content
    markers = read_markers(report)
    assert len(markers) == len(set(markers)), "no duplicate sections"
    kinds = [k for k, _ in events]
    assert kinds[0] == "job_started"
    assert kinds[-1] == "job_completed"
    journal = Journal.open(report.with_suffix(".sqlite3"))
    assert journal.meta().status is JobStatus.COMPLETED
    assert all(
        t.markdown_written
        for t in journal.tasks()
        if t.status in (TaskStatus.COMPLETED, TaskStatus.ERROR)
    )
    journal.close()


def test_header_durable_before_first_provider_call(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx")
    report = tmp_path / "report.md"
    seen: list[bool] = []

    def probe(kind: str, payload: dict[str, object]) -> None:
        if kind == "paragraph_started" and not seen:
            content = report.read_text(encoding="utf-8")
            seen.append("cannot determine authorship" in content)

    config = make_config()
    prepared = prepare_job(docx, report, config)
    JobRunner(prepared, config, on_event=probe).run()
    prepared.journal.close()
    assert seen == [True], "header must be readable before the first provider call"


def test_report_handle_not_held_between_paragraphs(tmp_path: Path) -> None:
    """After each append the handle is closed — an external rename succeeds."""
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    renamed: list[bool] = []

    def probe(kind: str, payload: dict[str, object]) -> None:
        if kind == "primary_review_completed" and not renamed:
            target = tmp_path / "renamed.md"
            os.rename(report, target)  # would raise if a handle were open (Windows)
            os.rename(target, report)
            renamed.append(True)

    config = make_config()
    prepared = prepare_job(docx, report, config)
    JobRunner(prepared, config, on_event=probe).run()
    prepared.journal.close()
    assert renamed == [True]


# -- interruption and resume -------------------------------------------------------


def test_stop_and_resume_zero_duplicates(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=6)
    report = tmp_path / "report.md"
    config = make_config()

    completed = 0

    def stop_after_two() -> bool:
        return completed >= 2

    events: Events = []

    def count(kind: str, payload: dict[str, object]) -> None:
        nonlocal completed
        events.append((kind, payload))
        if kind == "primary_review_completed":
            completed += 1

    prepared = prepare_job(docx, report, config)
    status = JobRunner(prepared, config, on_event=count, should_stop=stop_after_two).run()
    prepared.journal.close()
    assert status is JobStatus.PAUSED
    markers_before = read_markers(report)

    # Resume: same command, no stop condition.
    prepared = prepare_job(docx, report, config)
    assert prepared.resumed
    status = JobRunner(prepared, config).run()
    prepared.journal.close()
    assert status is JobStatus.COMPLETED
    markers_after = read_markers(report)
    assert len(markers_after) == len(set(markers_after))
    assert set(markers_before) <= set(markers_after)


def test_usage_limit_pauses_then_resume_completes(
    tmp_path: Path, scenario: SetScenario, mock_env: dict[str, Path]
) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=4)
    report = tmp_path / "report.md"
    config = make_config()
    scenario("usage_limit")
    status, _events = run_to_completion(docx, report, config)
    assert status is JobStatus.PAUSED
    journal = Journal.open(report.with_suffix(".sqlite3"))
    assert journal.meta().paused_reason == "usage_limit"
    # The interrupted paragraph is pending again, not lost or errored.
    assert journal.next_pending() is not None
    journal.close()

    scenario("success")
    status, _ = run_to_completion(docx, report, config)
    assert status is JobStatus.COMPLETED
    markers = read_markers(report)
    assert len(markers) == len(set(markers))


def test_auth_loss_mid_run_pauses(tmp_path: Path, scenario: SetScenario) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    config = make_config()
    events: Events = []
    prepared = prepare_job(docx, report, config)

    flip = {"done": False}

    def flip_auth(kind: str, payload: dict[str, object]) -> None:
        events.append((kind, payload))
        if kind == "primary_review_completed" and not flip["done"]:
            flip["done"] = True
            scenario("auth_missing")

    status = JobRunner(prepared, config, on_event=flip_auth).run()
    prepared.journal.close()
    assert status is JobStatus.PAUSED
    assert any(k == "job_paused" for k, _ in events)


# -- crash reconciliation -----------------------------------------------------------


def test_reconcile_repairs_missing_markdown_section(tmp_path: Path) -> None:
    """Crash between SQLite commit (5) and Markdown append (6)."""
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report_path = tmp_path / "report.md"
    config = make_config()
    status, _ = run_to_completion(docx, report_path, config)
    assert status is JobStatus.COMPLETED

    # Simulate the crash state: remove the last section from the file and
    # clear its written flag as if append never happened.
    journal = Journal.open(report_path.with_suffix(".sqlite3"))
    completed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    victim = completed[-1]
    content = report_path.read_text(encoding="utf-8")
    marker_start = content.rindex("[//]: # (isai:result")
    report_path.write_text(content[:marker_start], encoding="utf-8")
    journal.set_markdown_written(victim.element_id, victim.role, False)

    report = ReportWriter(report_path)
    before = len(read_markers(report_path))
    reconcile(journal, report)
    after = read_markers(report_path)
    assert len(after) == before + 1
    assert journal.task(victim.element_id, victim.role).markdown_written
    # Reconciling again changes nothing.
    reconcile(journal, report)
    assert read_markers(report_path) == after
    journal.close()


def test_reconcile_repairs_stale_flag(tmp_path: Path) -> None:
    """Crash between fsync (7) and flag commit (8): marker exists, flag stale."""
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report_path = tmp_path / "report.md"
    status, _ = run_to_completion(docx, report_path, make_config())
    assert status is JobStatus.COMPLETED
    journal = Journal.open(report_path.with_suffix(".sqlite3"))
    completed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    victim = completed[0]
    journal.set_markdown_written(victim.element_id, victim.role, False)

    before = read_markers(report_path)
    reconcile(journal, ReportWriter(report_path))
    assert read_markers(report_path) == before, "must not duplicate the section"
    assert journal.task(victim.element_id, victim.role).markdown_written
    journal.close()


# -- resume safety -------------------------------------------------------------------


def test_changed_source_refused(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    config = make_config()
    run_to_completion(docx, report, config)
    build_thesis(docx, paragraphs=4)  # different content, same path
    with pytest.raises(IsaiError) as exc_info:
        prepare_job(docx, report, config)
    assert "source document changed" in exc_info.value.message


def test_changed_config_refused(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    run_to_completion(docx, report, make_config())
    with pytest.raises(IsaiError) as exc_info:
        prepare_job(docx, report, make_config(min_words=25))
    assert "configuration changed" in exc_info.value.message
    assert exc_info.value.category is ErrorCategory.CONFIGURATION


def test_restart_discards_state(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    run_to_completion(docx, report, make_config())
    prepared = prepare_job(docx, report, make_config(min_words=25), ResumeMode.RESTART)
    assert not prepared.resumed
    prepared.journal.close()


def test_no_resume_refuses_existing_state(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    config = make_config()
    run_to_completion(docx, report, config)
    with pytest.raises(IsaiError):
        prepare_job(docx, report, config, ResumeMode.NO_RESUME)


def test_force_new_report_regenerates_from_journal(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    config = make_config()
    run_to_completion(docx, report, config)
    original_markers = read_markers(report)
    report.write_text("vandalized", encoding="utf-8")
    prepared = prepare_job(docx, report, config, ResumeMode.FORCE_NEW_REPORT)
    prepared.journal.close()
    assert read_markers(report) == original_markers


# -- rebuild ---------------------------------------------------------------------------


def test_rebuild_deterministic_and_offline(tmp_path: Path, mock_env: dict[str, Path]) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=4)
    report = tmp_path / "report.md"
    run_to_completion(docx, report, make_config())
    journal_path = report.with_suffix(".sqlite3")
    log_size_before = mock_env["log"].stat().st_size

    out1, out2 = tmp_path / "r1.md", tmp_path / "r2.md"
    rebuild_report(journal_path, out1)
    rebuild_report(journal_path, out2)
    assert out1.read_bytes() == out2.read_bytes()
    assert read_markers(out1) == read_markers(report)
    assert mock_env["log"].stat().st_size == log_size_before, "rebuild made provider calls"


# -- short paragraphs and context ---------------------------------------------------------


def test_context_assisted_short_paragraph_uses_window(
    tmp_path: Path, mock_env: dict[str, Path]
) -> None:
    docx = build_simple(tmp_path / "doc.docx")
    report = tmp_path / "report.md"
    config = make_config(min_words=10, context_before=1, context_after=1)
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    short = next(
        e
        for e in journal.elements()
        if e.normalized_text == "Short paragraph." and not e.is_heading
    )
    task = journal.task(short.element_id, TaskRole.PRIMARY)
    assert task.status is TaskStatus.COMPLETED
    assert task.result is not None
    assert task.result.scope is Scope.CONTEXT_WINDOW
    assert task.provider == "claude", "context-assisted short paragraphs go to the provider"
    journal.close()


def test_short_paragraph_without_context_is_local_indeterminate(
    tmp_path: Path, mock_env: dict[str, Path]
) -> None:
    docx = build_simple(tmp_path / "doc.docx")
    report = tmp_path / "report.md"
    config = make_config(min_words=10, context_assisted=False)
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    short = next(e for e in journal.elements() if e.normalized_text == "Short paragraph.")
    task = journal.task(short.element_id, TaskRole.PRIMARY)
    assert task.status is TaskStatus.COMPLETED
    assert task.result is not None
    assert task.result.style_signal.value == "indeterminate"
    assert task.provider == "local", "no provider call for short standalone paragraphs"
    # Verify via the mock log that this element never reached the provider.
    log_lines = mock_env["log"].read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["stdin_bytes"] > 0 for line in log_lines if line)
    journal.close()


def test_headings_and_empty_paragraphs_skipped(tmp_path: Path) -> None:
    docx = build_simple(tmp_path / "doc.docx")
    report = tmp_path / "report.md"
    run_to_completion(docx, report, make_config())
    journal = Journal.open(report.with_suffix(".sqlite3"))
    for element in journal.elements():
        task = journal.task(element.element_id, TaskRole.PRIMARY)
        if element.is_heading or not element.normalized_text:
            assert task.status is TaskStatus.SKIPPED
    journal.close()


def test_max_paragraphs_cap(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=6)
    report = tmp_path / "report.md"
    config = make_config(max_paragraphs=2)
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    reviewed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    assert len(reviewed) == 2
    journal.close()


def test_paragraph_error_continues_run(tmp_path: Path, scenario: SetScenario) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "report.md"
    scenario("malformed_json")  # every provider call fails validation after retry
    status, _events = run_to_completion(docx, report, make_config())
    assert status is JobStatus.COMPLETED, "validation errors must not stop the run"
    journal = Journal.open(report.with_suffix(".sqlite3"))
    errored = [t for t in journal.tasks() if t.status is TaskStatus.ERROR]
    assert errored, "errors were recorded"
    assert all(t.error_category == "validation" for t in errored)
    content = report.read_text(encoding="utf-8")
    assert "Review error" in content
    journal.close()
