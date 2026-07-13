"""Ordered DOCX content extraction with deterministic paragraph IDs.

Walks ``word/document.xml`` body children **in XML order**, descending into table
cells (and nested tables) where they occur — never "all paragraphs, then all
tables". Every visible paragraph becomes one :class:`DocElement`; empty paragraphs
are kept for location accounting but are never sent to a provider.

Not extracted (documented, never claimed as reviewed): text boxes and shapes
(``w:drawing``/``w:pict``), comments, footnotes/endnotes, headers/footers, tracked
deletions (``w:del``), embedded objects, and field instruction codes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Literal

from docx import Document as load_document
from docx.oxml.ns import qn
from pydantic import BaseModel, ConfigDict

from isai.errors import ErrorCategory, IsaiError
from isai.textmatch import word_count

EXTRACTOR_VERSION = "1"

#: Minimum words for a paragraph to serve as review context for its neighbors.
SUBSTANTIAL_MIN_WORDS = 10

NOT_EXTRACTED_FEATURES = (
    "text boxes and shapes",
    "comments",
    "footnotes and endnotes",
    "headers and footers",
    "tracked deletions",
    "embedded objects (charts, equations as OLE, images)",
)

_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_TR = qn("w:tr")
_W_TC = qn("w:tc")
_W_T = qn("w:t")
_W_BR = qn("w:br")
_W_CR = qn("w:cr")
_W_TAB = qn("w:tab")
_W_NO_BREAK_HYPHEN = qn("w:noBreakHyphen")
_W_PPR = qn("w:pPr")
_W_RPR = qn("w:rPr")
_W_DEL = qn("w:del")
_W_DRAWING = qn("w:drawing")
_W_PICT = qn("w:pict")
_W_OBJECT = qn("w:object")
_W_INSTR_TEXT = qn("w:instrText")
_W_PSTYLE = qn("w:pStyle")
_W_OUTLINE_LVL = qn("w:outlineLvl")
_W_TCPR = qn("w:tcPr")
_W_VMERGE = qn("w:vMerge")
_W_VAL = qn("w:val")
# python-docx's nsmap has no "mc" prefix; spell out the Clark name.
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

#: Subtrees whose text must not leak into paragraph text.
_SKIP_SUBTREES = frozenset({_W_PPR, _W_RPR, _W_DEL, _W_DRAWING, _W_PICT, _W_OBJECT, _MC_FALLBACK})

_HEADING_STYLE_RE = re.compile(r"^heading\s+(\d)$", re.IGNORECASE)


class ExtractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    include_tables: bool = True

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"extractor_version": EXTRACTOR_VERSION, "include_tables": self.include_tables},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DocElement(BaseModel):
    """One extracted paragraph (body or table cell), in document order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    order: int  # 0-based position among extracted elements
    display_number: int  # 1-based, what users see ("paragraph 12")
    kind: Literal["body", "table"]
    location: str  # "body" or "tbl1/r2/c3" (nested: "tbl1/r2/c3/tbl1/r1/c1")
    style_name: str
    is_heading: bool
    heading_level: int | None
    nearest_heading: str | None
    heading_path: list[str]
    text: str  # exact visible text
    normalized_text: str
    word_count: int
    char_count: int
    content_sha256: str
    prev_substantial_id: str | None = None
    next_substantial_id: str | None = None


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    elements: list[DocElement]
    config: ExtractionConfig
    extraction_fingerprint: str
    total_count: int
    body_count: int
    table_count: int
    empty_count: int
    heading_count: int
    not_extracted: tuple[str, ...] = NOT_EXTRACTED_FEATURES


def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _paragraph_text(p_el: object) -> str:
    parts: list[str] = []

    def walk(el: object) -> None:
        for child in el:  # type: ignore[attr-defined]
            tag = child.tag
            if tag in _SKIP_SUBTREES:
                continue
            if tag == _W_T:
                parts.append(child.text or "")
            elif tag in (_W_BR, _W_CR):
                parts.append("\n")
            elif tag == _W_TAB:
                parts.append("\t")
            elif tag == _W_NO_BREAK_HYPHEN:
                parts.append(chr(0x2011))  # non-breaking hyphen  # non-breaking hyphen
            elif tag == _W_INSTR_TEXT:
                continue
            else:
                walk(child)

    walk(p_el)
    return "".join(parts)


def _style_id(p_el: object) -> str | None:
    ppr = p_el.find(_W_PPR)  # type: ignore[attr-defined]
    if ppr is None:
        return None
    pstyle = ppr.find(_W_PSTYLE)
    if pstyle is None:
        return None
    return pstyle.get(_W_VAL)


def _outline_level(p_el: object) -> int | None:
    ppr = p_el.find(_W_PPR)  # type: ignore[attr-defined]
    if ppr is None:
        return None
    lvl = ppr.find(_W_OUTLINE_LVL)
    if lvl is None:
        return None
    raw = lvl.get(_W_VAL)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _heading_level(style_name: str, p_el: object) -> int | None:
    match = _HEADING_STYLE_RE.match(style_name)
    if match:
        return int(match.group(1))
    if style_name.lower() == "title":
        return 1
    outline = _outline_level(p_el)
    if outline is not None and 0 <= outline <= 8:
        return outline + 1
    return None


def _is_vmerge_continue(tc_el: object) -> bool:
    tcpr = tc_el.find(_W_TCPR)  # type: ignore[attr-defined]
    if tcpr is None:
        return False
    vmerge = tcpr.find(_W_VMERGE)
    if vmerge is None:
        return False
    val = vmerge.get(_W_VAL)
    return val is None or val == "continue"


def _iter_paragraphs(
    container_el: object, loc_prefix: str, include_tables: bool
) -> Iterator[tuple[str, object]]:
    """Yield ``(location, w:p element)`` in true document order."""
    tbl_index = 0
    for child in container_el.iterchildren():  # type: ignore[attr-defined]
        if child.tag == _W_P:
            yield (loc_prefix or "body", child)
        elif child.tag == _W_TBL:
            tbl_index += 1
            if not include_tables:
                continue
            for row_num, tr in enumerate(child.findall(_W_TR), start=1):
                for cell_num, tc in enumerate(tr.findall(_W_TC), start=1):
                    if _is_vmerge_continue(tc):
                        continue
                    prefix = f"{loc_prefix}/" if loc_prefix else ""
                    cell_loc = f"{prefix}tbl{tbl_index}/r{row_num}/c{cell_num}"
                    yield from _iter_paragraphs(tc, cell_loc, include_tables)


def _style_name_map(document: object) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for style in document.styles:  # type: ignore[attr-defined]
            style_id = getattr(style, "style_id", None)
            name = getattr(style, "name", None)
            if style_id and name:
                names[style_id] = name
    except (AttributeError, KeyError):  # pragma: no cover - defensive
        pass
    return names


def extract_document(path: object, config: ExtractionConfig | None = None) -> ExtractionResult:
    """Extract all reviewable elements from a safety-validated DOCX file."""
    config = config or ExtractionConfig()
    try:
        document = load_document(str(path))
    except IsaiError:
        raise
    except Exception as exc:
        raise IsaiError(
            ErrorCategory.DOCUMENT,
            "failed to parse DOCX structure; the file may be corrupt "
            "(re-save it in Word and retry)",
        ) from exc

    style_names = _style_name_map(document)
    body = document.element.body

    heading_stack: list[tuple[int, str]] = []
    elements: list[DocElement] = []

    for order, (location, p_el) in enumerate(_iter_paragraphs(body, "", config.include_tables)):
        text = _paragraph_text(p_el)
        normalized = _normalize_text(text)
        style_id = _style_id(p_el)
        style_name = style_names.get(style_id or "", style_id or "Normal")
        level = _heading_level(style_name, p_el)
        is_heading = level is not None and bool(normalized)

        if is_heading and level is not None:
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, normalized))

        digest_basis = f"{location}|{normalized}".encode()
        short_hash = hashlib.sha256(digest_basis).hexdigest()[:8]
        elements.append(
            DocElement(
                element_id=f"p-{order:06d}-{short_hash}",
                order=order,
                display_number=order + 1,
                kind="body" if location == "body" else "table",
                location=location,
                style_name=style_name,
                is_heading=is_heading,
                heading_level=level if is_heading else None,
                nearest_heading=heading_stack[-1][1] if heading_stack else None,
                heading_path=[title for _, title in heading_stack],
                text=text,
                normalized_text=normalized,
                word_count=word_count(text),
                char_count=len(text),
                content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )

    elements = _link_substantial_neighbors(elements)
    return ExtractionResult(
        elements=elements,
        config=config,
        extraction_fingerprint=config.fingerprint(),
        total_count=len(elements),
        body_count=sum(1 for e in elements if e.kind == "body"),
        table_count=sum(1 for e in elements if e.kind == "table"),
        empty_count=sum(1 for e in elements if not e.normalized_text),
        heading_count=sum(1 for e in elements if e.is_heading),
    )


def _link_substantial_neighbors(elements: list[DocElement]) -> list[DocElement]:
    """Fill prev/next substantial-paragraph IDs (context-assist candidates)."""

    def substantial(e: DocElement) -> bool:
        return not e.is_heading and e.word_count >= SUBSTANTIAL_MIN_WORDS

    linked: list[DocElement] = []
    prev_id: str | None = None
    prev_ids: list[str | None] = []
    for e in elements:
        prev_ids.append(prev_id)
        if substantial(e):
            prev_id = e.element_id

    next_id: str | None = None
    next_ids: list[str | None] = [None] * len(elements)
    for i in range(len(elements) - 1, -1, -1):
        next_ids[i] = next_id
        if substantial(elements[i]):
            next_id = elements[i].element_id

    for e, prev, nxt in zip(elements, prev_ids, next_ids, strict=True):
        linked.append(
            e.model_copy(update={"prev_substantial_id": prev, "next_substantial_id": nxt})
        )
    return linked
