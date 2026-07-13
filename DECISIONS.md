# Engineering decisions

Running log of decisions made while building IsAI, per the project brief. Newest entries
are appended at the bottom of each section.

## Naming & packaging

- **D-001 — PyPI name.** `isai` returned 404 on `https://pypi.org/pypi/isai/json`
  (checked 2026-07-13), so the distribution, package, and CLI command are all `isai`.
  No suffix needed.
- **D-002 — Python versions.** `requires-python = ">=3.11"` per the brief; local dev and
  CI both pin **3.12** (`.python-version`) so behavior matches the single CI matrix slot.
- **D-003 — Build backend.** Hatchling with a `src/` layout: simplest modern backend, no
  setup.py, clean wheel content control (`prompts/` ships inside the package data).
- **D-004 — Type checker.** Pyright strict (of the "mypy or Pyright" choice): faster on
  Windows, better Pydantic inference, no plugin needed.

## Provider CLI facts (verified against installed executables, 2026-07-13)

- **D-010 — Claude CLI surface** (`claude 2.1.183`): non-interactive mode is
  `-p/--print` with `--output-format json`; structured output via `--json-schema`;
  isolation via `--tools ""` (disables all built-in tools), `--strict-mcp-config` (with no
  `--mcp-config` → no MCP), `--disable-slash-commands`, `--no-session-persistence`,
  `--setting-sources ""` (don't load user/project settings). Auth inspection:
  `claude auth status --json` → `{loggedIn, authMethod, apiProvider, subscriptionType}`;
  `authMethod: "claude.ai"` indicates subscription (OAuth) auth. Full help captured in
  `docs/research/claude-cli-help-2.1.183.txt`.
- **D-011 — `--bare` is forbidden for our use.** Although it looks like an isolation
  flag, `claude --bare` restricts auth to `ANTHROPIC_API_KEY`/apiKeyHelper only (OAuth and
  keychain are never read) — the opposite of our billing-protection requirement. We use
  the individual isolation flags from D-010 instead.
- **D-012 — Codex CLI surface** (`codex-cli 0.141.0`): `codex exec` reads the prompt from
  stdin when the positional arg is `-`; isolation via `--sandbox read-only`,
  `--ephemeral` (no session files), `--ignore-user-config`, `--ignore-rules`,
  `--skip-git-repo-check`, `--cd <tempdir>`; structured output via
  `--output-schema <file>` + `-o/--output-last-message <file>`. Auth inspection:
  `codex login status` → `"Logged in using ChatGPT"` for subscription auth. Full help
  captured in `docs/research/codex-exec-help-0.141.0.txt`.
- **D-013 — Capability detection.** Adapters parse `--help` output at preflight and
  require the exact flags above; a CLI lacking any required isolation/structured-output
  flag fails with a compatibility error naming the missing flag (never silently degrade).

## Prior work

- **D-020 — Previous AI artifacts deleted.** The repo contained `.omo/` research
  scaffolding (claim-graph templates, all statuses "Pending"), empty `.agents/`/`.tmp/`
  dirs, and a `.codegraph` symlink. Every research file was an unfilled template with zero
  findings, so all were deleted rather than reused.
