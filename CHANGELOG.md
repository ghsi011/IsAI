# Changelog

All notable changes to IsAI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.1.0] — 2026-07-14

Initial release.

### Changed
- Paragraphs below the `--min-words` threshold (names, list stubs, title-page
  lines) are skipped entirely — no provider call, no report section, no GUI
  card — and reviewable paragraphs are renumbered 1..N. `isai rebuild` applies
  the same filtering to journals recorded before this change.
- GUI highlight colors are semantic: red = AI-associated indicator, green =
  counter-indicator, amber = quality issue, blue = citation, purple =
  suggestion.

### Added
- `isai review`: paragraph-by-paragraph stylistic screening of `.docx` files via
  the user's own `claude` (Claude.ai subscription) or `codex` (ChatGPT) CLI.
- Crash-safe incremental Markdown report + authoritative SQLite journal with
  resume, reconciliation, and deterministic `isai rebuild`.
- Localhost-only web GUI (`isai gui`): drag-and-drop, live streaming results,
  exact-text highlighting with overlap splitting, filters/search, consensus
  view, job management.
- Provider modes: `claude`, `codex`, `auto`, `consensus` (second opinions with
  agreement classification, never averaging).
- Billing protection: subscription-auth verification, API-billed rejection by
  default, billing env-var scrubbing.
- `isai doctor` environment/provider diagnosis; `--live-test` opt-in.
- Executable mock provider CLIs and a full invariant test suite (Windows CI).
