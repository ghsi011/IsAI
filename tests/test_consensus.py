"""Auto and consensus provider modes (§7): immediate primary, triggers, agreement."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_thesis

from isai.config import ReviewConfig
from isai.errors import IsaiError
from isai.models import StyleSignal
from isai.persistence import Journal, TaskRole, TaskStatus
from isai.persistence.db import JobStatus
from isai.pipeline import JobRunner, classify_agreement, prepare_job
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


def run_job(docx: Path, report: Path, config: ReviewConfig) -> tuple[JobStatus, Events]:
    events: Events = []
    prepared = prepare_job(docx, report, config)
    try:
        status = JobRunner(prepared, config, on_event=lambda k, p: events.append((k, p))).run()
    finally:
        prepared.journal.close()
    return status, events


# -- agreement classification (pure) ----------------------------------------------


def test_agreement_classification_matrix() -> None:
    s = StyleSignal
    assert classify_agreement(s.MILD, s.MILD) == "agreement"
    assert classify_agreement(s.MODERATE, s.STRONG) == "partial_agreement"
    assert classify_agreement(s.NONE, s.MILD) == "partial_agreement"
    assert classify_agreement(s.NONE, s.STRONG) == "disagreement"
    assert classify_agreement(s.MILD, s.STRONG) == "disagreement"
    assert classify_agreement(s.INDETERMINATE, s.STRONG) == "partial_agreement"
    assert classify_agreement(s.INDETERMINATE, s.INDETERMINATE) == "agreement"


# -- auto mode -----------------------------------------------------------------------


def test_auto_prefers_claude_when_usable(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    config = make_config(provider_mode="auto")
    status, _ = run_job(docx, tmp_path / "r.md", config)
    assert status is JobStatus.COMPLETED
    journal = Journal.open(tmp_path / "r.sqlite3")
    completed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    assert all(t.provider == "claude" for t in completed if t.provider != "local")
    journal.close()


def test_auto_falls_back_to_codex(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    # Break the claude prefix so its preflight fails; codex stays healthy.
    config = make_config(provider_mode="auto", claude_command=["definitely-not-a-real-exe-1b2c3"])
    status, _ = run_job(docx, tmp_path / "r.md", config)
    assert status is JobStatus.COMPLETED
    journal = Journal.open(tmp_path / "r.sqlite3")
    completed = [t for t in journal.tasks() if t.status is TaskStatus.COMPLETED]
    providers = {t.provider for t in completed} - {"local"}
    assert providers == {"codex"}
    journal.close()


def test_auto_with_no_usable_provider_errors_actionably(
    tmp_path: Path, scenario: SetScenario
) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    scenario("auth_missing")  # both mocks report not-logged-in
    config = make_config(provider_mode="auto")
    prepared = prepare_job(docx, tmp_path / "r.md", config)
    with pytest.raises(IsaiError) as exc_info:
        JobRunner(prepared, config).run()
    prepared.journal.close()
    assert "claude" in exc_info.value.message
    assert "codex" in exc_info.value.message
    assert "doctor" in exc_info.value.message


# -- consensus -------------------------------------------------------------------------


def consensus_config(**overrides: object) -> ReviewConfig:
    defaults: dict[str, object] = {
        "provider_mode": "consensus",
        "audit_percent": 100,  # deterministic: every paragraph gets a second opinion
    }
    defaults.update(overrides)
    return make_config(**defaults)


def test_consensus_primary_streams_before_second_opinion(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    status, events = run_job(docx, tmp_path / "r.md", consensus_config())
    assert status is JobStatus.COMPLETED
    kinds = [k for k, _ in events]
    first_primary = kinds.index("primary_review_completed")
    first_second = kinds.index("second_opinion_completed")
    assert first_primary < first_second, "primary must stream immediately, never delayed"


def test_consensus_second_opinion_recorded_separately(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    status, _ = run_job(docx, tmp_path / "r.md", consensus_config())
    assert status is JobStatus.COMPLETED
    journal = Journal.open(tmp_path / "r.sqlite3")
    seconds = [
        t for t in journal.tasks(TaskRole.SECOND_OPINION) if t.status is TaskStatus.COMPLETED
    ]
    assert seconds, "audit_percent=100 must produce second opinions"
    for task in seconds:
        assert task.provider == "codex"
        assert task.result is not None, "both structured results preserved"
        assert task.agreement in ("agreement", "partial_agreement", "disagreement")
        primary = journal.task(task.element_id, TaskRole.PRIMARY)
        assert primary.result is not None
        assert primary.agreement == task.agreement
        # Never averaged: primary result unchanged by the second opinion.
        assert primary.provider == "claude"
    journal.close()


def test_consensus_report_contains_both_sections(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    run_job(docx, tmp_path / "r.md", consensus_config())
    content = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "## Second opinion" in content
    assert "role=second_opinion" in content
    assert "*Consensus:*" in content


def test_consensus_trigger_off_for_calm_results(tmp_path: Path) -> None:
    """audit_percent=0 and mild signals → second opinions all skipped."""
    docx = build_thesis(tmp_path / "t.docx", paragraphs=3)
    status, _ = run_job(docx, tmp_path / "r.md", consensus_config(audit_percent=0))
    assert status is JobStatus.COMPLETED
    journal = Journal.open(tmp_path / "r.sqlite3")
    seconds = journal.tasks(TaskRole.SECOND_OPINION)
    # Mock returns "mild" for long paragraphs → no triggers fire.
    reviewed = [t for t in seconds if t.status is TaskStatus.COMPLETED]
    assert reviewed == []
    assert all(t.status is TaskStatus.SKIPPED for t in seconds)
    journal.close()


def test_consensus_single_provider_when_second_unavailable(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    config = consensus_config(codex_command=["definitely-not-a-real-exe-1b2c3"])
    status, events = run_job(docx, tmp_path / "r.md", config)
    assert status is JobStatus.COMPLETED
    assert any(k == "consensus_degraded" for k, _ in events)
    journal = Journal.open(tmp_path / "r.sqlite3")
    primaries = [t for t in journal.tasks(TaskRole.PRIMARY) if t.status is TaskStatus.COMPLETED]
    provider_reviewed = [t for t in primaries if t.provider == "claude"]
    assert provider_reviewed
    assert all(t.agreement == "single_provider" for t in provider_reviewed)
    journal.close()


def test_consensus_usage_limit_on_second_pauses_and_resumes(
    tmp_path: Path, scenario: SetScenario
) -> None:
    """Usage exhaustion during a second opinion pauses cleanly; resume finishes."""
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    report = tmp_path / "r.md"
    config = consensus_config()

    # The shared MOCK_LLM_SCENARIO controls both mocks, so flip it to
    # usage_limit right after the first primary completes — the very next
    # provider call is the second opinion, which then hits the limit.
    events: Events = []
    prepared = prepare_job(docx, report, config)

    def flip(kind: str, payload: dict[str, object]) -> None:
        events.append((kind, payload))
        if kind == "primary_review_completed":
            scenario("usage_limit")
        if kind == "second_opinion_completed":  # pragma: no cover - shouldn't happen
            scenario("success")

    status = JobRunner(prepared, config, on_event=flip).run()
    prepared.journal.close()
    assert status is JobStatus.PAUSED

    scenario("success")
    prepared = prepare_job(docx, report, config)
    assert prepared.resumed
    status = JobRunner(prepared, config).run()
    prepared.journal.close()
    assert status is JobStatus.COMPLETED
    content = report.read_text(encoding="utf-8")
    markers = [ln for ln in content.splitlines() if ln.startswith("[//]: # (isai:result")]
    assert len(markers) == len(set(markers)), "no duplicated sections after resume"


def test_consensus_primary_can_be_codex(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "t.docx", paragraphs=2)
    config = consensus_config(primary_provider="codex", second_opinion_provider="claude")
    status, _ = run_job(docx, tmp_path / "r.md", config)
    assert status is JobStatus.COMPLETED
    journal = Journal.open(tmp_path / "r.sqlite3")
    primaries = {
        t.provider
        for t in journal.tasks(TaskRole.PRIMARY)
        if t.status is TaskStatus.COMPLETED and t.provider != "local"
    }
    seconds = {
        t.provider
        for t in journal.tasks(TaskRole.SECOND_OPINION)
        if t.status is TaskStatus.COMPLETED
    }
    assert primaries == {"codex"}
    assert seconds == {"claude"}
    journal.close()
