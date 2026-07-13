"""The single Playwright smoke test (§10): drag-drop → live results → highlight
click → annotation focus → report download. Synthetic fixtures + mock providers
only; no traces are recorded."""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from scripts.generate_docx_fixtures import build_thesis

from isai.web.jobs import JobManager
from isai.web.server import create_app
from tests.conftest import mock_prefix

playwright_sync = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.playwright_smoke, pytest.mark.e2e]

TOKEN = "smoke-test-token"


@pytest.fixture()
def gui_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("ISAI_CLAUDE_COMMAND", json.dumps(mock_prefix("claude")))
    monkeypatch.setenv("ISAI_CODEX_COMMAND", json.dumps(mock_prefix("codex")))
    monkeypatch.setenv("MOCK_LLM_SCENARIO", "delayed_completion")
    monkeypatch.setenv("MOCK_LLM_DELAY_SECONDS", "0.5")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(token=TOKEN, port=port, manager=JobManager(base_dir=tmp_path / "appdata"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert server.started, "uvicorn failed to start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.timeout(300)
def test_gui_smoke(gui_server: str, tmp_path: Path) -> None:
    docx = build_thesis(tmp_path / "smoke-thesis.docx", paragraphs=3)
    encoded = base64.b64encode(docx.read_bytes()).decode("ascii")

    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{gui_server}/?token={TOKEN}")
        assert "IsAI" in page.title()

        # Fixture paragraphs are ~45 words; lower the threshold so they get a
        # full review (with indicators → highlights) instead of short-paragraph
        # handling.
        page.fill('input[name="min_words"]', "10")

        # Drag-and-drop the synthetic DOCX onto the dropzone.
        page.evaluate(
            """([name, b64]) => {
                const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
                const file = new File([bytes], name, {
                    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                });
                const dt = new DataTransfer();
                dt.items.add(file);
                const dz = document.getElementById('dropzone');
                dz.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true }));
            }""",
            ["smoke-thesis.docx", encoded],
        )
        # Upload redirects to the job page; analysis starts with mock providers.
        page.wait_for_url("**/job/**", timeout=30_000)

        # Paragraph results appear live (SSE + refetch).
        page.wait_for_selector(".card .chip[class*='signal-']", timeout=120_000)

        # Click a highlighted phrase → its annotation focuses.
        page.wait_for_selector("mark.hl", timeout=120_000)
        page.click("mark.hl")
        page.wait_for_selector(".annotation.focused", timeout=30_000)

        # Clicking the focused annotation re-emphasizes the span (both directions).
        page.click(".annotation.focused")
        page.wait_for_selector("mark.hl.focused", timeout=30_000)

        # Download the report.
        with page.expect_download(timeout=60_000) as download_info:
            page.click("#btn-report")
        download = download_info.value
        target = tmp_path / "downloaded-report.md"
        download.save_as(str(target))
        content = target.read_text(encoding="utf-8")
        assert "cannot determine authorship" in content

        browser.close()
