# Contributing to IsAI

Thanks for your interest! Before anything else, understand the product framing:
IsAI screens for **AI-associated stylistic patterns**. It is not, and must never
become, an authorship detector. PRs that add authorship claims, probabilities,
detector-evasion features, or third-party detector integrations will be declined.

## Setup

```powershell
git clone https://github.com/ghsi011/IsAI
cd IsAI
uv sync                      # Python 3.12 (pinned), all dev deps
uv run playwright install chromium   # for the single GUI smoke test
uv run pre-commit install
```

## Development loop

```powershell
uv run pytest -m "not e2e and not playwright_smoke"   # fast suite
uv run pytest                                         # everything (mock CLIs only)
uv run ruff format . ; uv run ruff check .
uv run pyright
```

The full suite uses **executable mock provider CLIs** (`tests/fake_cli`) as real
child processes. Nothing in the test suite or CI ever calls the real `claude` or
`codex` binaries; optional real-provider tests are opt-in, local-only, and
consume your subscription usage (see README → Testing).

## Hard rules (enforced by tests — see AGENTS.md for the full list)

- Argument arrays only; document text reaches providers only via stdin.
- No API-key billing without the explicit override flag; billing env vars are
  scrubbed from child processes.
- Logs never contain paragraph text, raw provider output, or secrets.
- All document/provider text is untrusted: no `innerHTML`, escape Markdown.
- Never commit real documents, reports, or SQLite job files.
- Record non-obvious decisions in `DECISIONS.md`.

## Pull requests

- Keep changes focused; include tests for behavior you add or fix.
- CI must be green: format, lint, strict types, tests (85% branch coverage on
  core packages), build, audit.
- Describe *why*, not just *what*, in the PR body.
