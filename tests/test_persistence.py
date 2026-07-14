"""Journal + report durability: commit protocol, markers, reconcile inputs, rebuild."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_simple

from isai.docxio import DocElement, extract_document
from isai.errors import ErrorCategory, IsaiError
from isai.highlights import resolve_highlights
from isai.persistence import JobMeta, Journal, ReportWriter, TaskRole, TaskStatus
from isai.persistence.db import JobStatus, utc_now_iso
from isai.persistence.render import (
    md_escape,
    render_header,
    render_summary,
    render_task_section,
)
from tests.helpers import make_result


@pytest.fixture()
def elements(tmp_path: Path) -> list[DocElement]:
    return extract_document(build_simple(tmp_path / "doc.docx")).elements


def make_meta(**overrides: object) -> JobMeta:
    defaults: dict[str, object] = {
        "job_id": "job-test-0001",
        "source_filename": "doc.docx",
        "source_sha256": "ab" * 32,
        "extraction_fingerprint": "ef" * 32,
        "config_fingerprint": "cf" * 32,
        "config_json": "{}",
        "prompt_version": "v1",
        "provider_mode": "claude",
        "created_at": utc_now_iso(),
    }
    defaults.update(overrides)
    return JobMeta.model_validate(defaults)


def make_journal(tmp_path: Path, elements: list[DocElement]) -> Journal:
    return Journal.create(tmp_path / "job.sqlite3", make_meta(), elements, [TaskRole.PRIMARY])


# -- journal basics ------------------------------------------------------------


def test_create_and_reopen_roundtrip(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    journal.close()
    reopened = Journal.open(tmp_path / "job.sqlite3")
    assert reopened.meta().job_id == "job-test-0001"
    assert len(reopened.elements()) == len(elements)
    assert reopened.next_pending() == elements[0].element_id
    reopened.close()


def test_create_refuses_existing(tmp_path: Path, elements: list[DocElement]) -> None:
    make_journal(tmp_path, elements).close()
    with pytest.raises(IsaiError) as exc_info:
        make_journal(tmp_path, elements)
    assert exc_info.value.category is ErrorCategory.DATABASE


def test_result_commit_and_progress(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    element = elements[0]
    result = make_result()
    journal.mark_active(element.element_id, TaskRole.PRIMARY, "claude")
    journal.record_result(
        element.element_id,
        TaskRole.PRIMARY,
        provider="claude",
        result=result,
        highlights=resolve_highlights(result, element.text),
        attempts=[],
    )
    task = journal.task(element.element_id, TaskRole.PRIMARY)
    assert task.status is TaskStatus.COMPLETED
    assert task.result is not None
    assert not task.markdown_written
    journal.set_markdown_written(element.element_id, TaskRole.PRIMARY)
    assert journal.task(element.element_id, TaskRole.PRIMARY).markdown_written
    assert journal.next_pending() == elements[1].element_id
    journal.close()


def test_error_recorded_and_run_continues(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    journal.record_error(
        elements[0].element_id,
        TaskRole.PRIMARY,
        provider="claude",
        category=ErrorCategory.VALIDATION,
        message="provider output failed validation",
        attempts=[],
    )
    task = journal.task(elements[0].element_id, TaskRole.PRIMARY)
    assert task.status is TaskStatus.ERROR
    assert task.error_category == "validation"
    # The next pending task moves on — errors don't block the run.
    assert journal.next_pending() == elements[1].element_id
    journal.close()


def test_active_task_counts_as_incomplete_for_resume(
    tmp_path: Path, elements: list[DocElement]
) -> None:
    journal = make_journal(tmp_path, elements)
    journal.mark_active(elements[0].element_id, TaskRole.PRIMARY, "claude")
    journal.close()
    # Simulated crash: reopen — the active row is the resume point.
    reopened = Journal.open(tmp_path / "job.sqlite3")
    assert reopened.next_pending() == elements[0].element_id
    reopened.close()


def test_pause_and_status(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    journal.set_status(JobStatus.PAUSED, "usage_limit")
    meta = journal.meta()
    assert meta.status is JobStatus.PAUSED
    assert meta.paused_reason == "usage_limit"
    journal.close()


def test_wrong_schema_version_rejected(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    journal._conn.execute("PRAGMA user_version = 99")  # pyright: ignore[reportPrivateUsage]
    journal.close()
    with pytest.raises(IsaiError):
        Journal.open(tmp_path / "job.sqlite3")


# -- report writer ---------------------------------------------------------------


def test_header_written_durably_before_results(tmp_path: Path, elements: list[DocElement]) -> None:
    report = ReportWriter(tmp_path / "report.md")
    meta = make_meta()
    report.create(render_header(meta, len(elements), 10))
    content = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "cannot determine authorship" in content
    assert meta.source_sha256 in content


def test_create_refuses_existing_report(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text("existing", encoding="utf-8")
    with pytest.raises(IsaiError):
        ReportWriter(path).create("# header")


def test_append_and_marker_scan(tmp_path: Path, elements: list[DocElement]) -> None:
    report = ReportWriter(tmp_path / "report.md")
    report.create(render_header(make_meta(), len(elements), 10))
    element = elements[1]
    result = make_result()
    journal = make_journal(tmp_path, elements)
    journal.record_result(
        element.element_id,
        TaskRole.PRIMARY,
        provider="claude",
        result=result,
        highlights=[],
        attempts=[],
    )
    section = render_task_section(element, journal.task(element.element_id, TaskRole.PRIMARY), 1)
    report.append_section(section)
    markers = report.existing_markers()
    assert (element.element_id, "primary") in markers
    journal.close()


def test_concurrent_reader_sees_valid_report_mid_run(
    tmp_path: Path, elements: list[DocElement]
) -> None:
    """A second handle reads complete sections while the writer appends more."""
    report = ReportWriter(tmp_path / "report.md")
    report.create(render_header(make_meta(), len(elements), 10))
    journal = make_journal(tmp_path, elements)
    for element in elements[:3]:
        result = make_result()
        journal.record_result(
            element.element_id,
            TaskRole.PRIMARY,
            provider="claude",
            result=result,
            highlights=[],
            attempts=[],
        )
        report.append_section(
            render_task_section(element, journal.task(element.element_id, TaskRole.PRIMARY), 1)
        )
        # Reader between appends: file is always complete and parseable.
        mid = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert mid.count("[//]: # (isai:result") == elements.index(element) + 1
    journal.close()


def test_hostile_document_text_cannot_break_report_structure(tmp_path: Path) -> None:
    hostile = "# fake heading\n``` fence\n<!-- comment --> | pipe [link](x)"
    escaped = md_escape(hostile)
    assert "\\#" in escaped
    assert "\\`" in escaped
    assert "&lt;" in escaped
    assert "\\|" in escaped
    # A rendered block never opens a real fence or heading.
    for line in escaped.splitlines():
        assert not line.strip().startswith(("```", "# "))


# -- rebuild determinism -----------------------------------------------------------


def rebuild_markdown(journal: Journal) -> str:
    meta = journal.meta()
    elements = {e.element_id: e for e in journal.elements()}
    tasks = journal.tasks()
    parts = [render_header(meta, len(elements), len(elements))]
    parts.extend(
        render_task_section(elements[t.element_id], t, i + 1)
        for i, t in enumerate(
            t for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.ERROR)
        )
    )
    parts.append(render_summary(meta, tasks))
    return "".join(parts)


def test_rebuild_is_deterministic(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    for element in elements[:4]:
        result = make_result()
        journal.record_result(
            element.element_id,
            TaskRole.PRIMARY,
            provider="claude",
            result=result,
            highlights=resolve_highlights(result, element.text),
            attempts=[],
        )
    journal.record_error(
        elements[4].element_id,
        TaskRole.PRIMARY,
        provider="claude",
        category=ErrorCategory.TIMEOUT,
        message="provider invocation timed out",
        attempts=[],
    )
    first = rebuild_markdown(journal)
    second = rebuild_markdown(journal)
    assert first == second
    assert first.count("[//]: # (isai:result") == 5
    journal.close()


def test_task_row_json_roundtrip(tmp_path: Path, elements: list[DocElement]) -> None:
    journal = make_journal(tmp_path, elements)
    element = elements[0]
    result = make_result(
        indicators=[
            {
                "category": "formulaic_transition",
                "evidence": element.text.split()[0],
                "explanation": "x",
            }
        ]
    )
    highlights = resolve_highlights(result, element.text)
    journal.record_result(
        element.element_id,
        TaskRole.PRIMARY,
        provider="claude",
        result=result,
        highlights=highlights,
        attempts=[],
    )
    task = journal.task(element.element_id, TaskRole.PRIMARY)
    assert task.result == result
    assert task.highlights == highlights
    raw = journal._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT result_json FROM task WHERE element_id = ?", (element.element_id,)
    ).fetchone()[0]
    assert json.loads(raw)["schema_version"] == "1.0"
    journal.close()
