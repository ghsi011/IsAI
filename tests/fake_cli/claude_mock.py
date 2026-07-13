"""Mock of the `claude` CLI surface that the Claude adapter depends on."""

from __future__ import annotations

import json
import sys

from tests.fake_cli import common

VERSION_LINE = "2.1.183 (Claude Code)"

_HELP_FULL = """Usage: claude [options] [command] [prompt]

Claude Code - starts an interactive session by default, use -p/--print for
non-interactive output

Options:
  -p, --print                           Print response and exit
  --output-format <format>             Output format: "text", "json", "stream-json"
  --json-schema <schema>               JSON Schema for structured output
  --tools <tools...>                   Specify the list of available tools; "" disables all
  --strict-mcp-config                  Only use MCP servers from --mcp-config
  --disable-slash-commands             Disable all skills
  --no-session-persistence            Disable session persistence
  --setting-sources <sources>          Comma-separated list of setting sources to load
  --model <model>                      Model for the current session
  --effort <level>                     Effort level (low, medium, high, xhigh, max)
  -v, --version                        Output the version number

Commands:
  auth                                 Manage authentication
"""

# The unsupported_flag scenario advertises a CLI without structured output.
_HELP_MISSING = _HELP_FULL.replace(
    "  --json-schema <schema>               JSON Schema for structured output\n", ""
)


def _auth_status_payload() -> tuple[dict[str, object], int]:
    match common.scenario():
        case "auth_api_billed":
            return (
                {
                    "loggedIn": True,
                    "authMethod": "console",
                    "apiProvider": "firstParty",
                    "subscriptionType": None,
                },
                0,
            )
        case "auth_missing":
            return ({"loggedIn": False}, 1)
        case _:  # subscription auth for every other scenario
            return (
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "email": "mock@example.com",
                    "orgName": "Mock Org",
                    "subscriptionType": "max",
                },
                0,
            )


def _emit_envelope(result: dict[str, object]) -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1200,
        "result": json.dumps(result, ensure_ascii=False),
        "session_id": "mock-session",
        "usage": {"input_tokens": 2000, "output_tokens": 400},
    }
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False))
    sys.stdout.flush()


def _review(argv: list[str]) -> int:
    stdin_data = common.read_stdin_bytes()
    exit_code = 0
    try:
        exit_code = _review_scenarios(argv, stdin_data)
    finally:
        common.log_invocation("claude", argv, stdin_data, exit_code, None)
    return exit_code


def _review_scenarios(argv: list[str], stdin_data: bytes) -> int:
    scen = common.scenario()

    if scen == "unsupported_flag" and "--json-schema" in argv:
        sys.stderr.write("error: unknown option '--json-schema'\n")
        return 2
    if scen == "auth_missing":
        sys.stderr.write("Not logged in. Run `claude auth login`.\n")
        return 1
    if scen == "usage_limit":
        sys.stderr.write("Claude AI usage limit reached|1784332800\n")
        return 1
    if scen == "rate_limit":
        sys.stderr.write(
            'API Error: 429 {"type":"error","error":{"type":"rate_limit_error",'
            '"message":"Number of concurrent connections exceeded"}}\n'
        )
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
        sys.stdout.write("{ this is not JSON at all —")
        return 0
    if scen == "schema_violation" or (scen == "schema_violation_then_success" and attempt == 1):
        _emit_envelope(common.build_schema_violation_result(task))
        return 0

    _emit_envelope(common.build_success_result(task))
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv or "-v" in argv:
        print(VERSION_LINE)
        return 0
    if argv[:2] == ["auth", "status"]:
        payload, code = _auth_status_payload()
        print(json.dumps(payload))
        return code
    if "--help" in argv or "-h" in argv:
        scen = common.scenario()
        sys.stdout.write(_HELP_MISSING if scen == "unsupported_flag" else _HELP_FULL)
        return 0
    if "--print" in argv or "-p" in argv:
        return _review(argv)
    sys.stderr.write("fake claude: unrecognized invocation\n")
    return 2
