# Security policy

## Reporting a vulnerability

Please open a **private security advisory** on GitHub
(Security → Advisories → "Report a vulnerability") rather than a public issue.
Include reproduction steps and affected versions. You should receive a response
within 7 days.

## Scope — what IsAI promises

IsAI is a local, single-user Windows tool. Its security-relevant guarantees:

- The web GUI binds to `127.0.0.1` only; every request requires a random
  per-run access token; the `Host` header is validated; a restrictive CSP
  forbids external origins and inline script.
- Provider CLIs run as subprocesses with argument arrays (no shell), isolated
  temporary working directories, all built-in tools/MCP/sessions disabled, and
  billing-capable environment variables scrubbed by name.
- Document text is treated as untrusted everywhere (extraction, prompts,
  reports, GUI); provider output is treated as untrusted too.
- Hostile DOCX containers (ZIP bombs, traversal names, encrypted or DTD-bearing
  XML) are rejected before parsing.
- IsAI never transmits documents anywhere except through the user's own
  `claude`/`codex` CLI to their chosen provider; there is no telemetry.

Reports about weaknesses in any of these areas are very welcome. Reports that
require an attacker who already controls the local user account are generally
out of scope (IsAI does not defend against a compromised machine).
