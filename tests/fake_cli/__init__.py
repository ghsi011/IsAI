"""Executable mock provider CLIs, run as real Windows child processes.

Usage (always through the injectable command prefix):

    [sys.executable, "-m", "tests.fake_cli", "claude", ...]
    [sys.executable, "-m", "tests.fake_cli", "codex", ...]

Behavior is selected via environment variables:

- ``MOCK_LLM_SCENARIO`` — one of the scenarios in :mod:`tests.fake_cli.common`
  (default ``success``).
- ``MOCK_LLM_LOG`` — path of a JSONL file receiving one safe record per
  invocation (argv, stdin SHA-256 + byte count, scenario, output path, exit
  code — never document text).
- ``MOCK_LLM_STATE_DIR`` — directory for invocation counters used by the
  ``*_then_success`` scenarios.
- ``MOCK_LLM_DELAY_SECONDS`` / ``MOCK_LLM_HANG_SECONDS`` — timings for
  ``delayed_completion`` and ``timeout``.

The mocks are stdlib-only so they start fast and cannot drift with app deps.
"""
