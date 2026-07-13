See [AGENTS.md](AGENTS.md) for the full agent guide — layout, commands, and the hard
rules (subprocess safety, billing protection, untrusted-content handling, no authorship
claims). The product spec is `prompt.md`; running decision log is `DECISIONS.md`.

Quick reference:

- Install/test: `uv sync`, `uv run pytest`, `uv run ruff format --check . && uv run ruff check .`, `uv run pyright`
- Never: `shell=True`, document text in argv/logs, API-key auth without the explicit
  override flag, `claude --bare`, `innerHTML` with untrusted content, authorship claims.
- Always: stdin-only text delivery, fsync-per-result Markdown appends, deterministic
  paragraph IDs, executable mock CLIs in tests.
