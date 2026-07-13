"""Claude Code CLI adapter.

Invokes ``claude`` in non-interactive print mode with structured JSON output and
every available isolation flag (verified against claude 2.1.183 — see D-010/D-011
in DECISIONS.md). ``--bare`` is deliberately NOT used: it disables OAuth and would
force API-key billing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from isai.models import review_result_json_schema
from isai.providers.base import (
    AuthState,
    PreflightStatus,
    ProviderName,
    ProviderSettings,
)
from isai.providers.cli_adapter import (
    CliReviewAdapter,
    OutputParseError,
    check_capabilities,
)
from isai.providers.runner import ProcessResult, detect_billing_env_vars

#: Flags this adapter passes; each must appear in ``--help`` or preflight fails.
REQUIRED_FLAGS = (
    "--print",
    "--output-format",
    "--json-schema",
    "--tools",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--setting-sources",
)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\S*)")

#: authMethod values that indicate the user's Claude.ai subscription (OAuth).
_SUBSCRIPTION_AUTH_METHODS = frozenset({"claude.ai", "claudeai", "oauth"})


class ClaudeAdapter(CliReviewAdapter):
    name = ProviderName.CLAUDE

    def __init__(self, settings: ProviderSettings) -> None:
        super().__init__(settings)
        self._schema_arg = json.dumps(review_result_json_schema(), separators=(",", ":"))

    # -- preflight ------------------------------------------------------------

    def preflight(self) -> PreflightStatus:
        billing_env = detect_billing_env_vars()
        try:
            version_proc = self._run_quick(["--version"])
        except FileNotFoundError:
            return PreflightStatus(
                provider=self.name,
                installed=False,
                billing_env_vars=billing_env,
                message="claude CLI not found on PATH; install Claude Code and sign in",
            )
        match = _VERSION_RE.search(version_proc.stdout)
        version = match.group(1) if match else None
        self._cli_version = version

        help_proc = self._run_quick(["--help"])
        missing = check_capabilities(help_proc.stdout, REQUIRED_FLAGS)
        if missing:
            return PreflightStatus(
                provider=self.name,
                installed=True,
                version=version,
                capabilities_ok=False,
                missing_capabilities=missing,
                billing_env_vars=billing_env,
                message=(
                    "installed claude CLI lacks required isolation/structured-output "
                    f"flags: {', '.join(missing)}; upgrade Claude Code — IsAI never "
                    "weakens isolation to compensate"
                ),
            )

        auth_state = self._auth_state()
        usable = auth_state is AuthState.SUBSCRIPTION or (
            auth_state is AuthState.API_BILLED and self.settings.allow_api_billed
        )
        message = {
            AuthState.SUBSCRIPTION: f"claude {version}: subscription auth verified",
            AuthState.API_BILLED: (
                "claude CLI is authenticated in an API-billed mode (Console/API key); "
                "refusing by default — pass --allow-api-billed-auth to override"
            ),
            AuthState.MISSING: "claude CLI is not logged in; run `claude auth login`",
            AuthState.UNKNOWN: (
                "could not determine claude auth mode; refusing to assume subscription billing"
            ),
        }[auth_state]
        if billing_env and usable:
            message += (
                "; note: billing-capable environment variables are set "
                f"({', '.join(billing_env)}) and will be scrubbed from provider "
                "subprocesses"
            )
        return PreflightStatus(
            provider=self.name,
            installed=True,
            version=version,
            auth_state=auth_state,
            capabilities_ok=True,
            billing_env_vars=billing_env,
            usable=usable,
            message=message,
        )

    def _auth_state(self) -> AuthState:
        proc = self._run_quick(["auth", "status", "--json"])
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return AuthState.MISSING if proc.exit_code != 0 else AuthState.UNKNOWN
        if not isinstance(payload, dict):
            return AuthState.UNKNOWN
        if payload.get("loggedIn") is not True:
            return AuthState.MISSING
        method = str(payload.get("authMethod", "")).lower()
        if method in _SUBSCRIPTION_AUTH_METHODS:
            return AuthState.SUBSCRIPTION
        return AuthState.API_BILLED

    # -- review ---------------------------------------------------------------

    def _review_argv(self, workdir: Path) -> list[str]:
        argv = [
            *self.settings.command_prefix,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            self._schema_arg,
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--setting-sources",
            "",
        ]
        if self.settings.model:
            argv += ["--model", self.settings.model]
        if self.settings.effort:
            argv += ["--effort", self.settings.effort]
        return argv

    def _extract_result_json(self, proc: ProcessResult, workdir: Path) -> str:
        try:
            outer = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise OutputParseError("stdout was not valid JSON") from exc
        if not isinstance(outer, dict):
            raise OutputParseError("stdout JSON was not an object")
        if outer.get("is_error"):
            raise OutputParseError("provider envelope reported an execution error")
        # Envelope variations, most specific first.
        structured = outer.get("structured_output")
        if isinstance(structured, dict):
            return json.dumps(structured, ensure_ascii=False)
        result_field = outer.get("result")
        if isinstance(result_field, str):
            return result_field
        if isinstance(result_field, dict):
            return json.dumps(result_field, ensure_ascii=False)
        if "schema_version" in outer:
            return json.dumps(outer, ensure_ascii=False)
        raise OutputParseError("envelope contained no usable result field")
