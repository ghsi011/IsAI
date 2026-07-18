# IsAI

> **Read this first.** Despite the name, **IsAI does not and cannot determine
> whether a text was written by AI.** It is an *AI-writing-style screening and
> academic-revision tool*: it flags **AI-associated stylistic patterns** and
> writing-quality issues in `.docx` documents so a human can review them.
> Authorship cannot be determined from style alone. IsAI never outputs
> authorship claims, probabilities, or pass/fail verdicts — and no tool of this
> kind reliably can. Treat every signal as "look here", never as "proof".

IsAI is a local **Windows 10/11** tool that reviews `.docx` documents —
primarily long academic and medical literature reviews — paragraph by
paragraph, using **your existing Claude (Claude.ai) or ChatGPT (Plus)
subscription** through the official `claude` and `codex` CLIs. No API keys, no
hosted service, no third-party detectors, no telemetry.

```powershell
# CLI
isai review thesis.docx --output thesis-review.md

# GUI (localhost only, opens your browser)
isai gui
```

Every job produces:

- **`thesis-review.md`** — a human-readable report, written incrementally: you
  can open and read it *while the review is still running*;
- **`thesis-review.sqlite3`** — the authoritative progress journal. Any
  interruption (crash, Ctrl+C, sleep, usage limit) is safely resumable with
  zero duplicated results, and `isai rebuild` regenerates the report from it
  deterministically.

---

## Install (Windows 10/11)

IsAI needs Python 3.11+ and at least one provider CLI.

**With [uv](https://docs.astral.sh/uv/) (recommended):**

```powershell
uv tool install isai
```

**With pipx:**

```powershell
pipx install isai
```

### Provider CLIs (at least one)

- **Claude Code** (uses your Claude.ai subscription — Pro/Max):
  install per [Anthropic's instructions](https://docs.anthropic.com/en/docs/claude-code),
  then sign in with your **Claude.ai account** (not an API key):
  `claude auth login` → choose the Claude.ai/subscription option.
- **Codex CLI** (uses your ChatGPT subscription — Plus/Pro):
  `npm install -g @openai/codex`, then `codex login` and sign in **with
  ChatGPT** (not an API key).

> **Avoid API billing.** If you sign in with an Anthropic Console account or an
> OpenAI API key, usage is billed per token separately from your subscription.
> IsAI checks the auth mode before every job and **refuses API-billed auth by
> default** (`--allow-api-billed-auth` overrides, if you truly want that). It
> also warns if `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY`
> / `CODEX_API_KEY` are set (detected by name only) and scrubs them from
> provider subprocesses.

### Check your setup

```powershell
isai doctor
```

`doctor` verifies Python, writable directories, SQLite durability, port
binding, browser launch, each provider's presence/version/auth mode/required
capabilities, and billing-related env-var names — **without calling a model**.
`isai doctor --live-test` additionally sends one synthetic paragraph through
each usable provider (this consumes a small amount of subscription usage and
says so).

---

## Using the GUI

```powershell
isai gui            # random port; --port N to fix it; --no-browser to not open one
```

Your browser opens a page served **only on this computer** (`127.0.0.1`, with a
random per-run access token in the URL). Then:

1. **Drag a `.docx` onto the drop zone** (or pick a file). Uploads are
   validated: extension + ZIP signature, size cap, ZIP-safety checks, sanitized
   names, SHA-256 recorded on arrival.
2. The **document pane** shows every paragraph in true document order —
   including table cells — as cards with number, style, nearest heading, and
   status. Results stream in live as each paragraph completes.
3. Click a card to open the **analysis pane**: style signal
   (`none/mild/moderate/strong/indeterminate`), confidence *in the
   observations*, review priority, indicators with exact quoted evidence,
   counter-indicators, writing-quality issues, citation observations, revision
   suggestions, and the mandatory limitations note.
4. **Exact text highlighting:** the quoted evidence is located in the paragraph
   locally (never trusting model offsets) and marked with per-category
   underline styles and icons (not color alone). Click a highlight to focus its
   annotation; click an annotation to focus its highlight. Quotes that cannot
   be located reliably are labeled *unresolved* — never guessed. Note this is
   text-based highlighting: page/line positions in Word may differ.
5. **Filter/search bar:** all / unanalyzed / analyzing / high priority /
   moderate+strong / needs source check / has suggestions / provider
   disagreement / errors / short-indeterminate; search by number, heading,
   text, or category.
6. **Controls:** pause after current paragraph, stop the provider process,
   resume, rebuild report, download report (and, deliberately gated, the SQLite
   journal). The **job list** on the home page survives restarts, with
   open/resume/rebuild/delete for each job.

Closing the tab does **not** stop the analysis — it continues in the IsAI
process and the page catches up when reopened. Exiting the `isai gui` process
*does* stop it; the job stays resumable.

## Using the CLI

```powershell
# Claude-first (default provider):
isai review thesis.docx --output thesis-review.md

# Codex only:
isai review thesis.docx --output thesis-review.md --provider codex

# Whichever subscription provider is available:
isai review thesis.docx --output thesis-review.md --provider auto

# Consensus (two providers, second opinions on notable paragraphs):
isai review thesis.docx --output thesis-review.md --provider consensus

# Resume an interrupted job (same command resumes automatically), explicitly:
isai review thesis.docx --output thesis-review.md --resume

# Rebuild the Markdown deterministically from the journal (no provider calls):
isai rebuild thesis-review.sqlite3 --output thesis-review.md

# Move a review to another PC: export there, import here (viewing needs no .docx):
isai export <job-id-or-journal-path> --output thesis-review.sqlite3
isai import thesis-review.sqlite3
```

Selected options (see `isai review --help` for all): `--min-words` (default
50), `--context-assisted/--no-context-assisted` with `--context-before/-after`
(default 1/1), `--include-tables/--exclude-tables`, `--timeout-seconds` 300,
`--audit-percent` 5 (deterministic consensus sampling), `--claude-model`,
`--claude-effort`, `--codex-model`, `--primary-provider`,
`--second-opinion-provider`, `--fallback-provider`,
`--start-paragraph/--end-paragraph/--max-paragraphs`, `--restart`,
`--force-new-report`, `--debug`, `--verbose`.

### Stop, resume, usage limits

Interrupt any time with `Ctrl+C` — everything committed so far is preserved.
When your subscription's usage window is exhausted, the job **pauses cleanly**
(exit code 7) with the paragraph left pending; run the same command again later
and it resumes from the first incomplete paragraph with zero duplicates. Resume
refuses to run if the source document, extraction settings, or review
configuration changed (use `--restart` to start over).

### How results are worded (and why)

- `style_signal` describes **observable style only**. `strong` requires several
  independent indicator types with exact quoted evidence; single features are
  never decisive. Formal tone, passive voice, technical terminology, correct
  grammar, and non-native phrasing are explicitly *not* penalized.
- Fragments below `--min-words` (default 50) — names, dates, list stubs,
  title-page lines — are **skipped entirely**: no provider call, no report
  section, no GUI card. Reviewable paragraphs are numbered 1..N in reading
  order, so "Paragraph 1" is the first substantive paragraph regardless of how
  much front matter precedes it. Lower `--min-words` if you want shorter
  paragraphs included.
- Revision suggestions are justified as improvements to natural, specific,
  evidence-connected academic writing. IsAI never advises deliberate errors or
  "detector evasion". As defense-in-depth behind the reviewer prompt,
  validation rejects fabricated quotes outright and rejects recognizable
  authorship-claim, probability, and evasion language in provider output
  (a pattern filter — thorough, but no language filter is exhaustive).

## Privacy

- Your document stays on your machine. Reviewed paragraph text (plus one
  neighboring paragraph each way, by default) is sent to the provider **you
  chose** through **your own account** via the official CLI — nowhere else.
- The Markdown report and SQLite journal contain document text; keep them as
  private as the document. The GUI's journal download is deliberately gated.
- Normal logs and console output never contain document text; `--debug` may,
  and warns you.
- No telemetry, no analytics, no network access except the provider CLIs'.
- GUI jobs (uploaded copy, report, journal) live under `%LOCALAPPDATA%\IsAI\jobs`;
  delete them in the GUI or with `isai delete-job JOB_ID`.
- `isai export` / `isai import` move a finished (or in-progress) review between
  PCs as a single journal file — it contains the full document text, so
  transfer it as carefully as the document itself.

## Known DOCX limitations

Extracted and reviewed: body paragraphs, headings, lists, and table cells
(interleaved in true document order), visible hyperlink text, explicit line
breaks, Unicode/RTL text. **Not extracted** (and never claimed as reviewed):
text boxes and shapes, comments, footnotes/endnotes, headers/footers, tracked
deletions, embedded objects. Corrupt, encrypted, or suspicious containers
(ZIP bombs, traversal paths, DTDs) are rejected with actionable errors.

## Troubleshooting (Windows)

- **File locked:** close the report in apps that take exclusive locks (Word
  does; most editors don't) or antivirus scanners mid-scan; IsAI retries are
  deliberate about durability, so a locked report fails loudly rather than
  silently.
- **Antivirus:** the first run may be slow while Defender scans the venv; the
  SQLite `-wal` files next to the journal are normal.
- **`claude`/`codex` not found:** new terminals pick up PATH changes; `isai
  doctor` shows what IsAI sees.

## Testing (for contributors)

```powershell
uv sync
uv run pytest -m "not e2e and not playwright_smoke"   # fast unit suite
uv run pytest                                         # everything
uv run playwright install chromium                    # once, for the GUI smoke test
```

The suite runs **executable mock provider CLIs** (`tests/fake_cli`) as real
Windows child processes, covering success, malformed output, schema violations,
usage limits, auth states, timeouts, and process-tree kills. CI (GitHub
Actions, `windows-latest`, Python 3.12) runs format, lint, strict type checks,
the full suite including mock end-to-end and one Playwright smoke test, a
package build with wheel smoke test, and `pip-audit` — with **no secrets and no
real provider CLIs**.

Optional real-provider tests exist behind the `claude_integration` /
`codex_integration` markers; they are disabled by default, never run in CI,
require explicit environment variables, use synthetic text only, and consume
subscription usage.

## Sample output

A report section for a (synthetic) formulaic paragraph looks like:

> **Style signal:** moderate · **Confidence in observations:** medium ·
> **Review priority:** medium
>
> The paragraph shows uniform constructions; manual review recommended…
>
> **Indicators (AI-associated style):**
> - *formulaic_transition* — "Moreover, it is important to note": the same
>   stock transition opens consecutive sentences.
>
> *Limitations:* Stylistic observation only; authorship cannot be determined
> from style alone.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The one-paragraph architecture tour
lives in [AGENTS.md](AGENTS.md); design decisions in [DECISIONS.md](DECISIONS.md).

## License

MIT — see [LICENSE](LICENSE).
