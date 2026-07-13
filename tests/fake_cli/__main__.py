"""Entry point: python -m tests.fake_cli {claude|codex} [args...]"""

from __future__ import annotations

import sys


def main() -> int:
    # Piped stdio on Windows defaults to the ANSI code page; the payloads are UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if len(sys.argv) < 2 or sys.argv[1] not in ("claude", "codex"):
        sys.stderr.write("usage: python -m tests.fake_cli {claude|codex} [args...]\n")
        return 64
    tool, argv = sys.argv[1], sys.argv[2:]
    if tool == "claude":
        from tests.fake_cli.claude_mock import main as tool_main  # noqa: PLC0415
    else:
        from tests.fake_cli.codex_mock import main as tool_main  # noqa: PLC0415
    return tool_main(argv)


if __name__ == "__main__":
    sys.exit(main())
