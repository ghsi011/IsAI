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

## Core pipeline (M1)

- **D-030 — Occurrence indices are 1-based.** ``occurrence_index=1`` is the first
  occurrence of a quotation in the target paragraph; ``null`` means first/only. An
  out-of-range index resolves to ``unresolved``, never clamped to a wrong occurrence.
- **D-031 — Conservative-Unicode matching is regex-class based.** Instead of index-
  mapping normalized strings back to original offsets, the fallback tiers compile the
  quotation into a regex over the *original* text (quote/dash/space equivalence classes,
  NFC/NFD letter alternation, combining-mark tolerance, ellipsis≈"..."), so matches are
  natively in original coordinates.
- **D-032 — Short-paragraph policy.** Headings and empty paragraphs are skipped (no
  provider call, marked in report/GUI). Short non-heading paragraphs use context-assisted
  review (scope ``context_window``) when neighbors exist; with context assist off or no
  neighbors, IsAI synthesizes an ``indeterminate`` result locally (provider ``local``) and
  never calls the model.
- **D-033 — Windows tree-kill order.** ``taskkill /T`` must run while the hung root
  process is still alive (Windows enumerates a tree from a living root), so timeout
  handling is: graceful ``taskkill /T`` → short wait → forced ``taskkill /T /F`` — never
  ``terminate()`` first (that orphans grandchildren).
- **D-034 — SQLite settings.** WAL + ``synchronous=FULL`` + explicit ``BEGIN IMMEDIATE``
  transactions (``isolation_level=None``); DDL executed statement-wise because
  ``executescript`` implicitly commits.
- **D-035 — Provider command override env vars.** ``ISAI_CLAUDE_COMMAND`` /
  ``ISAI_CODEX_COMMAND`` (JSON arrays) override the provider executables — used by the
  mock end-to-end tests and available for nonstandard install paths. Part of the config
  fingerprint, so a resumed job can't silently switch executables.
- **D-036 — CLI exit codes.** 0 success; 3 document; 4 configuration; 5 authentication;
  6 billing_mode; 7 paused (usage limit/interrupt with resumable state); 130 Ctrl+C.
- **D-037 — `--max-turns` does not exist in claude 2.1.183.** Verified against the real
  help output; single-turn behavior is guaranteed by ``--tools ""`` instead.
- **D-038 — Repair-retry scope.** Only repairable *output* problems (malformed JSON,
  schema violation, content-rule violation) trigger the single repair retry. Process
  failures (timeout, non-zero exit) are classified and recorded without retry; pausing
  categories (auth/billing/usage) reset the paragraph to pending and pause the job.

## Web GUI (M2)

- **D-050 — Pure ASGI security middleware.** Starlette's ``BaseHTTPMiddleware``
  buffers response bodies, which silently stalls Server-Sent Events; the
  token/Host/headers middleware is therefore written against the raw ASGI
  interface.
- **D-051 — SSE design.** Events carry IDs and statuses only; the browser
  re-fetches authoritative state after each event and on every (re)connect — no
  ``Last-Event-ID`` replay. Streams close at terminal job states and accept an
  optional ``max_seconds`` bound because Starlette's TestClient cannot consume
  infinite streams.
- **D-052 — Access token transport.** The token is embedded in the launched URL
  (query param) and accepted via the ``X-IsAI-Token`` header for API calls;
  no cookies, so nothing is attached cross-origin automatically.
- **D-053 — GUI stop semantics.** "Pause after current" sets a flag checked
  between paragraphs; "Stop provider process" additionally tree-kills the job
  thread's in-flight provider subprocess (tracked in a per-thread registry) and
  the resulting failure is classified as an interruption — the paragraph goes
  back to pending, never to an error.
- **D-054 — Frontend text handling.** Document and provider text reaches the
  page only through JSON APIs and is rendered exclusively with
  ``textContent``/``createTextNode``; templates interpolate nothing but the
  token and job ID. Highlight categories are distinguished by underline style
  and icon, not color alone.

## Release engineering (M3)

- **D-060 — Coverage gate.** 85% branch coverage enforced in CI only on the core
  logic packages (docxio, textmatch, models, validation, highlights, providers,
  persistence, pipeline) — measured 91% at gate introduction. Web templates,
  static assets, and CLI glue carry no numeric gate by design.
- **D-061 — Actions pinned by SHA.** All workflow actions are pinned to commit
  SHAs (resolved from the GitHub API at authoring time) with tag comments;
  Dependabot updates them weekly.
- **D-062 — `--max-retries` is real.** 0 disables the schema-repair retry; 1
  (default, also the maximum) allows exactly one. The flag flows
  ReviewConfig → ProviderSettings → the adapter loop.

## Post-release adjustments

- **D-070 — Sub-threshold paragraphs are skipped, not reviewed (user decision,
  2026-07-14).** The original spec reported short paragraphs as ``indeterminate``
  (standalone) or reviewed them in a context window. Real-thesis usage showed this
  buries the report under title-page/list noise and spends provider usage on
  fragments, so paragraphs below ``min_words`` are now excluded from review and
  display entirely, and reviewable paragraphs are renumbered 1..N. ``rebuild``
  filters legacy journals the same way. Users who want short paragraphs reviewed
  lower ``--min-words``.

## Prior work

- **D-020 — Previous AI artifacts deleted.** The repo contained `.omo/` research
  scaffolding (claim-graph templates, all statuses "Pending"), empty `.agents/`/`.tmp/`
  dirs, and a `.codegraph` symlink. Every research file was an unfilled template with zero
  findings, so all were deleted rather than reused.
