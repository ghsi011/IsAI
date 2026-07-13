"""OPT-IN tests against the real provider CLIs.

Never run in CI. Each test:

- requires an explicit environment variable (``ISAI_RUN_CLAUDE_INTEGRATION=1`` /
  ``ISAI_RUN_CODEX_INTEGRATION=1``),
- uses synthetic text only,
- makes at most one model call,
- consumes your real subscription usage (that's the point — you asked for it).

Run:  uv run pytest tests/test_real_providers.py -m claude_integration
"""

from __future__ import annotations

import os

import pytest

from isai.models import Scope, StyleSignal
from isai.prompting import ReviewTask
from isai.providers.base import ProviderSettings
from isai.providers.claude import ClaudeAdapter
from isai.providers.codex import CodexAdapter

_SYNTHETIC = (
    "Moreover, it is important to note that this entirely synthetic paragraph was "
    "generated for integration testing. Furthermore, these invented findings "
    "underscore the significance of comprehensive evaluation. Additionally, the "
    "fabricated evidence demonstrates the crucial role of systematic verification "
    "in achieving optimal outcomes across all fictional domains studied here."
)


def _task() -> ReviewTask:
    return ReviewTask(
        element_id="p-000000-realtest",
        display_number=1,
        location="body",
        style_name="Normal",
        nearest_heading="Integration test",
        word_count=len(_SYNTHETIC.split()),
        min_words=10,
        scope=Scope.PARAGRAPH,
        text=_SYNTHETIC,
    )


@pytest.mark.claude_integration
@pytest.mark.skipif(
    os.environ.get("ISAI_RUN_CLAUDE_INTEGRATION") != "1",
    reason="set ISAI_RUN_CLAUDE_INTEGRATION=1 to spend Claude subscription usage",
)
@pytest.mark.timeout(600)
def test_real_claude_review() -> None:
    adapter = ClaudeAdapter(ProviderSettings(command_prefix=["claude"], timeout_seconds=300))
    status = adapter.preflight()
    if not status.usable:
        pytest.skip(f"claude not usable: {status.message}")
    outcome = adapter.review(_task())
    assert outcome.ok, f"{outcome.error_category}: {outcome.error_message}"
    assert outcome.result is not None
    assert outcome.result.style_signal in StyleSignal
    for indicator in outcome.result.indicators:
        assert indicator.evidence == "" or indicator.evidence in _SYNTHETIC


@pytest.mark.codex_integration
@pytest.mark.skipif(
    os.environ.get("ISAI_RUN_CODEX_INTEGRATION") != "1",
    reason="set ISAI_RUN_CODEX_INTEGRATION=1 to spend ChatGPT subscription usage",
)
@pytest.mark.timeout(600)
def test_real_codex_review() -> None:
    adapter = CodexAdapter(ProviderSettings(command_prefix=["codex"], timeout_seconds=300))
    status = adapter.preflight()
    if not status.usable:
        pytest.skip(f"codex not usable: {status.message}")
    outcome = adapter.review(_task())
    assert outcome.ok, f"{outcome.error_category}: {outcome.error_message}"
    assert outcome.result is not None
    for indicator in outcome.result.indicators:
        assert indicator.evidence == "" or indicator.evidence in _SYNTHETIC
