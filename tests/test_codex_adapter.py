"""Codex adapter against the executable mock CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from isai.errors import ErrorCategory
from isai.providers.base import AuthState
from isai.providers.codex import CodexAdapter
from tests.conftest import SetScenario, codex_settings, make_task

pytestmark = pytest.mark.usefixtures("mock_env", "no_billing_env")


def test_preflight_chatgpt_subscription(scenario: SetScenario) -> None:
    scenario("auth_subscription")
    status = CodexAdapter(codex_settings()).preflight()
    assert status.installed and status.capabilities_ok
    assert status.version == "0.141.0"
    assert status.auth_state is AuthState.SUBSCRIPTION
    assert status.usable


def test_preflight_api_key_rejected(scenario: SetScenario) -> None:
    scenario("auth_api_billed")
    status = CodexAdapter(codex_settings()).preflight()
    assert status.auth_state is AuthState.API_BILLED
    assert not status.usable
    assert status.blocking_category() is ErrorCategory.BILLING_MODE


def test_preflight_not_logged_in(scenario: SetScenario) -> None:
    scenario("auth_missing")
    status = CodexAdapter(codex_settings()).preflight()
    assert status.auth_state is AuthState.MISSING


def test_preflight_missing_output_schema_flag(scenario: SetScenario) -> None:
    scenario("unsupported_flag")
    status = CodexAdapter(codex_settings()).preflight()
    assert not status.capabilities_ok
    assert "--output-schema" in status.missing_capabilities


def test_review_success_via_output_file(mock_env: dict[str, Path]) -> None:
    task = make_task()
    outcome = CodexAdapter(codex_settings()).review(task)
    assert outcome.ok, outcome.error_message
    assert outcome.result is not None
    assert outcome.result.indicators[0].evidence in task.text


def test_review_argv_isolation_flags(mock_env: dict[str, Path]) -> None:
    CodexAdapter(codex_settings()).review(make_task())
    record = json.loads(mock_env["log"].read_text(encoding="utf-8").splitlines()[0])
    argv = record["argv"]
    assert argv[0] == "exec"
    sandbox_value = argv[argv.index("--sandbox") + 1]
    assert sandbox_value == "read-only"
    for flag in (
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--output-schema",
        "--output-last-message",
    ):
        assert flag in argv
    assert "-" in argv, "prompt must be read from stdin"
    assert "--yolo" not in argv
    assert "danger-full-access" not in argv


def test_document_text_never_in_codex_argv(mock_env: dict[str, Path]) -> None:
    task = make_task()
    CodexAdapter(codex_settings()).review(task)
    record = json.loads(mock_env["log"].read_text(encoding="utf-8").splitlines()[0])
    assert task.text[:30] not in " ".join(record["argv"])
    assert record["stdin_bytes"] > len(task.text)


def test_usage_limit_classified(scenario: SetScenario) -> None:
    scenario("usage_limit")
    outcome = CodexAdapter(codex_settings()).review(make_task())
    assert outcome.error_category is ErrorCategory.USAGE_LIMIT


def test_malformed_then_success_repairs(scenario: SetScenario, mock_env: dict[str, Path]) -> None:
    scenario("malformed_then_success")
    outcome = CodexAdapter(codex_settings()).review(make_task())
    assert outcome.ok
    assert [a.status for a in outcome.attempts] == ["invalid", "ok"]


def test_schema_violation_rejected(scenario: SetScenario) -> None:
    scenario("schema_violation")
    outcome = CodexAdapter(codex_settings()).review(make_task())
    assert not outcome.ok
    assert outcome.error_category is ErrorCategory.VALIDATION


@pytest.mark.timeout(90)
def test_timeout_tree_kill(scenario: SetScenario, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario("timeout")
    monkeypatch.setenv("MOCK_LLM_HANG_SECONDS", "120")
    outcome = CodexAdapter(codex_settings(timeout_seconds=3)).review(make_task())
    assert outcome.error_category is ErrorCategory.TIMEOUT
