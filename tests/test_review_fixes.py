"""Regression tests for the adversarial-review findings (§12 pass)."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_thesis

from isai.errors import IsaiError
from isai.models import Scope
from isai.persistence import Journal, ReportWriter, TaskRole, TaskStatus
from isai.persistence.db import JobStatus
from isai.pipeline import JobRunner, prepare_job, rebuild_report, reconcile
from isai.validation import IssueCode, validate_result
from tests.helpers import make_result
from tests.test_pipeline import make_config, read_markers, run_to_completion

pytestmark = pytest.mark.usefixtures("mock_env", "no_billing_env")


# -- HIGH: duplicate summary on re-run of a completed job ---------------------------


def test_rerun_of_completed_job_is_a_noop(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    report = tmp_path / "r.md"
    config = make_config()
    status, _ = run_to_completion(docx, report, config)
    assert status is JobStatus.COMPLETED
    content_first = report.read_text(encoding="utf-8")

    # The documented resume gesture: run the identical command again.
    status, _ = run_to_completion(docx, report, config)
    assert status is JobStatus.COMPLETED
    content_second = report.read_text(encoding="utf-8")
    assert content_second == content_first, "re-run must not modify the report"
    assert content_second.count("## Run summary") == 1
    journal = Journal.open(report.with_suffix(".sqlite3"))
    assert journal.meta().status is JobStatus.COMPLETED
    journal.close()


def test_crash_between_summary_and_completed_status(tmp_path: Path) -> None:
    """Summary appended but status commit lost → resume must not duplicate it."""
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    report = tmp_path / "r.md"
    config = make_config()
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    journal.set_status(JobStatus.IN_PROGRESS)  # simulate the lost commit
    journal.close()

    status, _ = run_to_completion(docx, report, config)
    assert status is JobStatus.COMPLETED
    assert report.read_text(encoding="utf-8").count("## Run summary") == 1


# -- MEDIUM: torn append must not leave a trusted-but-truncated section -------------


def test_torn_append_is_repaired_on_reconcile(tmp_path: Path) -> None:
    """The marker is the LAST line of a section; a tear that loses the tail
    loses the marker too, so reconcile re-appends the full section."""
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    report_path = tmp_path / "r.md"
    run_to_completion(docx, report_path, make_config())

    journal = Journal.open(report_path.with_suffix(".sqlite3"))
    completed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    victim = completed[-1]

    # Simulate the tear: cut the file in the MIDDLE of the victim's section
    # body (its marker and everything after are lost); flag still true.
    content = report_path.read_text(encoding="utf-8")
    marker_pos = content.rindex("[//]: # (isai:result")
    section_head = content.rindex("## ", 0, marker_pos)
    tear_at = section_head + (marker_pos - section_head) // 2
    report_path.write_text(content[:tear_at], encoding="utf-8")

    report = ReportWriter(report_path)
    assert (victim.element_id, victim.role.value) not in report.existing_markers()
    reconcile(journal, report)
    markers = read_markers(report_path)
    assert len(markers) == len(set(markers))
    assert (victim.element_id, victim.role.value) in report.existing_markers()
    journal.close()


# -- MEDIUM: --max-paragraphs must cap the job, not each run -------------------------


def test_max_paragraphs_holds_across_resume(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=6)
    report = tmp_path / "r.md"
    config = make_config(max_paragraphs=2)

    completed = 0

    def stop_after_one() -> bool:
        return completed >= 1

    def count(kind: str, payload: dict[str, object]) -> None:
        nonlocal completed
        if kind == "primary_review_completed":
            completed += 1

    prepared = prepare_job(docx, report, config)
    status = JobRunner(prepared, config, on_event=count, should_stop=stop_after_one).run()
    prepared.journal.close()
    assert status is JobStatus.PAUSED

    prepared = prepare_job(docx, report, config)
    JobRunner(prepared, config).run()
    prepared.journal.close()

    journal = Journal.open(report.with_suffix(".sqlite3"))
    reviewed = [t for t in journal.tasks(TaskRole.PRIMARY) if t.status is TaskStatus.COMPLETED]
    assert len(reviewed) == 2, "resume granted extra provider budget"
    journal.close()


# -- resume safety: element tampering and prompt-version change ----------------------


def test_tampered_element_hash_refuses_resume(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    report = tmp_path / "r.md"
    config = make_config()
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    element = journal.elements()[0]
    tampered = element.model_copy(update={"content_sha256": "0" * 64})
    with journal.transaction() as cur:
        cur.execute(
            "UPDATE element SET data_json = ? WHERE element_id = ?",
            (tampered.model_dump_json(), element.element_id),
        )
    journal.close()
    with pytest.raises(IsaiError) as exc_info:
        prepare_job(docx, report, config)
    assert "do not match the journal" in exc_info.value.message


def test_prompt_version_change_refuses_resume(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    report = tmp_path / "r.md"
    config = make_config()
    run_to_completion(docx, report, config)
    journal = Journal.open(report.with_suffix(".sqlite3"))
    with journal.transaction() as cur:
        cur.execute("UPDATE job SET prompt_version = 'v0' WHERE id = 1")
    journal.close()
    with pytest.raises(IsaiError) as exc_info:
        prepare_job(docx, report, config)
    assert "prompt version" in exc_info.value.message


# -- consensus live report matches rebuild byte-for-byte -----------------------------


def test_consensus_live_report_equals_rebuild(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    report = tmp_path / "r.md"
    config = make_config(provider_mode="consensus", audit_percent=100)
    status, _ = run_to_completion(docx, report, config)
    assert status is JobStatus.COMPLETED
    rebuilt = tmp_path / "rebuilt.md"
    rebuild_report(report.with_suffix(".sqlite3"), rebuilt)
    assert rebuilt.read_text(encoding="utf-8") == report.read_text(encoding="utf-8")


# -- validation: previously slipping strings now rejected -----------------------------

PARA = "The retrospective cohort included 412 patients treated between 2015 and 2019."


def _codes(summary: str) -> set[IssueCode]:
    result = make_result(summary=summary)
    return {
        i.code
        for i in validate_result(
            result, target_text=PARA, requested_scope=Scope.PARAGRAPH, min_words=5
        )
    }


@pytest.mark.parametrize(
    "claim",
    [
        "It appears that ChatGPT wrote this paragraph.",
        "In my judgment this is likely AI-generated.",
        "An LLM likely drafted this passage.",
        "Probability of AI authorship: 0.9.",
        "There is a ninety percent chance this came from ChatGPT.",
        "This wasn't written by a person.",
        "הטקסט נכתב על ידי בינה מלאכותית.",
    ],
)
def test_authorship_claims_previously_slipping(claim: str) -> None:
    assert IssueCode.AUTHORSHIP_CLAIM in _codes(claim)


@pytest.mark.parametrize(
    "advice",
    [
        "Reword it so detectors won't flag it.",
        "Vary sentence length to lower the AI score.",
        "Humanize the text with colloquialisms.",
        "Throw in a couple of typos for authenticity.",
        "This phrasing helps the text slip past GPTZero.",
    ],
)
def test_evasion_advice_previously_slipping(advice: str) -> None:
    result = make_result(manual_checks=[advice])
    issues = validate_result(result, target_text=PARA, requested_scope=Scope.PARAGRAPH, min_words=5)
    assert IssueCode.EVASION_ADVICE in {i.code for i in issues}, advice


def test_permitted_register_still_passes_strengthened_patterns() -> None:
    ok = (
        "The paragraph contains strong AI-associated stylistic patterns, including "
        "unusually uniform constructions. Manual review recommended; authorship "
        "cannot be determined from style alone. Insufficient text for reliable "
        "stylistic assessment."
    )
    assert _codes(ok) == set()
