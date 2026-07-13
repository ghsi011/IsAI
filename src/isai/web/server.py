"""FastAPI server bootstrap (implemented in milestone M2)."""

from __future__ import annotations

from isai.errors import ErrorCategory, IsaiError


def run_gui(port: int | None = None, open_browser: bool = True) -> None:
    raise IsaiError(
        ErrorCategory.CONFIGURATION,
        "the GUI ships in milestone M2; use `isai review` meanwhile",
    )
