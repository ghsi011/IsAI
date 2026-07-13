# IsAI — agent guide

IsAI screens `.docx` documents (long academic/medical literature reviews) for
AI-associated **stylistic patterns** and academic-writing quality. It is explicitly *not*
an authorship detector — never add code, copy, or report language that claims a passage
*was* written by AI or a human. The full product spec lives in `prompt.md`; decisions made
along the way are in `DECISIONS.md`. Read both before large changes.

## How it works (one paragraph)

A DOCX is safety-checked, extracted in true document order into paragraphs with
deterministic IDs, then each paragraph is reviewed **sequentially** by spawning the user's
own `claude` or `codex` CLI as a subprocess (subscription auth only — no API keys, no
direct API calls, no telemetry). Every result is committed to a SQLite journal and
appended to an incrementally-written Markdown report (flush + fsync per paragraph) so any
interruption can resume with zero duplicates. A localhost-only FastAPI GUI streams results
live over SSE and highlights exact evidence text resolved locally (never trust LLM
offsets).

## Layout

- `src/isai/` — the package (src layout, hatchling build)
  - `models.py`, `validation.py` — strict Pydantic result schema + content rules
  - `docxio/` — ZIP safety + ordered extraction + deterministic IDs
  - `providers/` — subprocess runner, claude/codex adapters, auto, consensus
  - `persistence/` — SQLite journal, incremental Markdown writer, resume/rebuild
  - `pipeline.py` — per-paragraph crash-consistent orchestration
  - `web/` — FastAPI app, templates, local static assets (no CDNs, no build system)
  - `cli.py` — Typer app (`isai review|gui|rebuild|doctor|jobs|delete-job|version`)
- `prompts/` — versioned reviewer + repair prompts (ship in the wheel)
- `tests/fake_cli/` — executable mock claude/codex run as real child processes
- `scripts/generate_docx_fixtures.py` — synthetic DOCX fixtures (never commit real docs)
- `docs/research/` — captured `--help` output of the provider CLIs we target

## Commands

```powershell
uv sync                        # install (Python 3.12 pinned)
uv run pytest                  # full suite (mock CLIs, no real providers)
uv run pytest -m "not e2e and not playwright_smoke"   # fast unit tests
uv run ruff format --check . ; uv run ruff check .
uv run pyright
uv run isai doctor             # environment/provider diagnosis, no model call
```

## Hard rules (enforced by tests — do not weaken)

1. Subprocesses: argument arrays only, never `shell=True`, never via PowerShell/cmd.
   Document text reaches providers **only via stdin** — never argv, env, or a file path.
2. Billing: reject API-billed/API-key auth unless `--allow-api-billed-auth`; scrub
   `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`OPENAI_API_KEY`/`CODEX_API_KEY` from child
   env (detect by name only, never read values). `claude --bare` is forbidden (D-011).
3. Logs never contain paragraph/context text, raw provider output, or secrets
   (`--debug` may, and must warn).
4. All document text and provider output is untrusted: escape everywhere, no `innerHTML`,
   escape Markdown control characters in reports.
5. Web server binds `127.0.0.1` only; every request requires the per-run access token.
6. Never present authorship conclusions; keep the §2 register ("AI-associated stylistic
   patterns", "manual review recommended", …).
7. Tests must exercise the executable mock CLIs as real child processes, not just
   monkeypatched functions.
8. Never commit user documents, reports, SQLite job files, or anything containing
   document text.

## Conventions

- Python ≥3.11 code style, Ruff (format + lint) and Pyright strict must pass.
- Provider adapters take an **injectable command prefix** so tests substitute
  `[sys.executable, "-m", "tests.fake_cli", "claude"]`.
- Error taxonomy (use everywhere): `document`, `configuration`, `authentication`,
  `billing_mode`, `usage_limit`, `rate_limit`, `timeout`, `provider_transient`,
  `provider_permanent`, `validation`, `filesystem`, `database`, `web_security`,
  `interrupted`, `unknown`.
- Record any new non-obvious decision in `DECISIONS.md` when you make it.
