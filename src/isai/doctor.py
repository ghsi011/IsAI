"""Environment and provider diagnosis. Never calls a model unless --live-test."""

from __future__ import annotations

import os
import platform
import socket
import sqlite3
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from isai import __version__
from isai.config import ReviewConfig, command_override
from isai.pipeline import make_provider
from isai.providers.runner import detect_billing_env_vars


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = False


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "IsAI"


def _check_writable(name: str, directory: Path) -> Check:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".isai-write-probe"
        probe.write_text("probe", encoding="utf-8")
        with probe.open("r+", encoding="utf-8") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        probe.unlink()
        return Check(name, True, str(directory))
    except OSError as exc:
        return Check(name, False, f"{directory}: {exc.strerror}", critical=True)


def _check_sqlite() -> Check:
    try:
        with tempfile.TemporaryDirectory(prefix="isai-doctor-") as tmp:
            path = Path(tmp) / "probe.sqlite3"
            conn = sqlite3.connect(str(path))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('durable')")
            conn.commit()
            conn.close()
        return Check("sqlite durable writes", True, sqlite3.sqlite_version)
    except sqlite3.Error as exc:  # pragma: no cover - environment-specific
        return Check("sqlite durable writes", False, str(exc), critical=True)


def _check_port_binding() -> Check:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        return Check("loopback port binding", True, f"bound 127.0.0.1:{port}")
    except OSError as exc:  # pragma: no cover - environment-specific
        return Check("loopback port binding", False, str(exc))


def _check_browser() -> Check:
    try:
        browser = webbrowser.get()
        return Check("default browser", True, type(browser).__name__)
    except webbrowser.Error:  # pragma: no cover - environment-specific
        return Check("default browser", False, "no launchable browser found")


def _provider_checks(config: ReviewConfig) -> list[Check]:
    checks: list[Check] = []
    for name in ("claude", "codex"):
        try:
            status = make_provider(name, config).preflight()
        except Exception as exc:
            checks.append(Check(f"provider: {name}", False, f"preflight failed: {exc}"))
            continue
        checks.append(Check(f"provider: {name}", status.usable, status.message))
    return checks


def run_doctor(config: ReviewConfig | None = None, *, live_test: bool = False) -> list[Check]:
    config = config or ReviewConfig(
        claude_command=command_override("ISAI_CLAUDE_COMMAND", ["claude"]),
        codex_command=command_override("ISAI_CODEX_COMMAND", ["codex"]),
    )
    checks = [
        Check("operating system", sys.platform == "win32", platform.platform()),
        Check(
            "python",
            sys.version_info >= (3, 11),
            platform.python_version(),
            critical=sys.version_info < (3, 11),
        ),
        Check("isai package", True, __version__),
        _check_writable("app data directory", app_data_dir()),
        _check_writable("temp directory", Path(tempfile.gettempdir())),
        _check_sqlite(),
        _check_port_binding(),
        _check_browser(),
    ]
    env_names = detect_billing_env_vars()
    checks.append(
        Check(
            "billing env vars",
            not env_names,
            (
                "none set"
                if not env_names
                else "SET (names only): "
                + ", ".join(env_names)
                + " — these can trigger separate API billing in provider CLIs; "
                "IsAI scrubs them from provider subprocesses"
            ),
        )
    )
    checks.extend(_provider_checks(config))
    if live_test:
        checks.extend(_live_test(config))
    return checks


_SYNTHETIC_TEXT = (
    "This synthetic calibration paragraph exists only to verify connectivity. "
    "It describes an invented study of 100 imaginary participants whose entirely "
    "fictional outcomes were measured over twelve invented months for testing."
)


def _live_test(config: ReviewConfig) -> list[Check]:
    from isai.models import Scope  # noqa: PLC0415
    from isai.prompting import ReviewTask  # noqa: PLC0415

    task = ReviewTask(
        element_id="p-000000-livetest",
        display_number=1,
        location="body",
        style_name="Normal",
        nearest_heading=None,
        word_count=len(_SYNTHETIC_TEXT.split()),
        min_words=10,
        scope=Scope.PARAGRAPH,
        text=_SYNTHETIC_TEXT,
    )
    checks: list[Check] = []
    for name in ("claude", "codex"):
        provider = make_provider(name, config)
        status = provider.preflight()
        if not status.usable:
            checks.append(Check(f"live test: {name}", False, f"skipped — {status.message}"))
            continue
        outcome = provider.review(task)
        detail = (
            f"round-trip ok (signal: {outcome.result.style_signal.value})"
            if outcome.ok and outcome.result
            else f"failed: {outcome.error_message}"
        )
        checks.append(Check(f"live test: {name}", outcome.ok, detail))
    return checks
