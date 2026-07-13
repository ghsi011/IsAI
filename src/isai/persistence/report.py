"""Crash-safe incremental Markdown report writing.

The contract (tested in test_report.py):

- the header is durably on disk (flush + fsync + close) before any provider call;
- each section append opens the file, writes, flushes, fsyncs, and closes before
  the next paragraph starts — the handle is never held across paragraphs;
- a concurrent reader sees a valid, readable report at any moment;
- every section starts with a unique marker line carrying only IDs and hashes, so
  reconciliation can detect which sections already exist without parsing prose.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from isai.errors import ErrorCategory, IsaiError
from isai.persistence.render import MARKER_PREFIX, SUMMARY_MARKER

_MARKER_RE = re.compile(
    r"^\[//\]: # \(isai:result element=(?P<element>\S+) role=(?P<role>\S+) sha=(?P<sha>\S+)\)\s*$"
)


class ReportWriter:
    """Append-only writer for one report file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, header_markdown: str) -> None:
        """Write the report header durably. Fails if the file already exists."""
        if self.path.exists():
            raise IsaiError(
                ErrorCategory.FILESYSTEM,
                f"report already exists: {self.path.name} (use resume, --restart, or "
                "--force-new-report)",
            )
        self._durable_write("x", header_markdown)

    def append_section(self, section_markdown: str) -> None:
        """Durably append one section; the handle is closed before returning."""
        if not self.path.is_file():
            raise IsaiError(
                ErrorCategory.FILESYSTEM,
                f"report file disappeared mid-run: {self.path.name}",
            )
        self._durable_write("a", section_markdown)

    def _durable_write(self, mode: str, content: str) -> None:
        try:
            with self.path.open(mode, encoding="utf-8", newline="\n") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise IsaiError(
                ErrorCategory.FILESYSTEM,
                f"could not write report file {self.path.name}: {exc.strerror}",
            ) from exc

    def has_summary(self) -> bool:
        if not self.path.is_file():
            return False
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            return any(line.startswith(SUMMARY_MARKER) for line in fh)

    def existing_markers(self) -> set[tuple[str, str]]:
        """(element_id, role) pairs whose sections are already in the file."""
        if not self.path.is_file():
            return set()
        markers: set[tuple[str, str]] = set()
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith(MARKER_PREFIX):
                    continue
                match = _MARKER_RE.match(line.rstrip("\n"))
                if match:
                    markers.add((match.group("element"), match.group("role")))
        return markers
