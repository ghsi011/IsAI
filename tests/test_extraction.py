"""Extraction: true document order, deterministic IDs, merged/nested cells, Unicode."""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from scripts.generate_docx_fixtures import (
    HEBREW,
    SMART_PUNCTUATION,
    build_nested_and_merged,
    build_simple,
    build_unicode,
    build_with_hyperlink_and_breaks,
)

from isai.docxio import ExtractionConfig, ExtractionResult, extract_document


@pytest.fixture(scope="module")
def fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fixtures")


@pytest.fixture(scope="module")
def simple(fixtures_dir: Path) -> ExtractionResult:
    return extract_document(build_simple(fixtures_dir / "simple.docx"))


def test_table_interleaved_in_document_order(simple: ExtractionResult) -> None:
    texts = [e.normalized_text for e in simple.elements]
    before = next(i for i, t in enumerate(texts) if t.startswith("Before-table"))
    variable = texts.index("Variable")
    after = next(i for i, t in enumerate(texts) if t.startswith("After-table"))
    assert before < variable < after, "table cells must appear between surrounding paragraphs"


def test_kinds_and_locations(simple: ExtractionResult) -> None:
    cell = next(e for e in simple.elements if e.normalized_text == "Variable")
    assert cell.kind == "table"
    assert cell.location == "tbl1/r1/c1"
    body = next(e for e in simple.elements if e.normalized_text.startswith("Before-table"))
    assert body.kind == "body"
    assert body.location == "body"


def test_orders_are_sequential_and_display_numbers_1_based(simple: ExtractionResult) -> None:
    assert [e.order for e in simple.elements] == list(range(len(simple.elements)))
    assert [e.display_number for e in simple.elements] == [e.order + 1 for e in simple.elements]


def test_deterministic_ids_across_extractions(fixtures_dir: Path) -> None:
    path = fixtures_dir / "same.docx"
    build_simple(path)
    first = extract_document(path)
    second = extract_document(path)
    assert [e.element_id for e in first.elements] == [e.element_id for e in second.elements]
    assert first.extraction_fingerprint == second.extraction_fingerprint


def test_changed_content_changes_id_and_hash(tmp_path: Path) -> None:
    a_path, b_path = tmp_path / "a.docx", tmp_path / "b.docx"
    for path, text in ((a_path, "Original wording here."), (b_path, "Changed wording here.")):
        doc = Document()
        doc.add_paragraph(text)
        doc.save(str(path))
    a = extract_document(a_path).elements[0]
    b = extract_document(b_path).elements[0]
    assert a.element_id != b.element_id
    assert a.content_sha256 != b.content_sha256


def test_merged_cells_not_duplicated(fixtures_dir: Path) -> None:
    result = extract_document(build_nested_and_merged(fixtures_dir / "nested.docx"))
    labels = [e.normalized_text for e in result.elements if e.normalized_text == "Merged label"]
    assert labels == ["Merged label"], "vertically merged cell must appear exactly once"


def test_nested_table_extracted_with_path(fixtures_dir: Path) -> None:
    result = extract_document(build_nested_and_merged(fixtures_dir / "nested2.docx"))
    nested = next(e for e in result.elements if e.normalized_text == "Nested cell content")
    assert nested.location.count("tbl") == 2, nested.location
    all_texts = [e.normalized_text for e in result.elements]
    assert all_texts.count("Nested cell content") == 1


def test_exclude_tables_config(fixtures_dir: Path) -> None:
    path = build_simple(fixtures_dir / "exclude.docx")
    with_tables = extract_document(path)
    without = extract_document(path, ExtractionConfig(include_tables=False))
    assert without.table_count == 0
    assert with_tables.table_count > 0
    assert without.extraction_fingerprint != with_tables.extraction_fingerprint


def test_headings_tracked(simple: ExtractionResult) -> None:
    methods_para = next(e for e in simple.elements if e.normalized_text.startswith("Before-table"))
    assert methods_para.nearest_heading == "Methods"
    stats = next(e for e in simple.elements if e.normalized_text.startswith("Analyses used R"))
    assert stats.heading_path == ["Methods", "Statistical analysis"]
    heading = next(e for e in simple.elements if e.normalized_text == "Introduction")
    assert heading.is_heading and heading.heading_level == 1


def test_empty_paragraph_kept_but_flagged(simple: ExtractionResult) -> None:
    assert simple.empty_count >= 1
    empty = next(e for e in simple.elements if not e.normalized_text)
    assert empty.word_count == 0


def test_hebrew_and_smart_punctuation_preserved(fixtures_dir: Path) -> None:
    result = extract_document(build_unicode(fixtures_dir / "unicode.docx"))
    texts = [e.text for e in result.elements]
    assert any(HEBREW in t for t in texts)
    assert any(SMART_PUNCTUATION in t for t in texts), "smart quotes/dashes must survive"
    assert any("∆H = −41.8" in t for t in texts)


def test_hyperlink_text_and_line_breaks(fixtures_dir: Path) -> None:
    result = extract_document(build_with_hyperlink_and_breaks(fixtures_dir / "hyper.docx"))
    para = result.elements[0]
    assert "ClinicalTrials.gov entry" in para.text, "visible hyperlink text must be kept"
    assert "\n" in para.text, "explicit line break must be preserved"
    assert "Second visual line" in para.text


def test_substantial_neighbors_linked(simple: ExtractionResult) -> None:
    substantial = [e for e in simple.elements if not e.is_heading and e.word_count >= 10]
    assert len(substantial) >= 3
    middle = substantial[1]
    assert middle.prev_substantial_id == substantial[0].element_id
    assert middle.next_substantial_id == substantial[2].element_id
    assert substantial[0].prev_substantial_id is None


def test_unicode_path_supported(tmp_path: Path) -> None:
    path = build_simple(tmp_path / "מסמך בדיקה.docx")
    result = extract_document(path)
    assert result.total_count > 0
