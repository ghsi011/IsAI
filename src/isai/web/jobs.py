"""GUI job management: upload storage, background execution, event fan-out.

Each job lives in ``%LOCALAPPDATA%\\IsAI\\jobs\\<job_id>\\`` as ``source.docx`` +
``report.md`` + ``report.sqlite3``. Review runs on a daemon thread — analysis
continues with the browser tab closed but never survives process exit (and the
UI says so). SSE subscribers receive only IDs and statuses; the browser fetches
authoritative data afterwards.
"""

from __future__ import annotations

import contextlib
import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from isai.config import ReviewConfig
from isai.doctor import app_data_dir
from isai.errors import ErrorCategory, IsaiError
from isai.persistence import Journal
from isai.persistence.db import JobStatus
from isai.pipeline import JobRunner, ResumeMode, prepare_job

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ZIP_SIGNATURE = b"PK\x03\x04"

_SAFE_NAME_RE = re.compile(r"[^\w֐-׿ .()\[\]-]", re.UNICODE)


def _rmtree_quiet(path: Path) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)


def sanitize_filename(raw: str) -> str:
    """A display-safe basename: path parts stripped, hostile characters removed."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip(". ")
    return name[:120] or "document.docx"


@dataclass
class ManagedJob:
    job_id: str
    directory: Path
    display_name: str
    thread: threading.Thread | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    subscribers: list[queue.Queue[dict[str, object]]] = field(default_factory=list)
    history: list[dict[str, object]] = field(default_factory=list)
    last_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def source_path(self) -> Path:
        return self.directory / "source.docx"

    @property
    def report_path(self) -> Path:
        return self.directory / "report.md"

    @property
    def journal_path(self) -> Path:
        return self.directory / "report.sqlite3"

    def publish(self, kind: str, payload: dict[str, object]) -> None:
        event = {"kind": kind, **payload}
        with self.lock:
            self.history.append(event)
            for q in list(self.subscribers):
                q.put(event)

    def subscribe(self) -> queue.Queue[dict[str, object]]:
        q: queue.Queue[dict[str, object]] = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, object]]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class JobManager:
    """Registry + executor for GUI jobs. One review thread per job at most."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or app_data_dir()) / "jobs"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, ManagedJob] = {}
        self._lock = threading.Lock()
        self._discover_existing()

    def _discover_existing(self) -> None:
        for job_dir in self.base_dir.iterdir() if self.base_dir.is_dir() else []:
            if (job_dir / "report.sqlite3").is_file():
                display = job_dir.name
                meta_file = job_dir / "meta.json"
                if meta_file.is_file():
                    with contextlib.suppress(json.JSONDecodeError, KeyError):
                        display = json.loads(meta_file.read_text(encoding="utf-8"))["display_name"]
                self._jobs[job_dir.name] = ManagedJob(
                    job_id=job_dir.name, directory=job_dir, display_name=display
                )

    # -- creation ---------------------------------------------------------------

    def create_from_upload(self, filename: str, content: bytes) -> ManagedJob:
        if len(content) > MAX_UPLOAD_BYTES:
            raise IsaiError(
                ErrorCategory.DOCUMENT,
                f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap",
            )
        display_name = sanitize_filename(filename)
        if not display_name.lower().endswith(".docx"):
            raise IsaiError(ErrorCategory.DOCUMENT, "only .docx files are accepted")
        if not content.startswith(ZIP_SIGNATURE):
            raise IsaiError(ErrorCategory.DOCUMENT, "file is not a DOCX (missing ZIP signature)")
        job_id = uuid.uuid4().hex[:12]
        directory = self.base_dir / job_id
        directory.mkdir(parents=True)
        job = ManagedJob(job_id=job_id, directory=directory, display_name=display_name)
        job.source_path.write_bytes(content)
        (directory / "meta.json").write_text(
            json.dumps({"display_name": display_name}), encoding="utf-8"
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def import_journal(self, source: Path, display_name: str | None = None) -> ManagedJob:
        """Register an exported journal as a viewable job (no .docx required).

        The journal is snapshot-copied, validated end to end, and its Markdown
        report regenerated locally — no provider calls. On any failure nothing
        is left behind.
        """
        from isai.persistence.db import safe_copy_journal  # noqa: PLC0415
        from isai.pipeline import rebuild_report  # noqa: PLC0415

        job_id = uuid.uuid4().hex[:12]
        directory = self.base_dir / job_id
        try:
            safe_copy_journal(source, directory / "report.sqlite3")
            journal = Journal.open(directory / "report.sqlite3")
            meta = journal.meta()
            if not journal.elements():
                journal.close()
                raise IsaiError(ErrorCategory.DOCUMENT, "journal contains no elements")
            journal.close()
            name = sanitize_filename(display_name or meta.source_filename)
            (directory / "meta.json").write_text(
                json.dumps({"display_name": name}), encoding="utf-8"
            )
            rebuild_report(directory / "report.sqlite3", directory / "report.md")
        except IsaiError:
            _rmtree_quiet(directory)
            raise
        except Exception as exc:  # unreadable/foreign file
            _rmtree_quiet(directory)
            raise IsaiError(
                ErrorCategory.DOCUMENT, f"not a usable IsAI journal: {source.name}"
            ) from exc
        job = ManagedJob(job_id=job_id, directory=directory, display_name=name)
        with self._lock:
            self._jobs[job_id] = job
        return job

    # -- lookup --------------------------------------------------------------------

    def get(self, job_id: str) -> ManagedJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise IsaiError(ErrorCategory.CONFIGURATION, "unknown job id")
        return job

    def all_jobs(self) -> list[ManagedJob]:
        return sorted(self._jobs.values(), key=lambda j: j.directory.stat().st_mtime)

    # -- execution --------------------------------------------------------------------

    def start(self, job_id: str, config: ReviewConfig, *, restart: bool = False) -> None:
        job = self.get(job_id)
        if job.running:
            raise IsaiError(ErrorCategory.CONFIGURATION, "job is already running")
        job.stop_requested.clear()
        job.last_error = None
        resume_mode = ResumeMode.RESTART if restart else ResumeMode.AUTO

        def work() -> None:
            try:
                prepared = prepare_job(job.source_path, job.report_path, config, resume_mode)
                try:
                    runner = JobRunner(
                        prepared,
                        config,
                        on_event=job.publish,
                        should_stop=job.stop_requested.is_set,
                    )
                    runner.run()
                finally:
                    prepared.journal.close()
            except IsaiError as exc:
                job.last_error = f"{exc.category.value}: {exc.message}"
                job.publish("job_failed", {"category": exc.category.value})
            except Exception:
                job.last_error = "unknown: unexpected error (see server logs with --debug)"
                job.publish("job_failed", {"category": "unknown"})

        job.thread = threading.Thread(target=work, name=f"isai-job-{job_id}", daemon=True)
        job.thread.start()

    def pause_after_current(self, job_id: str) -> None:
        self.get(job_id).stop_requested.set()

    def stop_now(self, job_id: str) -> None:
        """Pause and additionally tree-kill the in-flight provider subprocess."""
        from isai.providers.runner import kill_active_process_of_thread  # noqa: PLC0415

        job = self.get(job_id)
        job.stop_requested.set()
        if job.thread is not None and job.thread.is_alive():
            kill_active_process_of_thread(job.thread.ident or -1)

    def delete(self, job_id: str) -> None:
        import shutil  # noqa: PLC0415

        job = self.get(job_id)
        if job.running:
            self.stop_now(job_id)
            if job.thread is not None:
                job.thread.join(timeout=30)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.directory, ignore_errors=True)

    # -- state ---------------------------------------------------------------------------

    def journal_snapshot(self, job_id: str) -> Journal | None:
        job = self.get(job_id)
        if not job.journal_path.is_file():
            return None
        return Journal.open(job.journal_path)

    def summary(self, job_id: str) -> dict[str, object]:
        job = self.get(job_id)
        base: dict[str, object] = {
            "job_id": job.job_id,
            "display_name": job.display_name,
            "running": job.running,
            "last_error": job.last_error,
        }
        journal = self.journal_snapshot(job_id)
        if journal is None:
            base.update({"status": "new", "progress": {"total": 0, "done": 0}})
            return base
        try:
            meta = journal.meta()
            progress = journal.progress()
        finally:
            journal.close()
        status = meta.status.value
        if job.running and meta.status is JobStatus.IN_PROGRESS:
            status = "analyzing"
        base.update(
            {
                "status": status,
                "paused_reason": meta.paused_reason,
                "provider_mode": meta.provider_mode,
                "created_at": meta.created_at,
                "source_filename": meta.source_filename,
                "progress": progress,
                "resumable": meta.status is JobStatus.PAUSED and not job.running,
            }
        )
        return base
