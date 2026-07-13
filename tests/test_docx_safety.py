"""Container validation: corrupt, encrypted, traversal, bombs, DTDs."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest
from scripts.generate_docx_fixtures import build_simple

from isai.docxio import DocxSafetyLimits, validate_docx_container
from isai.errors import ErrorCategory, IsaiError


@pytest.fixture(scope="module")
def valid_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_simple(tmp_path_factory.mktemp("docx") / "valid.docx")


def expect_document_error(path: Path, fragment: str, limits: DocxSafetyLimits | None = None):
    with pytest.raises(IsaiError) as exc_info:
        validate_docx_container(path, limits)
    assert exc_info.value.category is ErrorCategory.DOCUMENT
    assert fragment in exc_info.value.message
    return exc_info.value


def test_valid_docx_passes(valid_docx: Path) -> None:
    validate_docx_container(valid_docx)


def test_missing_file_rejected(tmp_path: Path) -> None:
    expect_document_error(tmp_path / "nope.docx", "not found")


def test_wrong_extension_rejected(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "doc.pdf"
    target.write_bytes(valid_docx.read_bytes())
    expect_document_error(target, "only .docx")


def test_empty_file_rejected(tmp_path: Path) -> None:
    target = tmp_path / "empty.docx"
    target.write_bytes(b"")
    expect_document_error(target, "empty")


def test_not_a_zip_rejected(tmp_path: Path) -> None:
    target = tmp_path / "fake.docx"
    target.write_bytes(b"This is not a zip file at all, just text." * 10)
    expect_document_error(target, "ZIP signature")


def test_truncated_zip_rejected(tmp_path: Path, valid_docx: Path) -> None:
    data = valid_docx.read_bytes()
    target = tmp_path / "truncated.docx"
    target.write_bytes(data[: len(data) // 2])
    expect_document_error(target, "corrupt")


def test_oversized_file_rejected(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "big.docx"
    target.write_bytes(valid_docx.read_bytes())
    limits = DocxSafetyLimits(max_file_bytes=100)
    expect_document_error(target, "safety cap", limits)


def test_encrypted_entry_rejected(tmp_path: Path, valid_docx: Path) -> None:
    # Flip the encryption bit in the first local file header.
    data = bytearray(valid_docx.read_bytes())
    flags = struct.unpack_from("<H", data, 6)[0]
    struct.pack_into("<H", data, 6, flags | 0x1)
    # Also flip it in the matching central directory record so both agree.
    cd = data.find(b"PK\x01\x02")
    assert cd != -1
    cd_flags = struct.unpack_from("<H", data, cd + 8)[0]
    struct.pack_into("<H", data, cd + 8, cd_flags | 0x1)
    target = tmp_path / "encrypted.docx"
    target.write_bytes(bytes(data))
    expect_document_error(target, "password")


def test_path_traversal_entry_rejected(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "traversal.docx"
    target.write_bytes(valid_docx.read_bytes())
    with zipfile.ZipFile(target, "a") as zf:
        zf.writestr("../../evil.txt", "escape attempt")
    expect_document_error(target, "unsafe member path")


def test_zip_bomb_ratio_rejected(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "bomb.docx"
    target.write_bytes(valid_docx.read_bytes())
    with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/media/huge.bin", b"\x00" * (8 * 1024 * 1024))
    limits = DocxSafetyLimits(ratio_check_threshold_bytes=1024, max_compression_ratio=50.0)
    expect_document_error(target, "compression ratio", limits)


def test_total_uncompressed_cap(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "total.docx"
    target.write_bytes(valid_docx.read_bytes())
    limits = DocxSafetyLimits(max_total_uncompressed_bytes=1024)
    expect_document_error(target, "safety cap", limits)


def test_too_many_entries_rejected(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "many.docx"
    target.write_bytes(valid_docx.read_bytes())
    limits = DocxSafetyLimits(max_entries=3)
    expect_document_error(target, "entries", limits)


def test_missing_document_xml_rejected(tmp_path: Path) -> None:
    target = tmp_path / "hollow.docx"
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/other.xml", "<x/>")
    expect_document_error(target, "word/document.xml")


def test_dtd_in_document_xml_rejected(tmp_path: Path) -> None:
    target = tmp_path / "dtd.docx"
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "b">]><doc/>',
        )
    expect_document_error(target, "DTD")


def test_unicode_filename_accepted(tmp_path: Path, valid_docx: Path) -> None:
    target = tmp_path / "עבודת גמר – סופי.docx"
    target.write_bytes(valid_docx.read_bytes())
    validate_docx_container(target)
