"""Markdown rendering — pure functions of journal data.

The same functions produce the live incrementally-appended report and the
``rebuild`` output, which is what makes rebuild deterministic. Document and
provider text is always passed through :func:`md_escape` so headings, fences,
backticks, HTML comments, and table pipes in untrusted content cannot alter the
report's structure.
"""

from __future__ import annotations

from isai.docxio import DocElement
from isai.models import ReviewResult
from isai.persistence.db import JobMeta, TaskRow, TaskStatus

DISCLAIMER = (
    "> **What this report is.** IsAI screens for **AI-associated stylistic patterns** "
    "and academic-writing quality. It **cannot determine authorship**: no stylistic "
    "analysis can prove that a passage was or was not written with AI assistance, and "
    "nothing in this report should be read as such a claim. Signals describe observable "
    "style only; manual review is always required."
)

_ESCAPE_MAP = {
    "\\": "\\\\",
    "`": "\\`",
    "*": "\\*",
    "_": "\\_",
    "#": "\\#",
    "|": "\\|",
    "<": "&lt;",
    ">": "&gt;",
    "[": "\\[",
    "]": "\\]",
}


def md_escape(text: str) -> str:
    """Neutralize Markdown/HTML structure in untrusted text (single-line safe)."""
    out = []
    for ch in text:
        out.append(_ESCAPE_MAP.get(ch, ch))
    return "".join(out)


def md_escape_block(text: str) -> str:
    """Escape untrusted multi-line text for quoting in the report."""
    lines = text.splitlines() or [""]
    return "\n".join("> " + md_escape(line) for line in lines)


def result_marker(element_id: str, role: str, content_sha256: str) -> str:
    """Unique, greppable section marker. IDs and hashes only — never text."""
    return f"[//]: # (isai:result element={element_id} role={role} sha={content_sha256})"


MARKER_PREFIX = "[//]: # (isai:result "


def render_header(meta: JobMeta, total_elements: int, reviewable: int) -> str:
    lines = [
        "# IsAI style review",
        "",
        DISCLAIMER,
        "",
        f"- **Source:** {md_escape(meta.source_filename)}",
        f"- **Source SHA-256:** `{meta.source_sha256}`",
        f"- **Started:** {meta.created_at}",
        f"- **Provider mode:** {meta.provider_mode}",
        f"- **Prompt version:** {meta.prompt_version}",
        f"- **Elements extracted:** {total_elements} ({reviewable} reviewable)",
        f"- **Job ID:** `{meta.job_id}`",
        "- **Status:** in progress — sections are appended as each paragraph completes;",
        "  this file is safe to read mid-run.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def _render_indicator_lists(result: ReviewResult) -> list[str]:
    lines: list[str] = []
    if result.indicators:
        lines.append("**Indicators (AI-associated style):**")
        for ind in result.indicators:
            quote = f' — "{md_escape(ind.evidence)}"' if ind.evidence else ""
            occ = (
                f" (occurrence {ind.occurrence_index})"
                if ind.occurrence_index and ind.occurrence_index > 1
                else ""
            )
            lines.append(f"- *{ind.category.value}*{quote}{occ}: {md_escape(ind.explanation)}")
        lines.append("")
    if result.counter_indicators:
        lines.append("**Counter-indicators (natural/specific writing):**")
        for ind in result.counter_indicators:
            quote = f' — "{md_escape(ind.evidence)}"' if ind.evidence else ""
            lines.append(f"- *{ind.category.value}*{quote}: {md_escape(ind.explanation)}")
        lines.append("")
    if result.quality_issues:
        lines.append("**Writing-quality issues:**")
        for qi in result.quality_issues:
            quote = f' — "{md_escape(qi.target_text)}"' if qi.target_text else ""
            lines.append(f"- *{qi.category.value}*{quote}: {md_escape(qi.description)}")
        lines.append("")
    if result.citation_observations:
        lines.append("**Citation observations:**")
        for co in result.citation_observations:
            check = " ⚠ requires source check" if co.requires_source_check else ""
            quote = f' — "{md_escape(co.target_text)}"' if co.target_text else ""
            lines.append(f"- {md_escape(co.observation)}{quote}{check}")
        lines.append("")
    return lines


def _render_result_body(result: ReviewResult) -> list[str]:
    lines: list[str] = []
    lines.append(
        f"**Style signal:** {result.style_signal.value} · "
        f"**Confidence in observations:** {result.assessment_confidence.value} · "
        f"**Review priority:** {result.review_priority.value}"
    )
    lines.append("")
    lines.append(md_escape(result.summary))
    lines.append("")
    lines.extend(_render_indicator_lists(result))
    if result.revision_suggestions:
        lines.append("**Revision suggestions:**")
        for rs in result.revision_suggestions:
            target = (
                f'For "{md_escape(rs.target_text)}": ' if rs.target_text else "Whole paragraph: "
            )
            lines.append(f"- {target}{md_escape(rs.issue)} → {md_escape(rs.recommended_change)}")
            if rs.proposed_replacement:
                lines.append(f'  - Proposed wording: "{md_escape(rs.proposed_replacement)}"')
            lines.append(f"  - Why: {md_escape(rs.reason)}")
            if rs.requires_source_check:
                lines.append("  - ⚠ Verify against the cited source before applying.")
        lines.append("")
    else:
        lines.append("**Revision suggestions:** none — no substantial revision recommended.")
        lines.append("")
    if result.manual_checks:
        lines.append("**Manual checks:**")
        lines.extend(f"- {md_escape(mc)}" for mc in result.manual_checks)
        lines.append("")
    lines.append(f"*Limitations:* {md_escape(result.limitations_note)}")
    return lines


def render_task_section(element: DocElement, task: TaskRow) -> str:
    """One paragraph's report section (primary or second opinion)."""
    heading_note = (
        f" — under “{md_escape(element.nearest_heading)}”" if element.nearest_heading else ""
    )
    role_label = "Second opinion" if task.role.value == "second_opinion" else "Paragraph"
    title = f"## {role_label} {element.display_number}{heading_note}"

    marker = result_marker(element.element_id, task.role.value, element.content_sha256)
    lines = [marker, "", title, ""]
    lines.append(f"*Location:* `{element.location}` · *Style:* {md_escape(element.style_name)}")
    lines.append("")
    lines.append("**Text:**")
    lines.append("")
    lines.append(md_escape_block(element.text))
    lines.append("")

    if task.status is TaskStatus.COMPLETED and task.result is not None:
        provider_note = f"*Reviewed by:* {task.provider}"
        if task.agreement:
            provider_note += f" · *Consensus:* {task.agreement}"
        lines.append(provider_note)
        lines.append("")
        lines.extend(_render_result_body(task.result))
    elif task.status is TaskStatus.ERROR:
        lines.append(
            f"**Review error** ({task.error_category}): {md_escape(task.error_message or '')}"
        )
        lines.append("")
        lines.append("The run continued with the next paragraph; re-run with resume to retry.")
    elif task.status is TaskStatus.SKIPPED:
        lines.append("*Skipped (empty or excluded element).*")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def render_summary(meta: JobMeta, tasks: list[TaskRow]) -> str:
    primary = [t for t in tasks if t.role.value == "primary"]
    completed = sum(1 for t in primary if t.status is TaskStatus.COMPLETED)
    errors = sum(1 for t in primary if t.status is TaskStatus.ERROR)
    skipped = sum(1 for t in primary if t.status is TaskStatus.SKIPPED)
    signals: dict[str, int] = {}
    for t in primary:
        if t.result is not None:
            signals[t.result.style_signal.value] = signals.get(t.result.style_signal.value, 0) + 1
    signal_line = ", ".join(f"{k}: {v}" for k, v in sorted(signals.items())) if signals else "none"
    lines = [
        "## Run summary",
        "",
        f"- **Job:** `{meta.job_id}` — {meta.status.value}",
        f"- **Reviewed:** {completed} paragraph(s); errors: {errors}; skipped: {skipped}",
        f"- **Style signals:** {signal_line}",
        "",
        DISCLAIMER,
        "",
    ]
    return "\n".join(lines)
