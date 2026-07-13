"""The SQLite progress journal — the authoritative record of a review job.

Design rules:

- every state change is one committed transaction (`synchronous=FULL`, WAL);
- a result is committed to SQLite *before* its Markdown section is appended, and
  the ``markdown_written`` flag is committed *after* the fsync — so a crash at any
  point is recoverable by reconciliation without duplicating a completed result;
- resume verifies source hash, extraction fingerprint, and configuration
  fingerprint before touching anything;
- rows never contain log output or secrets; they do contain document text
  (element records and results), exactly like the Markdown report next to them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from isai.docxio import DocElement
from isai.errors import ErrorCategory, IsaiError
from isai.highlights import Highlight
from isai.models import ReviewResult
from isai.providers.base import AttemptRecord

SCHEMA_VERSION = 1


class TaskRole(StrEnum):
    PRIMARY = "primary"
    SECOND_OPINION = "second_opinion"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    ERROR = "error"
    SKIPPED = "skipped"


class JobStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class JobMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    source_filename: str  # sanitized display name only
    source_sha256: str
    extraction_fingerprint: str
    config_fingerprint: str
    config_json: str
    prompt_version: str
    provider_mode: str
    created_at: str
    status: JobStatus = JobStatus.IN_PROGRESS
    paused_reason: str | None = None
    completed_at: str | None = None


class TaskRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    role: TaskRole
    status: TaskStatus
    provider: str | None = None
    result: ReviewResult | None = None
    error_category: str | None = None
    error_message: str | None = None
    attempts: list[AttemptRecord] = []
    highlights: list[Highlight] = []
    agreement: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    markdown_written: bool = False


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    job_id TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    extraction_fingerprint TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    paused_reason TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS element (
    element_id TEXT PRIMARY KEY,
    ord INTEGER NOT NULL UNIQUE,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task (
    element_id TEXT NOT NULL REFERENCES element(element_id),
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    result_json TEXT,
    error_category TEXT,
    error_message TEXT,
    attempts_json TEXT NOT NULL DEFAULT '[]',
    highlights_json TEXT NOT NULL DEFAULT '[]',
    agreement TEXT,
    started_at TEXT,
    finished_at TEXT,
    markdown_written INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (element_id, role)
);
"""


class Journal:
    """One review job's SQLite journal. Not thread-safe; one writer at a time."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._conn = connection
        self.path = path

    # -- lifecycle -------------------------------------------------------------

    @classmethod
    def create(
        cls, path: Path, meta: JobMeta, elements: list[DocElement], roles: list[TaskRole]
    ) -> Journal:
        if path.exists():
            raise IsaiError(
                ErrorCategory.DATABASE,
                f"journal already exists: {path.name} (use resume, --restart, or "
                "--force-new-report)",
            )
        conn = cls._connect(path)
        journal = cls(conn, path)
        with journal.transaction() as cur:
            # executescript() would implicitly COMMIT; run the DDL statement-wise
            # so the whole creation stays one atomic transaction.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            cur.execute(
                "INSERT INTO job (id, job_id, source_filename, source_sha256,"
                " extraction_fingerprint, config_fingerprint, config_json,"
                " prompt_version, provider_mode, created_at, status)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meta.job_id,
                    meta.source_filename,
                    meta.source_sha256,
                    meta.extraction_fingerprint,
                    meta.config_fingerprint,
                    meta.config_json,
                    meta.prompt_version,
                    meta.provider_mode,
                    meta.created_at,
                    meta.status.value,
                ),
            )
            for element in elements:
                cur.execute(
                    "INSERT INTO element (element_id, ord, data_json) VALUES (?, ?, ?)",
                    (element.element_id, element.order, element.model_dump_json()),
                )
                for role in roles:
                    cur.execute(
                        "INSERT INTO task (element_id, role, status) VALUES (?, ?, ?)",
                        (element.element_id, role.value, TaskStatus.PENDING.value),
                    )
        return journal

    @classmethod
    def open(cls, path: Path) -> Journal:
        if not path.is_file():
            raise IsaiError(ErrorCategory.DATABASE, f"journal not found: {path.name}")
        conn = cls._connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.close()
            raise IsaiError(
                ErrorCategory.DATABASE,
                f"journal schema version {version} is not supported (expected {SCHEMA_VERSION})",
            )
        return cls(conn, path)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    # -- job metadata ------------------------------------------------------------

    def meta(self) -> JobMeta:
        row = self._conn.execute("SELECT * FROM job WHERE id = 1").fetchone()
        if row is None:
            raise IsaiError(ErrorCategory.DATABASE, "journal has no job row")
        return JobMeta(
            job_id=row["job_id"],
            source_filename=row["source_filename"],
            source_sha256=row["source_sha256"],
            extraction_fingerprint=row["extraction_fingerprint"],
            config_fingerprint=row["config_fingerprint"],
            config_json=row["config_json"],
            prompt_version=row["prompt_version"],
            provider_mode=row["provider_mode"],
            created_at=row["created_at"],
            status=JobStatus(row["status"]),
            paused_reason=row["paused_reason"],
            completed_at=row["completed_at"],
        )

    def set_status(self, status: JobStatus, paused_reason: str | None = None) -> None:
        completed_at = utc_now_iso() if status is JobStatus.COMPLETED else None
        with self.transaction() as cur:
            cur.execute(
                "UPDATE job SET status = ?, paused_reason = ?,"
                " completed_at = COALESCE(?, completed_at) WHERE id = 1",
                (status.value, paused_reason, completed_at),
            )

    # -- elements ---------------------------------------------------------------

    def elements(self) -> list[DocElement]:
        rows = self._conn.execute("SELECT data_json FROM element ORDER BY ord").fetchall()
        return [DocElement.model_validate_json(row["data_json"]) for row in rows]

    def element(self, element_id: str) -> DocElement:
        row = self._conn.execute(
            "SELECT data_json FROM element WHERE element_id = ?", (element_id,)
        ).fetchone()
        if row is None:
            raise IsaiError(ErrorCategory.DATABASE, f"unknown element {element_id}")
        return DocElement.model_validate_json(row["data_json"])

    # -- tasks --------------------------------------------------------------------

    def _task_from_row(self, row: sqlite3.Row) -> TaskRow:
        return TaskRow(
            element_id=row["element_id"],
            role=TaskRole(row["role"]),
            status=TaskStatus(row["status"]),
            provider=row["provider"],
            result=(
                ReviewResult.model_validate_json(row["result_json"]) if row["result_json"] else None
            ),
            error_category=row["error_category"],
            error_message=row["error_message"],
            attempts=[AttemptRecord.model_validate(a) for a in json.loads(row["attempts_json"])],
            highlights=[Highlight.model_validate(h) for h in json.loads(row["highlights_json"])],
            agreement=row["agreement"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            markdown_written=bool(row["markdown_written"]),
        )

    def task(self, element_id: str, role: TaskRole) -> TaskRow:
        row = self._conn.execute(
            "SELECT * FROM task WHERE element_id = ? AND role = ?",
            (element_id, role.value),
        ).fetchone()
        if row is None:
            raise IsaiError(ErrorCategory.DATABASE, f"unknown task {element_id}/{role.value}")
        return self._task_from_row(row)

    def tasks(self, role: TaskRole | None = None) -> list[TaskRow]:
        if role is not None:
            rows = self._conn.execute(
                "SELECT task.* FROM task JOIN element USING (element_id)"
                " WHERE task.role = ? ORDER BY element.ord, task.role",
                (role.value,),
            )
        else:
            rows = self._conn.execute(
                "SELECT task.* FROM task JOIN element USING (element_id)"
                " ORDER BY element.ord, task.role"
            )
        return [self._task_from_row(r) for r in rows]

    def next_pending(self, role: TaskRole = TaskRole.PRIMARY) -> str | None:
        """Element ID of the first task not yet completed/skipped (resume point).

        ``active`` counts as incomplete: a crash mid-paragraph leaves an active
        row that must be redone.
        """
        row = self._conn.execute(
            "SELECT task.element_id FROM task JOIN element USING (element_id)"
            " WHERE task.role = ? AND task.status IN (?, ?)"
            " ORDER BY element.ord LIMIT 1",
            (role.value, TaskStatus.PENDING.value, TaskStatus.ACTIVE.value),
        ).fetchone()
        return row["element_id"] if row else None

    def next_pending_any(self, roles: list[TaskRole]) -> tuple[str, TaskRole] | None:
        """First (element, role) needing work, in document order, primary first."""
        placeholders = ",".join("?" for _ in roles)
        # S610-style guard: `placeholders` is only a run of "?" marks; all values bind.
        query = (
            "SELECT task.element_id, task.role"
            " FROM task JOIN element USING (element_id)"
            f" WHERE task.role IN ({placeholders}) AND task.status IN (?, ?)"
            " ORDER BY element.ord,"
            "  CASE task.role WHEN 'primary' THEN 0 ELSE 1 END LIMIT 1"
        )
        row = self._conn.execute(
            query,
            [r.value for r in roles] + [TaskStatus.PENDING.value, TaskStatus.ACTIVE.value],
        ).fetchone()
        return (row["element_id"], TaskRole(row["role"])) if row else None

    def set_agreement(self, element_id: str, role: TaskRole, agreement: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET agreement = ? WHERE element_id = ? AND role = ?",
                (agreement, element_id, role.value),
            )

    def mark_active(self, element_id: str, role: TaskRole, provider: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET status = ?, provider = ?, started_at = ?"
                " WHERE element_id = ? AND role = ?",
                (TaskStatus.ACTIVE.value, provider, utc_now_iso(), element_id, role.value),
            )

    def record_result(
        self,
        element_id: str,
        role: TaskRole,
        *,
        provider: str,
        result: ReviewResult,
        highlights: list[Highlight],
        attempts: list[AttemptRecord],
        agreement: str | None = None,
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET status = ?, provider = ?, result_json = ?,"
                " highlights_json = ?, attempts_json = ?, agreement = ?,"
                " finished_at = ?, error_category = NULL, error_message = NULL"
                " WHERE element_id = ? AND role = ?",
                (
                    TaskStatus.COMPLETED.value,
                    provider,
                    result.model_dump_json(),
                    json.dumps([h.model_dump(mode="json") for h in highlights]),
                    json.dumps([a.model_dump(mode="json") for a in attempts]),
                    agreement,
                    utc_now_iso(),
                    element_id,
                    role.value,
                ),
            )

    def record_error(
        self,
        element_id: str,
        role: TaskRole,
        *,
        provider: str | None,
        category: ErrorCategory,
        message: str,
        attempts: list[AttemptRecord],
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET status = ?, provider = ?, error_category = ?,"
                " error_message = ?, attempts_json = ?, finished_at = ?"
                " WHERE element_id = ? AND role = ?",
                (
                    TaskStatus.ERROR.value,
                    provider,
                    category.value,
                    message,
                    json.dumps([a.model_dump(mode="json") for a in attempts]),
                    utc_now_iso(),
                    element_id,
                    role.value,
                ),
            )

    def mark_skipped(self, element_id: str, role: TaskRole) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET status = ? WHERE element_id = ? AND role = ?",
                (TaskStatus.SKIPPED.value, element_id, role.value),
            )

    def set_markdown_written(self, element_id: str, role: TaskRole, written: bool = True) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE task SET markdown_written = ? WHERE element_id = ? AND role = ?",
                (int(written), element_id, role.value),
            )

    def progress(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM task WHERE role = ? GROUP BY status",
            (TaskRole.PRIMARY.value,),
        ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        total = sum(counts.values())
        done = counts.get("completed", 0) + counts.get("error", 0) + counts.get("skipped", 0)
        return {"total": total, "done": done, "by_status": counts}
