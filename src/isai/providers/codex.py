"""Codex CLI adapter.

Invokes ``codex exec`` non-interactively with the most restrictive available
isolation (verified against codex-cli 0.141.0 — see D-012 in DECISIONS.md):
read-only sandbox, ephemeral session, no user config, no rules files, fresh temp
working directory. The structured result arrives via ``--output-last-message``
into a file inside the isolated workdir; the schema goes in via
``--output-schema``. ``--yolo`` / ``danger-full-access`` are never used.
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

#: Flags this adapter passes to `codex exec`; each must appear in its --help.
REQUIRED_FLAGS = (
    "--sandbox",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--output-schema",
    "--output-last-message",
    "--cd",
)

_VERSION_RE = re.compile(r"codex-cli\s+(\S+)|(\d+\.\d+\.\d+\S*)")

_SCHEMA_FILE = "result_schema.json"
_OUTPUT_FILE = "last_message.json"


class CodexAdapter(CliReviewAdapter):
    name = ProviderName.CODEX

    def __init__(self, settings: ProviderSettings) -> None:
        super().__init__(settings)
        self._schema_text = json.dumps(review_result_json_schema(), indent=2)

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
                message="codex CLI not found on PATH; install Codex CLI and sign in "
                "with your ChatGPT account",
            )
        match = _VERSION_RE.search(version_proc.stdout)
        version = next((g for g in (match.groups() if match else ()) if g), None)
        self._cli_version = version

        help_proc = self._run_quick(["exec", "--help"])
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
                    "installed codex CLI lacks required isolation/structured-output "
                    f"flags: {', '.join(missing)}; upgrade Codex CLI — IsAI never "
                    "weakens isolation to compensate"
                ),
            )

        auth_state = self._auth_state()
        usable = auth_state is AuthState.SUBSCRIPTION or (
            auth_state is AuthState.API_BILLED and self.settings.allow_api_billed
        )
        message = {
            AuthState.SUBSCRIPTION: f"codex {version}: ChatGPT subscription auth verified",
            AuthState.API_BILLED: (
                "codex CLI is authenticated with an API key (usage-based billing); "
                "refusing by default — pass --allow-api-billed-auth to override"
            ),
            AuthState.MISSING: "codex CLI is not logged in; run `codex login`",
            AuthState.UNKNOWN: (
                "could not determine codex auth mode; refusing to assume subscription billing"
            ),
        }[auth_state]
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
        proc = self._run_quick(["login", "status"])
        text = f"{proc.stdout}\n{proc.stderr}".lower()
        if "logged in using chatgpt" in text:
            return AuthState.SUBSCRIPTION
        if "api key" in text:
            return AuthState.API_BILLED
        if "not logged in" in text or proc.exit_code != 0:
            return AuthState.MISSING
        return AuthState.UNKNOWN

    # -- review ---------------------------------------------------------------

    def _review_argv(self, workdir: Path) -> list[str]:
        schema_path = workdir / _SCHEMA_FILE
        schema_path.write_text(self._schema_text, encoding="utf-8")
        argv = [
            *self.settings.command_prefix,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--cd",
            str(workdir),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(workdir / _OUTPUT_FILE),
            "--color",
            "never",
        ]
        if self.settings.model:
            argv += ["--model", self.settings.model]
        argv.append("-")  # read the prompt from stdin
        return argv

    def _extract_result_json(self, proc: ProcessResult, workdir: Path) -> str:
        out_path = workdir / _OUTPUT_FILE
        if not out_path.is_file():
            raise OutputParseError("codex did not produce the last-message output file")
        content = out_path.read_text(encoding="utf-8").strip()
        if not content:
            raise OutputParseError("codex last-message output file was empty")
        return content
