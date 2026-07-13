# FastAPI registers the route closures below via decorators.
# pyright: reportUnusedFunction=false
"""The IsAI local web GUI server.

Binds to 127.0.0.1 only, on a random free port unless one is given. Every
request must carry the per-run access token (see :mod:`isai.web.security`).
The server is available only on this computer and dies with the process.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import webbrowser
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from isai.config import ReviewConfig, command_override
from isai.errors import ErrorCategory, IsaiError
from isai.pipeline import rebuild_report
from isai.web.jobs import JobManager
from isai.web.security import SecurityMiddleware, generate_token
from isai.web.state import element_detail, job_state

_WEB_DIR = Path(__file__).parent

_PROVIDER_MODES = {"claude", "codex", "auto", "consensus"}


def _error_response(exc: IsaiError) -> JSONResponse:
    status = {
        ErrorCategory.DOCUMENT: 400,
        ErrorCategory.CONFIGURATION: 400,
        ErrorCategory.WEB_SECURITY: 403,
        ErrorCategory.AUTHENTICATION: 409,
        ErrorCategory.BILLING_MODE: 409,
        ErrorCategory.USAGE_LIMIT: 409,
    }.get(exc.category, 500)
    return JSONResponse({"error": exc.category.value, "detail": exc.message}, status_code=status)


def _config_from_form(form: dict[str, Any]) -> ReviewConfig:
    provider = str(form.get("provider", "claude"))
    if provider not in _PROVIDER_MODES:
        raise IsaiError(ErrorCategory.CONFIGURATION, f"unknown provider '{provider}'")

    def _int(name: str, default: int) -> int:
        raw = form.get(name)
        try:
            return int(raw) if raw not in (None, "") else default
        except (TypeError, ValueError) as exc:
            raise IsaiError(ErrorCategory.CONFIGURATION, f"invalid integer for '{name}'") from exc

    return ReviewConfig(
        provider_mode=provider,  # type: ignore[arg-type]
        claude_command=command_override("ISAI_CLAUDE_COMMAND", ["claude"]),
        codex_command=command_override("ISAI_CODEX_COMMAND", ["codex"]),
        min_words=_int("min_words", 50),
        context_assisted=str(form.get("context_assisted", "on")) != "off",
        include_tables=str(form.get("include_tables", "on")) != "off",
        timeout_seconds=_int("timeout_seconds", 300),
        audit_percent=_int("audit_percent", 5),
        allow_api_billed=str(form.get("allow_api_billed", "")) == "on",
    )


def create_app(  # noqa: PLR0915 - route registration is linear, not complex
    *, token: str, port: int, manager: JobManager | None = None
) -> FastAPI:
    app = FastAPI(title="IsAI", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, token=token, port=port)
    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    jobs = manager if manager is not None else JobManager()
    app.state.manager = jobs
    app.state.token = token

    @app.exception_handler(IsaiError)
    async def isai_error_handler(_request: Request, exc: IsaiError) -> JSONResponse:
        return _error_response(exc)

    # -- pages -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {"token": token})

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str) -> HTMLResponse:
        jobs.get(job_id)  # 400 on unknown id
        return templates.TemplateResponse(request, "job.html", {"token": token, "job_id": job_id})

    # -- job collection -----------------------------------------------------------

    @app.get("/api/jobs")
    async def list_jobs() -> JSONResponse:
        return JSONResponse({"jobs": [jobs.summary(j.job_id) for j in jobs.all_jobs()]})

    @app.post("/api/jobs")
    async def upload(request: Request, file: UploadFile) -> JSONResponse:
        content = await file.read()
        form = dict(await request.form())
        config = _config_from_form(form)  # reject bad settings before storing anything
        job = jobs.create_from_upload(file.filename or "document.docx", content)
        (job.directory / "config.json").write_text(config.model_dump_json(), encoding="utf-8")
        jobs.start(job.job_id, config)
        return JSONResponse({"job_id": job.job_id}, status_code=201)

    # -- one job ----------------------------------------------------------------------

    def _saved_config(job_id: str) -> ReviewConfig:
        job = jobs.get(job_id)
        config_file = job.directory / "config.json"
        if config_file.is_file():
            return ReviewConfig.model_validate_json(config_file.read_text(encoding="utf-8"))
        return _config_from_form({})

    @app.get("/api/jobs/{job_id}/state")
    async def state(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        journal = jobs.journal_snapshot(job_id)
        if journal is None:
            return JSONResponse({"job": jobs.summary(job_id), "elements": []})
        try:
            payload = job_state(journal, running=job.running)
        finally:
            journal.close()
        payload["job"]["display_name"] = job.display_name
        payload["job"]["last_error"] = job.last_error
        return JSONResponse(payload)

    @app.get("/api/jobs/{job_id}/elements/{element_id}")
    async def element(job_id: str, element_id: str) -> JSONResponse:
        journal = jobs.journal_snapshot(job_id)
        if journal is None:
            raise IsaiError(ErrorCategory.CONFIGURATION, "job has no journal yet")
        try:
            return JSONResponse(element_detail(journal, element_id))
        finally:
            journal.close()

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, max_seconds: float | None = None) -> StreamingResponse:
        """SSE stream of job events (IDs and statuses only, never text).

        Ends at terminal job states; the browser's EventSource reconnects and the
        page re-fetches authoritative state. ``max_seconds`` bounds the connection
        (used by tests, whose HTTP client buffers whole responses)."""
        job = jobs.get(job_id)

        async def stream() -> AsyncIterator[str]:
            q = job.subscribe()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max_seconds if max_seconds and max_seconds > 0 else None
            try:
                while deadline is None or loop.time() < deadline:
                    try:
                        event = await loop.run_in_executor(None, q.get, True, _keepalive_seconds())
                    except Exception:
                        if deadline is not None and not job.running:
                            break
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                    if event["kind"] in ("job_completed", "job_failed"):
                        break
            finally:
                job.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- controls -------------------------------------------------------------------------

    @app.post("/api/jobs/{job_id}/resume")
    async def resume(job_id: str) -> JSONResponse:
        jobs.start(job_id, _saved_config(job_id))
        return JSONResponse({"ok": True})

    @app.post("/api/jobs/{job_id}/restart")
    async def restart(job_id: str) -> JSONResponse:
        jobs.start(job_id, _saved_config(job_id), restart=True)
        return JSONResponse({"ok": True})

    @app.post("/api/jobs/{job_id}/pause")
    async def pause(job_id: str) -> JSONResponse:
        jobs.pause_after_current(job_id)
        return JSONResponse({"ok": True})

    @app.post("/api/jobs/{job_id}/stop")
    async def stop(job_id: str) -> JSONResponse:
        jobs.stop_now(job_id)
        return JSONResponse({"ok": True})

    @app.post("/api/jobs/{job_id}/rebuild")
    async def rebuild(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job.running:
            raise IsaiError(ErrorCategory.CONFIGURATION, "stop or pause the job before rebuilding")
        rebuild_report(job.journal_path, job.report_path)
        return JSONResponse({"ok": True})

    @app.delete("/api/jobs/{job_id}")
    async def delete(job_id: str) -> JSONResponse:
        jobs.delete(job_id)
        return JSONResponse({"ok": True})

    # -- downloads (explicit endpoints only; never arbitrary paths) -------------------------

    @app.get("/api/jobs/{job_id}/report")
    async def report(job_id: str) -> FileResponse:
        job = jobs.get(job_id)
        if not job.report_path.is_file():
            raise IsaiError(ErrorCategory.FILESYSTEM, "no report yet")
        return FileResponse(
            job.report_path,
            media_type="text/markdown",
            filename=f"{Path(job.display_name).stem}-review.md",
        )

    @app.get("/api/jobs/{job_id}/journal")
    async def journal_download(job_id: str, confirm: str = "") -> FileResponse:
        if confirm != "yes":
            raise IsaiError(
                ErrorCategory.CONFIGURATION,
                "the journal contains full document text; pass confirm=yes to download",
            )
        job = jobs.get(job_id)
        if job.running:
            raise IsaiError(
                ErrorCategory.CONFIGURATION,
                "pause or stop the job before downloading the journal (the file is being written)",
            )
        if not job.journal_path.is_file():
            raise IsaiError(ErrorCategory.FILESYSTEM, "no journal yet")
        return FileResponse(
            job.journal_path,
            media_type="application/vnd.sqlite3",
            filename=f"{Path(job.display_name).stem}-review.sqlite3",
        )

    @app.post("/api/jobs/{job_id}/open-folder")
    async def open_folder(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        os.startfile(str(job.directory))  # noqa: S606 - user-initiated, own job dir
        return JSONResponse({"ok": True})

    return app


def _keepalive_seconds() -> float:
    """SSE keep-alive interval; overridable for tests."""
    try:
        return float(os.environ.get("ISAI_SSE_KEEPALIVE_SECONDS", "15"))
    except ValueError:
        return 15.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_gui(port: int | None = None, open_browser: bool = True) -> None:
    """Start the GUI server (blocking) and open the browser at the tokenized URL."""
    actual_port = port or _free_port()
    token = generate_token()
    app = create_app(token=token, port=actual_port)
    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    print(f"IsAI GUI: {url}")
    print("The server is available only on this computer. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=actual_port,
        log_level="warning",
        access_log=False,
    )
    uvicorn.Server(config).run()
