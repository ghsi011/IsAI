"""DOCX container validation — runs before any XML parsing.

A .docx is a ZIP archive; a hostile one can carry ZIP bombs, path traversal names,
or encrypted members. Everything here raises :class:`IsaiError` with category
``document`` and an actionable, log-safe message (file names of archive members are
structural, not document text, and may appear in errors).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from isai.errors import ErrorCategory, IsaiError

ZIP_LOCAL_HEADER = b"PK\x03\x04"
_ENCRYPTED_FLAG = 0x1


@dataclass(frozen=True)
class DocxSafetyLimits:
    """Caps chosen for long theses with headroom; a weak laptop must stay usable."""

    max_file_bytes: int = 200 * 1024 * 1024
    max_total_uncompressed_bytes: int = 600 * 1024 * 1024
    max_entry_uncompressed_bytes: int = 300 * 1024 * 1024
    max_document_xml_bytes: int = 100 * 1024 * 1024
    max_entries: int = 10_000
    #: Ratio cap applies only past this size — tiny files legitimately compress well.
    ratio_check_threshold_bytes: int = 1024 * 1024
    max_compression_ratio: float = 200.0


def _reject(message: str) -> IsaiError:
    return IsaiError(ErrorCategory.DOCUMENT, message)


def _entry_name_is_unsafe(name: str) -> bool:
    if "\\" in name:  # DOCX members always use forward slashes
        return True
    path = PurePosixPath(name)
    if path.is_absolute():
        return True
    parts = path.parts
    return ".." in parts or any(len(p) >= 2 and p[1] == ":" for p in parts[:1])


def _check_entry(info: zipfile.ZipInfo, limits: DocxSafetyLimits) -> None:
    if info.flag_bits & _ENCRYPTED_FLAG:
        raise _reject(
            "document is password-protected/encrypted; remove the password in Word and try again"
        )
    if _entry_name_is_unsafe(info.filename):
        raise _reject("archive contains an unsafe member path; refusing to parse")
    if info.file_size > limits.max_entry_uncompressed_bytes:
        raise _reject(
            f"archive member '{info.filename}' expands to "
            f"{info.file_size // (1024 * 1024)} MB, above the safety cap"
        )
    if (
        info.file_size > limits.ratio_check_threshold_bytes
        and info.compress_size > 0
        and info.file_size / info.compress_size > limits.max_compression_ratio
    ):
        raise _reject(
            "archive member has a suspicious compression ratio "
            "(possible ZIP bomb); refusing to parse"
        )


def _check_docx_structure(zf: zipfile.ZipFile, names: set[str], limits: DocxSafetyLimits) -> None:
    if "[Content_Types].xml" not in names:
        raise _reject("not a valid DOCX (missing [Content_Types].xml)")
    if "word/document.xml" not in names:
        raise _reject(
            "not a usable DOCX (missing word/document.xml); encrypted OOXML "
            "packages and non-Word archives are not supported"
        )
    doc_info = zf.getinfo("word/document.xml")
    if doc_info.file_size > limits.max_document_xml_bytes:
        raise _reject(
            "word/document.xml expands to "
            f"{doc_info.file_size // (1024 * 1024)} MB, above the safety cap"
        )
    # Word never writes DTDs; their presence signals entity-expansion attacks.
    with zf.open("word/document.xml") as doc_stream:
        head = doc_stream.read(8192)
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise _reject("document XML contains a DTD; refusing to parse")


def validate_docx_container(path: Path, limits: DocxSafetyLimits | None = None) -> None:
    """Validate the file at ``path`` as a safe, plausible DOCX container.

    Raises :class:`IsaiError` (category ``document``) on any problem; returns
    silently when the container looks safe to parse.
    """
    limits = limits or DocxSafetyLimits()

    if not path.is_file():
        raise _reject(f"input file not found: {path.name}")
    if path.suffix.lower() != ".docx":
        raise _reject(
            f"unsupported file type '{path.suffix or '(none)'}': only .docx is supported "
            "(convert .doc/.pdf/.odt in Word first)"
        )
    size = path.stat().st_size
    if size == 0:
        raise _reject("file is empty")
    if size > limits.max_file_bytes:
        raise _reject(
            f"file is {size // (1024 * 1024)} MB, above the "
            f"{limits.max_file_bytes // (1024 * 1024)} MB safety cap"
        )

    with path.open("rb") as fh:
        signature = fh.read(4)
    if signature != ZIP_LOCAL_HEADER:
        raise _reject(
            "not a DOCX file (missing ZIP signature); if this document came from an "
            "older Word version, re-save it as .docx"
        )

    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > limits.max_entries:
                raise _reject(
                    f"archive has {len(infos)} entries, above the {limits.max_entries} cap"
                )
            total_uncompressed = 0
            names = set()
            for info in infos:
                _check_entry(info, limits)
                total_uncompressed += info.file_size
                names.add(info.filename)
            if total_uncompressed > limits.max_total_uncompressed_bytes:
                raise _reject(
                    "archive expands to "
                    f"{total_uncompressed // (1024 * 1024)} MB total, above the safety cap"
                )
            _check_docx_structure(zf, names, limits)
    except zipfile.BadZipFile as exc:
        raise _reject("file is corrupt or not a ZIP archive; re-save the document in Word") from exc
