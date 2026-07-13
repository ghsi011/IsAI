"""Serializers turning journal rows into JSON payloads for the browser.

Text fields are sent as plain JSON strings; the frontend renders exclusively via
``textContent``/``createElement`` (no ``innerHTML`` with dynamic data), so
escaping happens at the DOM boundary.
"""

from __future__ import annotations

from typing import Any

from isai.docxio import DocElement
from isai.highlights import split_segments
from isai.persistence import Journal, TaskRole, TaskStatus
from isai.persistence.db import TaskRow


def _result_payload(task: TaskRow) -> dict[str, Any] | None:
    if task.result is None:
        return None
    payload = task.result.model_dump(mode="json")
    payload["provider"] = task.provider
    payload["agreement"] = task.agreement
    return payload


def element_card(element: DocElement, primary: TaskRow) -> dict[str, Any]:
    """The lightweight card entry used by the document pane list."""
    return {
        "element_id": element.element_id,
        "display_number": element.display_number,
        "kind": element.kind,
        "location": element.location,
        "style_name": element.style_name,
        "is_heading": element.is_heading,
        "nearest_heading": element.nearest_heading,
        "word_count": element.word_count,
        "status": primary.status.value,
        "style_signal": primary.result.style_signal.value if primary.result else None,
        "review_priority": primary.result.review_priority.value if primary.result else None,
        "needs_source_check": bool(
            primary.result
            and (
                any(c.requires_source_check for c in primary.result.citation_observations)
                or any(s.requires_source_check for s in primary.result.revision_suggestions)
            )
        ),
        "has_suggestions": bool(primary.result and primary.result.revision_suggestions),
        "agreement": primary.agreement,
        "error_category": primary.error_category,
    }


def job_state(journal: Journal, *, running: bool) -> dict[str, Any]:
    """Authoritative full state, fetched on connect/reconnect and after events."""
    meta = journal.meta()
    elements = journal.elements()
    primaries = {t.element_id: t for t in journal.tasks(TaskRole.PRIMARY)}
    cards = [element_card(e, primaries[e.element_id]) for e in elements]
    return {
        "job": {
            "job_id": meta.job_id,
            "source_filename": meta.source_filename,
            "status": (
                "analyzing" if running and meta.status.value == "in_progress" else meta.status.value
            ),
            "paused_reason": meta.paused_reason,
            "provider_mode": meta.provider_mode,
            "created_at": meta.created_at,
            "progress": journal.progress(),
            "running": running,
        },
        "elements": cards,
    }


def element_detail(journal: Journal, element_id: str) -> dict[str, Any]:
    """Full detail for one paragraph: text, highlights, segments, both results."""
    element = journal.element(element_id)
    primary = journal.task(element_id, TaskRole.PRIMARY)
    try:
        second = journal.task(element_id, TaskRole.SECOND_OPINION)
    except Exception:
        second = None

    highlights = [h.model_dump(mode="json") for h in primary.highlights]
    segments = [
        s.model_dump(mode="json") for s in split_segments(primary.highlights, len(element.text))
    ]
    detail: dict[str, Any] = {
        "element": {
            "element_id": element.element_id,
            "display_number": element.display_number,
            "location": element.location,
            "style_name": element.style_name,
            "nearest_heading": element.nearest_heading,
            "heading_path": element.heading_path,
            "text": element.text,
            "word_count": element.word_count,
        },
        "status": primary.status.value,
        "provider": primary.provider,
        "agreement": primary.agreement,
        "error_category": primary.error_category,
        "error_message": primary.error_message,
        "result": _result_payload(primary),
        "highlights": highlights,
        "segments": segments,
        "second_opinion": None,
    }
    if second is not None and second.status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
        detail["second_opinion"] = {
            "status": second.status.value,
            "provider": second.provider,
            "agreement": second.agreement,
            "error_category": second.error_category,
            "result": _result_payload(second),
        }
    return detail
