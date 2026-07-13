# IsAI

> **Important disclaimer — read this first.** Despite the name, **IsAI does not and
> cannot determine whether a text was written by AI.** It is a *stylistic screening and
> academic-revision tool*: it flags AI-associated writing patterns and writing-quality
> issues in `.docx` documents so a human can review them. Authorship cannot be determined
> from style alone, and IsAI never outputs authorship claims or probabilities.

IsAI is a local Windows 10/11 tool that reviews `.docx` documents — primarily long
academic and medical literature reviews — paragraph by paragraph, using **your existing
Claude (Claude.ai subscription) or ChatGPT (Plus) subscription** through the official
`claude` and `codex` CLIs. No API keys, no hosted service, no telemetry; your document
never leaves your machine except as review requests through your own provider account.

```powershell
# CLI
isai review thesis.docx --output thesis-review.md

# GUI (localhost only)
isai gui
```

Every job writes an incrementally-updated Markdown report you can read mid-run, plus a
SQLite journal that makes any interruption safely resumable.

*Full documentation (install, usage, provider setup, privacy notes, testing) is being
written as part of the M3 milestone — see `prompt.md` for the product specification.*

## License

MIT — see [LICENSE](LICENSE).
