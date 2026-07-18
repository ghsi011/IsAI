"""The `isai` command-line interface."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer

from isai import __version__
from isai.config import ProviderMode, ReviewConfig, command_override
from isai.errors import ErrorCategory, IsaiError

app = typer.Typer(
    name="isai",
    help=(
        "Screen .docx documents for AI-associated stylistic patterns and "
        "academic-writing quality via your Claude/ChatGPT subscription CLIs. "
        "IsAI cannot determine authorship — it flags style for human review."
    ),
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

_EXIT_BY_CATEGORY = {
    ErrorCategory.DOCUMENT: 3,
    ErrorCategory.CONFIGURATION: 4,
    ErrorCategory.AUTHENTICATION: 5,
    ErrorCategory.BILLING_MODE: 6,
    ErrorCategory.USAGE_LIMIT: 7,
}


def _fail(error: IsaiError) -> typer.Exit:
    typer.secho(f"error ({error.category.value}): {error.message}", fg="red", err=True)
    return typer.Exit(_EXIT_BY_CATEGORY.get(error.category, 1))


def _resolve_resume_mode(resume: bool | None, restart: bool, force_new_report: bool):
    from isai.pipeline import ResumeMode  # noqa: PLC0415 (keep CLI import light)

    if restart:
        return ResumeMode.RESTART
    if force_new_report:
        return ResumeMode.FORCE_NEW_REPORT
    if resume is False:
        return ResumeMode.NO_RESUME
    return ResumeMode.AUTO


@app.command()
def review(
    input_docx: Annotated[Path, typer.Argument(help="The .docx document to review.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Markdown report path.")],
    provider: Annotated[
        ProviderMode, typer.Option("--provider", help="claude | codex | auto | consensus")
    ] = ProviderMode.CLAUDE,
    primary_provider: Annotated[str | None, typer.Option()] = None,
    second_opinion_provider: Annotated[str | None, typer.Option()] = None,
    fallback_provider: Annotated[str | None, typer.Option()] = None,
    claude_model: Annotated[str | None, typer.Option()] = None,
    claude_effort: Annotated[str | None, typer.Option()] = None,
    codex_model: Annotated[str | None, typer.Option()] = None,
    min_words: Annotated[int, typer.Option(min=1)] = 50,
    context_assisted: Annotated[
        bool, typer.Option("--context-assisted/--no-context-assisted")
    ] = True,
    context_before: Annotated[int, typer.Option(min=0, max=5)] = 1,
    context_after: Annotated[int, typer.Option(min=0, max=5)] = 1,
    include_tables: Annotated[bool, typer.Option("--include-tables/--exclude-tables")] = True,
    timeout_seconds: Annotated[int, typer.Option(min=10)] = 300,
    max_retries: Annotated[int, typer.Option(min=0, max=1)] = 1,
    audit_percent: Annotated[int, typer.Option(min=0, max=100)] = 5,
    resume: Annotated[bool | None, typer.Option("--resume/--no-resume")] = None,
    restart: Annotated[bool, typer.Option("--restart")] = False,
    force_new_report: Annotated[bool, typer.Option("--force-new-report")] = False,
    allow_api_billed_auth: Annotated[bool, typer.Option("--allow-api-billed-auth")] = False,
    start_paragraph: Annotated[int | None, typer.Option(min=1)] = None,
    end_paragraph: Annotated[int | None, typer.Option(min=1)] = None,
    max_paragraphs: Annotated[int | None, typer.Option(min=1)] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Review a document paragraph by paragraph; writes REPORT.md + REPORT.sqlite3."""
    from isai.persistence.db import JobStatus  # noqa: PLC0415
    from isai.pipeline import JobRunner, prepare_job  # noqa: PLC0415

    if debug:
        typer.secho(
            "warning: --debug output may include document text; do not share debug "
            "logs if the document is confidential",
            fg="yellow",
            err=True,
        )
    config = ReviewConfig(
        provider_mode=provider,
        claude_command=command_override("ISAI_CLAUDE_COMMAND", ["claude"]),
        codex_command=command_override("ISAI_CODEX_COMMAND", ["codex"]),
        primary_provider=primary_provider,
        second_opinion_provider=second_opinion_provider,
        fallback_provider=fallback_provider,
        claude_model=claude_model,
        claude_effort=claude_effort,
        codex_model=codex_model,
        min_words=min_words,
        context_assisted=context_assisted,
        context_before=context_before,
        context_after=context_after,
        include_tables=include_tables,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        audit_percent=audit_percent,
        start_paragraph=start_paragraph,
        end_paragraph=end_paragraph,
        max_paragraphs=max_paragraphs,
        allow_api_billed=allow_api_billed_auth,
        debug=debug,
    )

    def on_event(kind: str, payload: dict[str, object]) -> None:
        if kind == "paragraph_started" and verbose:
            typer.echo(f"reviewing paragraph {payload.get('display_number')} ...")
        elif kind == "primary_review_completed":
            typer.echo(
                f"paragraph {payload.get('display_number')}: signal={payload.get('style_signal')}"
            )
        elif kind == "paragraph_error":
            typer.secho(f"paragraph error ({payload.get('category')}); continuing", fg="yellow")
        elif kind == "job_paused":
            typer.secho(
                f"job paused ({payload.get('reason')}): {payload.get('message')} — "
                "re-run the same command later to resume",
                fg="yellow",
            )

    try:
        prepared = prepare_job(
            input_docx, output, config, _resolve_resume_mode(resume, restart, force_new_report)
        )
        if prepared.resumed:
            progress = prepared.journal.progress()
            typer.echo(
                f"resuming job {prepared.journal.meta().job_id}: "
                f"{progress['done']}/{progress['total']} tasks already done"
            )
        runner = JobRunner(prepared, config, on_event=on_event)
        status = runner.run()
    except IsaiError as exc:
        raise _fail(exc) from exc
    except KeyboardInterrupt:
        typer.secho(
            "\ninterrupted — progress is saved; re-run the same command to resume",
            fg="yellow",
        )
        raise typer.Exit(130) from None

    if status is JobStatus.COMPLETED:
        typer.secho(f"review complete: {output}", fg="green")
    else:
        raise typer.Exit(7 if status is JobStatus.PAUSED else 1)


@app.command()
def rebuild(
    journal: Annotated[Path, typer.Argument(help="The REPORT.sqlite3 journal file.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Markdown output path.")],
) -> None:
    """Regenerate a deterministic Markdown report from the journal (no provider calls)."""
    from isai.pipeline import rebuild_report  # noqa: PLC0415

    try:
        rebuild_report(journal, output)
    except IsaiError as exc:
        raise _fail(exc) from exc
    typer.secho(f"rebuilt: {output}", fg="green")


@app.command()
def doctor(
    live_test: Annotated[
        bool,
        typer.Option(
            "--live-test",
            help="Send one synthetic paragraph through each usable provider "
            "(consumes a small amount of subscription usage).",
        ),
    ] = False,
) -> None:
    """Diagnose the environment and providers without calling a model."""
    from isai.doctor import run_doctor  # noqa: PLC0415

    if live_test:
        typer.secho(
            "live test enabled: this sends synthetic text through your provider "
            "subscriptions and consumes usage",
            fg="yellow",
        )
    failed_critical = False
    for check in run_doctor(live_test=live_test):
        mark = "ok " if check.ok else "FAIL"
        color = "green" if check.ok else ("red" if check.critical else "yellow")
        typer.secho(f"[{mark}] {check.name}: {check.detail}", fg=color)
        failed_critical |= check.critical and not check.ok
    if failed_critical:
        raise typer.Exit(1)


def _resolve_journal(job_or_path: str) -> Path:
    """Accept either a journal file path or a GUI job ID from `isai jobs`."""
    from isai.doctor import app_data_dir  # noqa: PLC0415

    as_path = Path(job_or_path)
    if as_path.is_file():
        return as_path
    for job_dir in (app_data_dir() / "jobs").glob("*"):
        journal_path = job_dir / "report.sqlite3"
        if not journal_path.is_file():
            continue
        if job_dir.name == job_or_path:
            return journal_path
        try:
            from isai.persistence import Journal  # noqa: PLC0415

            journal = Journal.open(journal_path)
            matches = journal.meta().job_id == job_or_path
            journal.close()
            if matches:
                return journal_path
        except IsaiError:
            continue
    raise IsaiError(
        ErrorCategory.CONFIGURATION,
        f"'{job_or_path}' is neither a journal file nor a known job ID (see `isai jobs`)",
    )


@app.command()
def export(
    job: Annotated[
        str, typer.Argument(help="A job ID from `isai jobs`, or a REPORT.sqlite3 path.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination .sqlite3 file.")],
) -> None:
    """Export a review as a single journal file, safe to move to another PC.

    The snapshot is taken with SQLite's backup API, so it is consistent even if
    the job is still running. The file contains the full document text — treat
    it as confidentially as the document itself.
    """
    from isai.persistence import Journal  # noqa: PLC0415
    from isai.persistence.db import safe_copy_journal  # noqa: PLC0415

    try:
        source = _resolve_journal(job)
        safe_copy_journal(source, output)
        journal = Journal.open(output)  # sanity-check the snapshot
        meta = journal.meta()
        journal.close()
    except IsaiError as exc:
        raise _fail(exc) from exc
    typer.secho(f"exported {meta.source_filename} ({meta.status.value}) -> {output}", fg="green")
    typer.echo("import it elsewhere with: isai import " + output.name)


@app.command("import")
def import_journal(
    journal_file: Annotated[
        Path, typer.Argument(help="A journal (.sqlite3) exported from another IsAI.")
    ],
    name: Annotated[
        str | None, typer.Option("--name", help="Display name shown in the GUI job list.")
    ] = None,
) -> None:
    """Import an exported journal as a viewable job in the GUI job list.

    The Markdown report is regenerated locally (no provider calls). Viewing,
    filtering, and highlights fully work; resuming an unfinished imported job
    would additionally need the original .docx.
    """
    from isai.web.jobs import JobManager  # noqa: PLC0415

    try:
        job = JobManager().import_journal(journal_file, name)
    except IsaiError as exc:
        raise _fail(exc) from exc
    typer.secho(f"imported '{job.display_name}' as job {job.job_id}", fg="green")
    typer.echo("open `isai gui` to view it")


@app.command()
def jobs() -> None:
    """List GUI-managed jobs stored under %LOCALAPPDATA%\\IsAI."""
    from isai.doctor import app_data_dir  # noqa: PLC0415
    from isai.persistence import Journal  # noqa: PLC0415

    jobs_dir = app_data_dir() / "jobs"
    if not jobs_dir.is_dir():
        typer.echo("no jobs")
        return
    for job_dir in sorted(jobs_dir.iterdir()):
        journal_path = job_dir / "report.sqlite3"
        if not journal_path.is_file():
            continue
        try:
            journal = Journal.open(journal_path)
            meta = journal.meta()
            progress = journal.progress()
            journal.close()
        except IsaiError:
            typer.echo(f"{job_dir.name}: (unreadable journal)")
            continue
        typer.echo(
            f"{meta.job_id}  {meta.source_filename}  {meta.status.value}  "
            f"{progress['done']}/{progress['total']}  started {meta.created_at}"
        )


@app.command("delete-job")
def delete_job(
    job_id: Annotated[str, typer.Argument(help="Job ID from `isai jobs`.")],
) -> None:
    """Delete one GUI-managed job's data (report, journal, uploaded copy)."""
    from isai.doctor import app_data_dir  # noqa: PLC0415

    job_dir = app_data_dir() / "jobs" / job_id
    if not job_dir.is_dir():
        typer.secho(f"no such job: {job_id}", fg="red", err=True)
        raise typer.Exit(4)
    shutil.rmtree(job_dir)
    typer.secho(f"deleted job {job_id}", fg="green")


@app.command()
def version() -> None:
    """Print the isai version."""
    typer.echo(__version__)


@app.command()
def gui(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option("--port")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser")] = False,
) -> None:
    """Start the local web GUI (127.0.0.1 only) and open the browser."""
    from isai.web.server import run_gui  # noqa: PLC0415

    if host != "127.0.0.1":
        typer.secho(
            "error (web_security): --host accepts only 127.0.0.1 — the IsAI server "
            "must never be exposed beyond this computer",
            fg="red",
            err=True,
        )
        raise typer.Exit(4)
    try:
        run_gui(port=port, open_browser=not no_browser)
    except IsaiError as exc:
        raise _fail(exc) from exc


def main() -> None:  # console_scripts entry point
    app()


if __name__ == "__main__":
    main()
