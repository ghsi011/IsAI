"""Web server tests (§10 item 8): security, upload validation, SSE, downloads."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from scripts.generate_docx_fixtures import build_thesis

from isai.web.jobs import JobManager, sanitize_filename
from isai.web.server import create_app
from tests.conftest import mock_prefix

pytestmark = [pytest.mark.web, pytest.mark.usefixtures("mock_env", "no_billing_env")]

TOKEN = "test-token-0123456789"
PORT = 8000  # TestClient default host is "testserver"; we validate Host explicitly


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ISAI_CLAUDE_COMMAND", json.dumps(mock_prefix("claude")))
    monkeypatch.setenv("ISAI_CODEX_COMMAND", json.dumps(mock_prefix("codex")))
    manager = JobManager(base_dir=tmp_path / "appdata")
    app = create_app(token=TOKEN, port=PORT, manager=manager)
    with TestClient(
        app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-IsAI-Token": TOKEN}
    ) as test_client:
        yield test_client


def make_docx_bytes(tmp_path: Path, paragraphs: int = 3) -> bytes:
    path = build_thesis(tmp_path / "upload-src.docx", paragraphs=paragraphs)
    return path.read_bytes()


def upload(
    client: TestClient, tmp_path: Path, *, filename: str = "thesis.docx", **form: str
) -> httpx.Response:
    data = {"provider": "claude", "min_words": "10", **form}
    return client.post(
        "/api/jobs",
        files={"file": (filename, make_docx_bytes(tmp_path), "application/octet-stream")},
        data=data,
    )


def wait_for_status(
    client: TestClient, job_id: str, wanted: str, timeout: float = 90.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        res = client.get(f"/api/jobs/{job_id}/state")
        assert res.status_code == 200
        last = res.json()
        if last["job"]["status"] == wanted:
            return last
        time.sleep(0.3)
    pytest.fail(f"job never reached {wanted}; last={last.get('job')}")


# -- security --------------------------------------------------------------------


def test_missing_token_rejected(client: TestClient) -> None:
    res = client.get("/api/jobs", headers={"X-IsAI-Token": ""})
    assert res.status_code == 403


def test_invalid_token_rejected(client: TestClient) -> None:
    res = client.get("/api/jobs", headers={"X-IsAI-Token": "wrong-token"})
    assert res.status_code == 403


def test_token_via_query_param_accepted(client: TestClient) -> None:
    res = client.get(f"/api/jobs?token={TOKEN}", headers={"X-IsAI-Token": ""})
    assert res.status_code == 200


def test_invalid_host_rejected(client: TestClient) -> None:
    res = client.get("/api/jobs", headers={"Host": "evil.example.com"})
    assert res.status_code == 403


def test_dns_rebinding_host_with_right_port_rejected(client: TestClient) -> None:
    res = client.get("/api/jobs", headers={"Host": f"attacker.tld:{PORT}"})
    assert res.status_code == 403


def test_security_headers_present(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp  # no inline script, no external origins
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Cache-Control"] == "no-store"


def test_unknown_job_is_client_error(client: TestClient) -> None:
    assert client.get("/api/jobs/nope/state").status_code == 400
    assert client.get("/job/nope").status_code == 400


# -- upload validation ---------------------------------------------------------------


def test_upload_rejects_wrong_extension(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path, filename="essay.pdf")
    assert res.status_code == 400
    assert ".docx" in res.json()["detail"]


def test_upload_rejects_bad_signature(client: TestClient) -> None:
    res = client.post(
        "/api/jobs",
        files={"file": ("fake.docx", b"MZ not a zip at all", "application/octet-stream")},
        data={"provider": "claude"},
    )
    assert res.status_code == 400
    assert "ZIP signature" in res.json()["detail"]


def test_upload_sanitizes_malicious_filename(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path, filename="..\\..\\evil<script>.docx")
    assert res.status_code == 201
    job_id = res.json()["job_id"]
    state = client.get(f"/api/jobs/{job_id}/state").json()
    name = state["job"]["display_name"]
    assert "\\" not in name and "/" not in name and "<" not in name
    wait_for_status(client, job_id, "completed")


def test_sanitize_filename_unit() -> None:
    assert sanitize_filename("../../x.docx") == "x.docx"
    assert sanitize_filename("C:\\docs\\תיזה סופית.docx") == "תיזה סופית.docx"
    assert "<" not in sanitize_filename("a<b>c.docx")
    assert sanitize_filename("") == "document.docx"


def test_upload_rejects_bad_provider(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path, provider="openrouter")
    assert res.status_code == 400


def test_no_arbitrary_path_access(client: TestClient) -> None:
    """Path traversal through job/element ids must not reach the filesystem."""
    res = client.get("/api/jobs/..%5C..%5Csecrets/state")
    assert res.status_code in (400, 404)
    res = client.get("/static/../pyproject.toml")
    assert res.status_code in (400, 404)


# -- job lifecycle + SSE -----------------------------------------------------------------


def test_upload_review_stream_and_download(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path)
    assert res.status_code == 201
    job_id = res.json()["job_id"]

    state = wait_for_status(client, job_id, "completed")
    cards = state["elements"]
    assert cards, "elements present"
    reviewed = [c for c in cards if c["status"] == "completed"]
    assert reviewed
    assert all(c["style_signal"] for c in reviewed)

    # Detail endpoint: exact text + locally resolved segments.
    target = next(c for c in reviewed if not c["is_heading"])
    detail = client.get(f"/api/jobs/{job_id}/elements/{target['element_id']}").json()
    assert detail["result"]["schema_version"] == "1.0"
    assert detail["segments"], "segments for highlighting"
    covered = "".join(detail["element"]["text"][s["start"] : s["end"]] for s in detail["segments"])
    assert covered == detail["element"]["text"], "segments tile the exact text"

    # Report download.
    report = client.get(f"/api/jobs/{job_id}/report")
    assert report.status_code == 200
    assert "cannot determine authorship" in report.text

    # Journal requires explicit confirmation.
    assert client.get(f"/api/jobs/{job_id}/journal").status_code == 400
    assert client.get(f"/api/jobs/{job_id}/journal?confirm=yes").status_code == 200


def collect_sse(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Slow the mock so the subscription opens before the job finishes."""
    monkeypatch.setenv("ISAI_SSE_KEEPALIVE_SECONDS", "1")
    monkeypatch.setenv("MOCK_LLM_DELAY_SECONDS", "0.3")
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "delayed_completion")
    res = upload(client, tmp_path)
    assert res.status_code == 201
    job_id = res.json()["job_id"]
    lines: list[str] = []
    keepalives = 0
    res = client.get(f"/api/jobs/{job_id}/events?max_seconds=60")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    for line in res.text.splitlines():
        if line.startswith(": keep-alive"):
            keepalives += 1
        else:
            lines.append(line)
    return lines


def test_sse_events_flow(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = collect_sse(client, tmp_path, monkeypatch)
    kinds = [ln.removeprefix("event: ") for ln in lines if ln.startswith("event: ")]
    assert "primary_review_completed" in kinds
    assert "job_completed" in kinds


def test_sse_events_carry_ids_not_text(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = collect_sse(client, tmp_path, monkeypatch)
    payloads = [ln for ln in lines if ln.startswith("data: ")]
    assert payloads
    joined = "\n".join(payloads)
    assert "retrospective cohort" not in joined, "SSE must carry IDs, not document text"


def test_reconnect_refetch_no_duplicates(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path)
    job_id = res.json()["job_id"]
    first = wait_for_status(client, job_id, "completed")
    second = client.get(f"/api/jobs/{job_id}/state").json()
    ids_first = [c["element_id"] for c in first["elements"]]
    ids_second = [c["element_id"] for c in second["elements"]]
    assert ids_first == ids_second
    assert len(ids_second) == len(set(ids_second)), "no duplicate entries after refetch"


def test_pause_resume_controls(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_LLM_DELAY_SECONDS", "0.8")
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "delayed_completion")
    res = upload(client, tmp_path)
    job_id = res.json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/pause").status_code == 200
    deadline = time.monotonic() + 60
    state = client.get(f"/api/jobs/{job_id}/state").json()
    while time.monotonic() < deadline and state["job"]["running"]:
        time.sleep(0.3)
        state = client.get(f"/api/jobs/{job_id}/state").json()
    assert state["job"]["status"] in ("paused", "in_progress", "completed")
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "success")
    if state["job"]["status"] != "completed":
        assert client.post(f"/api/jobs/{job_id}/resume").status_code == 200
        wait_for_status(client, job_id, "completed")


def test_rebuild_endpoint(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path)
    job_id = res.json()["job_id"]
    wait_for_status(client, job_id, "completed")
    assert client.post(f"/api/jobs/{job_id}/rebuild").status_code == 200
    report = client.get(f"/api/jobs/{job_id}/report")
    assert "Run summary" in report.text


def test_delete_job(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path)
    job_id = res.json()["job_id"]
    wait_for_status(client, job_id, "completed")
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/state").status_code == 400


def test_job_list_endpoint(client: TestClient, tmp_path: Path) -> None:
    res = upload(client, tmp_path)
    job_id = res.json()["job_id"]
    wait_for_status(client, job_id, "completed")
    jobs = client.get("/api/jobs").json()["jobs"]
    entry = next(j for j in jobs if j["job_id"] == job_id)
    assert entry["status"] == "completed"
    assert entry["progress"]["total"] > 0


def test_server_restart_recovers_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new server process (new JobManager) finds existing jobs and can resume."""
    monkeypatch.setenv("ISAI_CLAUDE_COMMAND", json.dumps(mock_prefix("claude")))
    base = tmp_path / "appdata"
    first = JobManager(base_dir=base)
    app1 = create_app(token=TOKEN, port=PORT, manager=first)
    with TestClient(
        app1, base_url=f"http://127.0.0.1:{PORT}", headers={"X-IsAI-Token": TOKEN}
    ) as c1:
        res = upload(c1, tmp_path)
        job_id = res.json()["job_id"]
        wait_for_status(c1, job_id, "completed")

    # "Restart": a fresh manager over the same directory.
    second = JobManager(base_dir=base)
    app2 = create_app(token=TOKEN, port=PORT, manager=second)
    with TestClient(
        app2, base_url=f"http://127.0.0.1:{PORT}", headers={"X-IsAI-Token": TOKEN}
    ) as c2:
        jobs = c2.get("/api/jobs").json()["jobs"]
        entry = next(j for j in jobs if j["job_id"] == job_id)
        assert entry["status"] == "completed"
        state = c2.get(f"/api/jobs/{job_id}/state").json()
        assert state["elements"], "journal state recovered after restart"
        assert c2.get(f"/api/jobs/{job_id}/report").status_code == 200


# -- XSS safety --------------------------------------------------------------------------


def test_hostile_document_text_never_interpolated_into_html(
    client: TestClient, tmp_path: Path
) -> None:
    """Pages contain no document text at all (data flows only through JSON +
    textContent); hostile text therefore cannot become markup."""
    hostile = tmp_path / "hostile.docx"
    doc = Document()
    doc.add_paragraph(
        "<script>alert(1)</script><img src=x onerror=alert(2)> plus enough words "
        "to pass the minimum threshold for a stylistic review of this paragraph."
    )
    doc.save(str(hostile))
    res = client.post(
        "/api/jobs",
        files={"file": ("hostile.docx", hostile.read_bytes(), "application/octet-stream")},
        data={"provider": "claude", "min_words": "10"},
    )
    job_id = res.json()["job_id"]
    wait_for_status(client, job_id, "completed")

    page = client.get(f"/job/{job_id}")
    assert "<script>alert(1)</script>" not in page.text
    assert "onerror=alert" not in page.text

    detail_res = client.get(f"/api/jobs/{job_id}/state")
    assert detail_res.headers["content-type"].startswith("application/json")
