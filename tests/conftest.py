"""Shared fixtures: mock-CLI wiring and review tasks."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from isai.models import Scope
from isai.prompting import ReviewTask
from isai.providers.base import ProviderSettings

REPO_ROOT = Path(__file__).resolve().parents[1]


def mock_prefix(tool: str) -> list[str]:
    return [sys.executable, "-m", "tests.fake_cli", tool]


@pytest.fixture()
def mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Configure mock-CLI env vars; returns the log/state paths."""
    log_path = tmp_path / "mock-log.jsonl"
    state_dir = tmp_path / "mock-state"
    state_dir.mkdir()
    monkeypatch.setenv("MOCK_LLM_LOG", str(log_path))
    monkeypatch.setenv("MOCK_LLM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "success")
    # The mock modules import `tests.fake_cli`; the child must find the repo root.
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    return {"log": log_path, "state": state_dir}


SetScenario = Callable[[str], None]


@pytest.fixture()
def scenario(monkeypatch: pytest.MonkeyPatch) -> SetScenario:
    def set_scenario(name: str) -> None:
        monkeypatch.setenv("MOCK_LLM_SCENARIO", name)

    return set_scenario


def make_task(
    text: str | None = None,
    *,
    element_id: str = "p-000001-abcdef12",
    min_words: int = 5,
    scope: Scope = Scope.PARAGRAPH,
) -> ReviewTask:
    if text is None:
        text = (
            "Moreover, it is important to note that the retrospective cohort included "
            "412 patients treated between 2015 and 2019 across three tertiary centers, "
            "and outcomes were assessed with standard survival methods."
        )
    return ReviewTask(
        element_id=element_id,
        display_number=1,
        location="body",
        style_name="Normal",
        nearest_heading="Methods",
        word_count=len(text.split()),
        min_words=min_words,
        scope=scope,
        text=text,
    )


def claude_settings(**overrides: object) -> ProviderSettings:
    defaults: dict[str, object] = {
        "command_prefix": mock_prefix("claude"),
        "timeout_seconds": 60,
    }
    defaults.update(overrides)
    return ProviderSettings.model_validate(defaults)


def codex_settings(**overrides: object) -> ProviderSettings:
    defaults: dict[str, object] = {
        "command_prefix": mock_prefix("codex"),
        "timeout_seconds": 60,
    }
    defaults.update(overrides)
    return ProviderSettings.model_validate(defaults)


@pytest.fixture()
def no_billing_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
