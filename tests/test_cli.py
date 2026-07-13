"""CLI surface tests (in-process) + real-entrypoint end-to-end runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_thesis
from typer.testing import CliRunner

from isai import __version__
from isai.cli import app
from tests.conftest import REPO_ROOT, SetScenario, mock_prefix

runner = CliRunner()

pytestmark = pytest.mark.usefixtures("mock_env", "no_billing_env")


@pytest.fixture(autouse=True)
def mock_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ISAI_CLAUDE_COMMAND", json.dumps(mock_prefix("claude")))
    monkeypatch.setenv("ISAI_CODEX_COMMAND", json.dumps(mock_prefix("codex")))


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_review_end_to_end(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=3)
    report = tmp_path / "out.md"
    result = runner.invoke(
        app,
        ["review", str(docx), "--output", str(report), "--min-words", "10"],
    )
    assert result.exit_code == 0, result.output
    assert report.is_file()
    assert report.with_suffix(".sqlite3").is_file()
    assert "review complete" in result.output
    assert "signal=" in result.output


def test_review_rejects_non_docx(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-doc.txt"
    bogus.write_text("hello", encoding="utf-8")
    result = runner.invoke(app, ["review", str(bogus), "--output", str(tmp_path / "o.md")])
    assert result.exit_code == 3
    assert "document" in result.output


def test_review_billing_mode_exit_code(tmp_path: Path, scenario: SetScenario) -> None:
    scenario("auth_api_billed")
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=2)
    result = runner.invoke(
        app, ["review", str(docx), "--output", str(tmp_path / "o.md"), "--min-words", "10"]
    )
    assert result.exit_code == 6
    assert "billing" in result.output.lower()


def test_review_billing_override_flag(tmp_path: Path, scenario: SetScenario) -> None:
    scenario("auth_api_billed")
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=2)
    result = runner.invoke(
        app,
        [
            "review",
            str(docx),
            "--output",
            str(tmp_path / "o.md"),
            "--min-words",
            "10",
            "--allow-api-billed-auth",
        ],
    )
    assert result.exit_code == 0, result.output


def test_debug_flag_warns(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=1)
    result = runner.invoke(
        app,
        ["review", str(docx), "--output", str(tmp_path / "o.md"), "--min-words", "10", "--debug"],
    )
    assert "--debug output may include document text" in result.output


def test_rebuild_command(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=2)
    report = tmp_path / "out.md"
    assert (
        runner.invoke(
            app, ["review", str(docx), "--output", str(report), "--min-words", "10"]
        ).exit_code
        == 0
    )
    rebuilt = tmp_path / "rebuilt.md"
    result = runner.invoke(
        app, ["rebuild", str(report.with_suffix(".sqlite3")), "--output", str(rebuilt)]
    )
    assert result.exit_code == 0
    assert rebuilt.is_file()
    assert "isai:result" in rebuilt.read_text(encoding="utf-8")


def test_doctor_runs_without_model_calls(mock_env: dict[str, Path]) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "provider: claude" in result.output
    assert "subscription auth verified" in result.output
    assert not mock_env["log"].exists() or "--print" not in mock_env["log"].read_text(
        encoding="utf-8"
    ), "doctor must not invoke a review"


def test_gui_rejects_non_loopback_host() -> None:
    result = runner.invoke(app, ["gui", "--host", "0.0.0.0"])
    assert result.exit_code == 4
    assert "127.0.0.1" in result.output


def test_no_text_in_cli_output(tmp_path: Path) -> None:
    """Normal CLI output (no --debug) must never echo document text."""
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=2)
    result = runner.invoke(
        app,
        ["review", str(docx), "--output", str(tmp_path / "o.md"), "--min-words", "10", "--verbose"],
    )
    assert result.exit_code == 0
    assert "retrospective cohort" not in result.output
    assert "Moreover, it is important" not in result.output


# -- real entrypoint end-to-end (marked e2e) ----------------------------------------


def entrypoint_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "ISAI_CLAUDE_COMMAND": json.dumps(mock_prefix("claude")),
            "ISAI_CODEX_COMMAND": json.dumps(mock_prefix("codex")),
            "MOCK_LLM_SCENARIO": "success",
            "MOCK_LLM_STATE_DIR": str(tmp_path / "state"),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    (tmp_path / "state").mkdir(exist_ok=True)
    return env


@pytest.mark.e2e
def test_python_m_isai_end_to_end(tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "עבודה sample.docx", paragraphs=3)  # spaces + Unicode
    report = tmp_path / "מסקנות report.md"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "isai",
            "review",
            str(docx),
            "--output",
            str(report),
            "--min-words",
            "10",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=entrypoint_env(tmp_path),
        cwd=str(REPO_ROOT),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "## Run summary" in content


@pytest.mark.e2e
@pytest.mark.timeout(300)
def test_kill_mid_run_then_resume(tmp_path: Path) -> None:
    """Real crash: the CLI process is killed mid-run; resume finishes cleanly."""
    docx = build_thesis(tmp_path / "thesis.docx", paragraphs=5)
    report = tmp_path / "out.md"
    env = entrypoint_env(tmp_path)
    env["MOCK_LLM_SCENARIO"] = "delayed_completion"
    env["MOCK_LLM_DELAY_SECONDS"] = "1.0"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "isai",
            "review",
            str(docx),
            "--output",
            str(report),
            "--min-words",
            "10",
        ],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    # Wait until at least one paragraph landed in the report, then kill hard.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if report.is_file() and "isai:result" in report.read_text(
            encoding="utf-8", errors="replace"
        ):
            break
        time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("no paragraph completed before the deadline")
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
    )
    proc.wait(timeout=30)

    # The report is readable and structurally sound right now.
    crashed_content = report.read_text(encoding="utf-8", errors="replace")
    assert "cannot determine authorship" in crashed_content
    crashed_markers = crashed_content.count("isai:result")
    assert crashed_markers >= 1

    # Resume with the same command; scenario switches to instant success.
    env["MOCK_LLM_SCENARIO"] = "success"
    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "isai",
            "review",
            str(docx),
            "--output",
            str(report),
            "--min-words",
            "10",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(REPO_ROOT),
        timeout=300,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert "resuming job" in proc2.stdout
    final = report.read_text(encoding="utf-8")
    markers = [line for line in final.splitlines() if line.startswith("[//]: # (isai:result")]
    assert len(markers) == len(set(markers)), "resume duplicated a section"
    assert "## Run summary" in final
