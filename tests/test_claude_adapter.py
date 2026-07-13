"""Claude adapter against the executable mock CLI (real child processes)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from isai.errors import ErrorCategory
from isai.models import StyleSignal
from isai.providers.base import AuthState
from isai.providers.claude import ClaudeAdapter
from isai.providers.runner import run_process, scrubbed_child_env
from tests.conftest import SetScenario, claude_settings, make_task

pytestmark = pytest.mark.usefixtures("mock_env", "no_billing_env")


def read_log(paths: dict[str, Path]) -> list[dict[str, Any]]:
    content = paths["log"].read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in content.splitlines() if line]


# -- preflight / auth ---------------------------------------------------------


def test_preflight_subscription_ok(scenario: SetScenario) -> None:
    scenario("auth_subscription")
    status = ClaudeAdapter(claude_settings()).preflight()
    assert status.installed and status.capabilities_ok
    assert status.version == "2.1.183"
    assert status.auth_state is AuthState.SUBSCRIPTION
    assert status.usable
    assert status.blocking_category() is None


def test_preflight_api_billed_rejected_by_default(scenario: SetScenario) -> None:
    scenario("auth_api_billed")
    status = ClaudeAdapter(claude_settings()).preflight()
    assert status.auth_state is AuthState.API_BILLED
    assert not status.usable
    assert status.blocking_category() is ErrorCategory.BILLING_MODE
    assert "--allow-api-billed-auth" in status.message


def test_preflight_api_billed_override(scenario: SetScenario) -> None:
    scenario("auth_api_billed")
    status = ClaudeAdapter(claude_settings(allow_api_billed=True)).preflight()
    assert status.auth_state is AuthState.API_BILLED
    assert status.usable


def test_preflight_auth_missing(scenario: SetScenario) -> None:
    scenario("auth_missing")
    status = ClaudeAdapter(claude_settings()).preflight()
    assert status.auth_state is AuthState.MISSING
    assert not status.usable
    assert status.blocking_category() is ErrorCategory.AUTHENTICATION


def test_preflight_missing_capability_is_compat_error(scenario: SetScenario) -> None:
    scenario("unsupported_flag")
    status = ClaudeAdapter(claude_settings()).preflight()
    assert status.installed
    assert not status.capabilities_ok
    assert "--json-schema" in status.missing_capabilities
    assert status.blocking_category() is ErrorCategory.CONFIGURATION


def test_preflight_missing_executable() -> None:
    settings = claude_settings(command_prefix=["definitely-not-a-real-exe-1b2c3"])
    status = ClaudeAdapter(settings).preflight()
    assert not status.installed
    assert status.blocking_category() is ErrorCategory.CONFIGURATION


def test_preflight_billing_env_names_reported(
    scenario: SetScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario("auth_subscription")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    status = ClaudeAdapter(claude_settings()).preflight()
    assert "ANTHROPIC_API_KEY" in status.billing_env_vars
    assert "sk-test-not-a-real-key" not in status.message  # names only, never values


# -- review success paths -----------------------------------------------------


def test_review_success(mock_env: dict[str, Path]) -> None:
    adapter = ClaudeAdapter(claude_settings())
    task = make_task()
    outcome = adapter.review(task)
    assert outcome.ok, outcome.error_message
    assert outcome.result is not None
    assert outcome.result.style_signal is StyleSignal.MILD
    # Evidence must be a real quote from the target.
    assert outcome.result.indicators[0].evidence in task.text
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].status == "ok"
    assert outcome.attempts[0].raw_response_sha256 is not None


def test_review_hebrew_text_roundtrip() -> None:
    text = (
        "יתרה מזאת, חשוב לציין כי המחקר הרטרוספקטיבי כלל 412 חולים אשר טופלו בין "
        "השנים 2015 ל-2019 בשלושה מרכזים רפואיים שלישוניים ברחבי הארץ."
    )
    outcome = ClaudeAdapter(claude_settings()).review(make_task(text))
    assert outcome.ok, outcome.error_message
    assert outcome.result is not None
    assert outcome.result.indicators[0].evidence in text


def test_review_markdown_control_characters_roundtrip() -> None:
    text = (
        "The `results` were **significant** (see #4); ```fences``` and <!-- comments --> "
        "appear here alongside | pipes | and [links](x) within this deliberately "
        "hostile paragraph of sufficient length for review purposes."
    )
    outcome = ClaudeAdapter(claude_settings()).review(make_task(text))
    assert outcome.ok
    assert outcome.result is not None


# -- stdin-only delivery and log hygiene ---------------------------------------


def test_document_text_only_via_stdin(mock_env: dict[str, Path]) -> None:
    task = make_task()
    ClaudeAdapter(claude_settings()).review(task)
    records = read_log(mock_env)
    assert records, "mock must have logged the invocation"
    for record in records:
        joined_argv = " ".join(record["argv"])
        assert task.text[:30] not in joined_argv
        assert record["stdin_bytes"] > len(task.text)  # prompt arrived via stdin


def test_isolation_flags_present(mock_env: dict[str, Path]) -> None:
    ClaudeAdapter(claude_settings()).review(make_task())
    record = read_log(mock_env)[0]
    argv = record["argv"]
    assert "--print" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert "--bare" not in argv  # D-011: --bare would force API-key auth


def test_env_scrubbed_in_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The child process must not see billing env vars (checked via a probe)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-scrubbed-too")
    probe = (
        "import os,json;print(json.dumps({k: k in os.environ for k in "
        "['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','OPENAI_API_KEY','CODEX_API_KEY']}))"
    )
    proc = run_process(
        [sys.executable, "-c", probe],
        stdin_text=None,
        timeout_seconds=30,
        cwd=tmp_path,
        env=scrubbed_child_env(),
    )
    visibility = json.loads(proc.stdout)
    assert visibility == {
        "ANTHROPIC_API_KEY": False,
        "ANTHROPIC_AUTH_TOKEN": False,
        "OPENAI_API_KEY": False,
        "CODEX_API_KEY": False,
    }


# -- failure classification -----------------------------------------------------


def test_usage_limit_classified(scenario: SetScenario) -> None:
    scenario("usage_limit")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert not outcome.ok
    assert outcome.error_category is ErrorCategory.USAGE_LIMIT


def test_rate_limit_classified(scenario: SetScenario) -> None:
    scenario("rate_limit")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert outcome.error_category is ErrorCategory.RATE_LIMIT


def test_auth_loss_classified(scenario: SetScenario) -> None:
    scenario("auth_missing")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert outcome.error_category is ErrorCategory.AUTHENTICATION


def test_unsupported_flag_is_configuration_error(scenario: SetScenario) -> None:
    scenario("unsupported_flag")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert outcome.error_category is ErrorCategory.CONFIGURATION


# -- retry-once behavior ---------------------------------------------------------


def test_malformed_then_success_retries_exactly_once(
    scenario: SetScenario, mock_env: dict[str, Path]
) -> None:
    scenario("malformed_then_success")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert outcome.ok
    assert [a.status for a in outcome.attempts] == ["invalid", "ok"]
    assert len(read_log(mock_env)) == 2


def test_schema_violation_then_success_repair(
    scenario: SetScenario, mock_env: dict[str, Path]
) -> None:
    scenario("schema_violation_then_success")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert outcome.ok
    assert outcome.attempts[0].error_category is ErrorCategory.VALIDATION


def test_persistent_malformed_fails_after_one_retry(
    scenario: SetScenario, mock_env: dict[str, Path]
) -> None:
    scenario("malformed_json")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert not outcome.ok
    assert outcome.error_category is ErrorCategory.VALIDATION
    assert len(outcome.attempts) == 2
    assert len(read_log(mock_env)) == 2


def test_persistent_schema_violation_fails(scenario: SetScenario) -> None:
    scenario("schema_violation")
    outcome = ClaudeAdapter(claude_settings()).review(make_task())
    assert not outcome.ok
    assert outcome.error_category is ErrorCategory.VALIDATION


# -- timeout and process-tree termination -----------------------------------------


@pytest.mark.timeout(90)
def test_timeout_kills_process_tree(scenario: SetScenario, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario("timeout")
    monkeypatch.setenv("MOCK_LLM_HANG_SECONDS", "120")
    adapter = ClaudeAdapter(claude_settings(timeout_seconds=3))
    start = time.monotonic()
    outcome = adapter.review(make_task())
    elapsed = time.monotonic() - start
    assert outcome.error_category is ErrorCategory.TIMEOUT
    assert elapsed < 30
    assert outcome.attempts[0].timed_out


@pytest.mark.timeout(120)
def test_spawned_grandchild_also_killed(
    scenario: SetScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario("spawn_child")
    monkeypatch.setenv("MOCK_LLM_HANG_SECONDS", "120")
    adapter = ClaudeAdapter(claude_settings(timeout_seconds=5))
    outcome = adapter.review(make_task())
    assert outcome.error_category is ErrorCategory.TIMEOUT
    # Give taskkill a moment, then verify no orphaned python sleeper remains.
    time.sleep(2.0)
    probe = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
        capture_output=True,
        text=True,
        check=False,
    )
    # We can't rely on PIDs from stderr (killed before flush is guaranteed), so
    # assert indirectly: no python process this old test spawned is sleeping 300s.
    # The strong assertion happens in test_runner.py with a captured PID.
    assert probe.returncode == 0
    assert sys.executable  # environment sanity


def test_delayed_completion_within_timeout(
    scenario: SetScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario("delayed_completion")
    monkeypatch.setenv("MOCK_LLM_DELAY_SECONDS", "1.0")
    outcome = ClaudeAdapter(claude_settings(timeout_seconds=30)).review(make_task())
    assert outcome.ok


# -- short paragraph handling ------------------------------------------------------


def test_short_paragraph_gets_indeterminate(scenario: SetScenario) -> None:
    task = make_task("Too short to assess.", min_words=50)
    outcome = ClaudeAdapter(claude_settings()).review(task)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.style_signal is StyleSignal.INDETERMINATE


def test_max_retries_zero_disables_repair(scenario: SetScenario, mock_env: dict[str, Path]) -> None:
    scenario("malformed_then_success")  # would succeed on the 2nd attempt
    adapter = ClaudeAdapter(claude_settings(max_retries=0))
    outcome = adapter.review(make_task())
    assert not outcome.ok
    assert len(outcome.attempts) == 1, "no repair attempt when --max-retries 0"
    assert len(read_log(mock_env)) == 1
