"""Safe subprocess execution for provider CLIs.

Invariants enforced here (tested in test_runner.py / test_claude_adapter.py):

- argument arrays only; no shell interpretation of any kind exists in this codebase;
- document text enters a child only through stdin bytes;
- billing-capable env vars are scrubbed **by name** from the child environment;
- every invocation runs in an isolated temporary working directory;
- on timeout: graceful terminate, short wait, then Windows process-**tree** kill
  (``taskkill /T /F``) so no orphaned provider or Python children survive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: Env vars that can silently switch a CLI to API billing. Detected by NAME only;
#: values are never read, logged, or compared.
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
)

_GRACEFUL_WAIT_SECONDS = 3.0


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


def detect_billing_env_vars(env: dict[str, str] | None = None) -> list[str]:
    """Names of billing-capable env vars present in ``env`` (default: this process)."""
    source = os.environ if env is None else env
    return [name for name in BILLING_ENV_VARS if name in source]


def scrubbed_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the current environment with billing-capable vars removed."""
    env = {k: v for k, v in os.environ.items() if k not in BILLING_ENV_VARS}
    if extra:
        env.update(extra)
    return env


@contextmanager
def isolated_workdir(prefix: str = "isai-run-") -> Generator[Path]:
    """A fresh temporary working directory, removed afterwards (best effort)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def kill_process_tree(pid: int, *, force: bool = True) -> None:
    """Terminate a process and all of its descendants.

    Must be called while the root process is still alive — Windows can only
    enumerate a tree from a living root. ``force=False`` asks politely (WM_CLOSE);
    ``force=True`` is TerminateProcess for the whole tree.
    """
    if sys.platform == "win32":
        argv = ["taskkill", "/T"] + (["/F"] if force else []) + ["/PID", str(pid)]
        subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
    else:  # pragma: no cover - Windows is the only supported platform
        import contextlib  # noqa: PLC0415
        import signal  # noqa: PLC0415

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)


def run_process(
    argv: list[str],
    *,
    stdin_text: str | None,
    timeout_seconds: float,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run one CLI invocation. Never raises on non-zero exit; raises only on
    a missing executable (``FileNotFoundError``) so callers can classify it."""
    if not argv:
        raise ValueError("argv must not be empty")
    start = time.monotonic()
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=env if env is not None else scrubbed_child_env(),
        creationflags=creationflags,
    )
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    timed_out = False
    try:
        stdout_b, stderr_b = process.communicate(stdin_bytes, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Tree operations must run while the root is alive (see kill_process_tree):
        # graceful tree close first, then a forced tree kill for whatever remains.
        kill_process_tree(process.pid, force=False)
        try:
            stdout_b, stderr_b = process.communicate(timeout=_GRACEFUL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_tree(process.pid, force=True)
            try:
                stdout_b, stderr_b = process.communicate(timeout=_GRACEFUL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                stdout_b, stderr_b = b"", b""
    duration = time.monotonic() - start
    return ProcessResult(
        argv=list(argv),
        exit_code=None if timed_out else process.returncode,
        stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
        duration_seconds=duration,
        timed_out=timed_out,
    )
