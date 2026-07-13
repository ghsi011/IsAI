"""Subprocess runner: stdin delivery, env scrubbing, tree-kill with captured PID."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from isai.providers.runner import (
    BILLING_ENV_VARS,
    ROUTING_ENV_VARS,
    detect_billing_env_vars,
    isolated_workdir,
    run_process,
    scrubbed_child_env,
)
from tests.conftest import mock_prefix


def test_stdin_bytes_reach_child(tmp_path: Path) -> None:
    echo = "import sys; data = sys.stdin.buffer.read(); sys.stdout.write(str(len(data)))"
    proc = run_process(
        [sys.executable, "-c", echo],
        stdin_text="hello עולם",
        timeout_seconds=30,
        cwd=tmp_path,
    )
    assert proc.exit_code == 0
    assert int(proc.stdout) == len("hello עולם".encode())


def test_child_runs_in_isolated_cwd(tmp_path: Path) -> None:
    probe = "import os; print(os.getcwd())"
    with isolated_workdir() as wd:
        proc = run_process(
            [sys.executable, "-c", probe], stdin_text=None, timeout_seconds=30, cwd=wd
        )
        assert Path(proc.stdout.strip()) == wd
    assert not wd.exists(), "isolated workdir must be cleaned up"


def test_detect_billing_env_vars_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*BILLING_ENV_VARS, *ROUTING_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    assert detect_billing_env_vars() == []
    monkeypatch.setenv("CODEX_API_KEY", "sk-x")
    assert detect_billing_env_vars() == ["CODEX_API_KEY"]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    assert set(detect_billing_env_vars()) == {"CODEX_API_KEY", "ANTHROPIC_BASE_URL"}


def test_scrubbed_env_preserves_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ISAI_HARMLESS_TEST_VAR", "keep-me")
    env = scrubbed_child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env, "rerouting vars must not reach providers"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert env["ISAI_HARMLESS_TEST_VAR"] == "keep-me"


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in out.stdout


@pytest.mark.timeout(90)
def test_tree_kill_terminates_grandchild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock spawns a 300s-sleeping grandchild, prints its PID, then hangs.

    After the runner's timeout handling, both the child and the grandchild must
    be gone — the no-orphaned-processes invariant.
    """
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "spawn_child")
    monkeypatch.setenv("MOCK_LLM_HANG_SECONDS", "300")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    argv = [*mock_prefix("claude"), "--print", "--output-format", "json"]
    proc = run_process(argv, stdin_text="irrelevant", timeout_seconds=6, cwd=tmp_path)
    assert proc.timed_out
    marker = next(
        (line for line in proc.stderr.splitlines() if line.startswith("MOCK_CHILD_PID=")),
        None,
    )
    assert marker is not None, f"grandchild PID not captured; stderr={proc.stderr!r}"
    grandchild_pid = int(marker.split("=", 1)[1])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _pid_alive(grandchild_pid):
        time.sleep(0.5)
    assert not _pid_alive(grandchild_pid), "grandchild survived the tree kill"


def test_missing_executable_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_process(
            ["definitely-not-a-real-exe-1b2c3"],
            stdin_text=None,
            timeout_seconds=5,
            cwd=tmp_path,
        )


def test_nonzero_exit_does_not_raise(tmp_path: Path) -> None:
    proc = run_process(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdin_text=None,
        timeout_seconds=30,
        cwd=tmp_path,
    )
    assert proc.exit_code == 3


def test_no_shell_anywhere() -> None:
    """Static guard: the whole package must never use shell=True or os.system."""
    src = Path(__file__).resolve().parents[1] / "src" / "isai"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "shell=True" in text or "os.system(" in text:
            offenders.append(str(py))
    assert offenders == []


def test_mock_log_contains_no_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock's own log must hold hashes and counts, never prompt text."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MOCK_LLM_LOG", str(log))
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "malformed_json")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    secret_text = "EXTREMELY-IDENTIFIABLE-DOCUMENT-SENTENCE"
    run_process(
        [*mock_prefix("claude"), "--print", "--output-format", "json"],
        stdin_text=secret_text,
        timeout_seconds=30,
        cwd=tmp_path,
    )
    content = log.read_text(encoding="utf-8")
    assert secret_text not in content
    record = json.loads(content.splitlines()[0])
    assert record["stdin_bytes"] == len(secret_text)
