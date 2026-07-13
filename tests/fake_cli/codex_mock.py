"""Mock of the `codex` CLI surface that the Codex adapter depends on."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.fake_cli import common

VERSION_LINE = "codex-cli 0.141.0"

_EXEC_HELP_FULL = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Options:
  -s, --sandbox <SANDBOX_MODE>       [values: read-only, workspace-write, danger-full-access]
      --ephemeral                    Run without persisting session files to disk
      --ignore-user-config           Do not load `$CODEX_HOME/config.toml`
      --ignore-rules                 Do not load execpolicy `.rules` files
      --skip-git-repo-check          Allow running Codex outside a Git repository
      --output-schema <FILE>         Path to a JSON Schema file for the final response
      --json                         Print events to stdout as JSONL
  -o, --output-last-message <FILE>   File for the last agent message
  -C, --cd <DIR>                     Working root
  -m, --model <MODEL>                Model the agent should use
  -h, --help                         Print help
"""

_EXEC_HELP_MISSING = _EXEC_HELP_FULL.replace(
    "      --output-schema <FILE>         Path to a JSON Schema file for the final response\n",
    "",
)


def _login_status() -> int:
    match common.scenario():
        case "auth_api_billed":
            print("Logged in using an API key - ...")
            return 0
        case "auth_missing":
            print("Not logged in")
            return 1
        case _:
            print("Logged in using ChatGPT")
            return 0


def _output_file(argv: list[str]) -> Path | None:
    for flag in ("--output-last-message", "-o"):
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                return Path(argv[idx + 1])
    return None


def _exec(argv: list[str]) -> int:
    stdin_data = common.read_stdin_bytes()
    out_path = _output_file(argv)
    exit_code = 0
    try:
        exit_code = _exec_scenarios(argv, stdin_data, out_path)
    finally:
        common.log_invocation(
            "codex", argv, stdin_data, exit_code, str(out_path) if out_path else None
        )
    return exit_code


def _exec_scenarios(argv: list[str], stdin_data: bytes, out_path: Path | None) -> int:
    scen = common.scenario()

    if scen == "unsupported_flag" and "--output-schema" in argv:
        sys.stderr.write("error: unexpected argument '--output-schema' found\n")
        return 2
    if scen == "auth_missing":
        sys.stderr.write("Not logged in. Run `codex login`.\n")
        return 1
    if scen == "usage_limit":
        sys.stderr.write("You've hit your usage limit. Upgrade to Pro or try again at 3pm.\n")
        return 1
    if scen == "rate_limit":
        sys.stderr.write("stream error: 429 Too Many Requests; retrying soon\n")
        return 1
    if scen == "timeout":
        common.sleep_hang()
        return 1
    if scen == "spawn_child":
        pid = common.spawn_grandchild()
        sys.stderr.write(f"MOCK_CHILD_PID={pid}\n")
        sys.stderr.flush()
        common.sleep_hang()
        return 1
    if scen == "delayed_completion":
        common.sleep_delay()

    prompt = stdin_data.decode("utf-8")
    task = common.parse_task_from_prompt(prompt)
    attempt = common.invocation_count(task["target"]["element_id"])

    if scen == "malformed_json" or (scen == "malformed_then_success" and attempt == 1):
        payload = "definitely not json {"
    elif scen == "schema_violation" or (scen == "schema_violation_then_success" and attempt == 1):
        payload = json.dumps(common.build_schema_violation_result(task), ensure_ascii=False)
    else:
        payload = json.dumps(common.build_success_result(task), ensure_ascii=False)

    if out_path is not None:
        out_path.write_text(payload, encoding="utf-8")
    if "--json" in argv:
        sys.stdout.write(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}) + "\n"
        )
    sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv or "-V" in argv:
        print(VERSION_LINE)
        return 0
    if argv[:2] == ["login", "status"]:
        return _login_status()
    if argv[:1] == ["exec"]:
        if "--help" in argv or "-h" in argv:
            scen = common.scenario()
            sys.stdout.write(_EXEC_HELP_MISSING if scen == "unsupported_flag" else _EXEC_HELP_FULL)
            return 0
        return _exec(argv)
    if "--help" in argv or "-h" in argv:
        sys.stdout.write("Codex CLI\n\nCommands:\n  exec  Run Codex non-interactively\n")
        return 0
    sys.stderr.write("fake codex: unrecognized invocation\n")
    return 2
