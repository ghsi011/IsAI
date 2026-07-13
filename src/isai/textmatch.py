"""Local quotation resolution — the only trusted source of text offsets.

Provider models quote evidence text; they never supply offsets we believe. Every
quotation is resolved against the paragraph locally, in strictly decreasing
strictness, and a quotation that no tier can place is marked ``unresolved`` rather
than ever falling back to a wrong-occurrence match:

1. ``exact``      — byte-for-byte substring match.
2. ``unicode``    — conservative equivalences only: NFC composition, smart/straight
                    quote variants, hyphen/dash variants, NBSP≈space, ellipsis≈"...",
                    tolerance for combining diacritics.
3. ``whitespace`` — the unicode tier plus any whitespace run matches any other.
4. ``unresolved`` — no reliable placement exists.

All offsets returned are in the coordinates of the *original* paragraph text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# Conservative equivalence classes. Members of one class are treated as
# interchangeable at the `unicode` tier; anything not listed must match exactly.
_SINGLE_QUOTES = "'‘’‚′ʼ"
_DOUBLE_QUOTES = '"“”„″'
_DASHES = "-‐‑‒–—―−"
_SPACES = "     "
_COMBINING = "[̀-ͯ]*"

_CLASS_OF: dict[str, str] = {}
for _cls in (_SINGLE_QUOTES, _DOUBLE_QUOTES, _DASHES, _SPACES):
    _pattern = "[" + re.escape(_cls) + "]"
    for _ch in _cls:
        _CLASS_OF[_ch] = _pattern


class MatchTier(StrEnum):
    EXACT = "exact"
    UNICODE = "unicode"
    WHITESPACE = "whitespace"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedSpan:
    """A quotation placed in original-text coordinates."""

    tier: MatchTier
    start: int
    end: int


def word_count(text: str) -> int:
    """Whitespace-token count; language-agnostic (works for Hebrew/RTL)."""
    return len(text.split())


def find_exact_occurrences(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Non-overlapping left-to-right exact occurrences of ``needle``."""
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (idx := haystack.find(needle, start)) != -1:
        spans.append((idx, idx + len(needle)))
        start = idx + len(needle)
    return spans


def _char_pattern(ch: str) -> str:
    """Unicode-tier pattern for one (NFC) needle character."""
    if ch in _CLASS_OF:
        return _CLASS_OF[ch]
    if ch == "…":  # ellipsis matches three dots and vice versa
        return "(?:…|\\.\\.\\.)"
    escaped = re.escape(ch)
    if unicodedata.category(ch).startswith("L"):
        decomposed = unicodedata.normalize("NFD", ch)
        if decomposed != ch:
            # Match the composed form or its canonical decomposition.
            escaped = f"(?:{escaped}|{re.escape(decomposed)})"
        # Tolerate additional decomposed diacritics in the document text.
        return escaped + _COMBINING
    return escaped


def _build_pattern(needle: str, *, collapse_whitespace: bool) -> re.Pattern[str] | None:
    normalized = unicodedata.normalize("NFC", needle)
    if not normalized:
        return None
    parts: list[str] = []
    i = 0
    while i < len(normalized):
        ch = normalized[i]
        if collapse_whitespace and ch.isspace():
            parts.append("[\\s ]+")
            while i < len(normalized) and normalized[i].isspace():
                i += 1
            continue
        if ch == "." and normalized[i : i + 3] == "...":
            parts.append("(?:…|\\.\\.\\.)")
            i += 3
            continue
        parts.append(_char_pattern(ch))
        i += 1
    try:
        return re.compile("".join(parts))
    except re.error:  # pragma: no cover - defensive; classes are all static
        return None


def _regex_occurrences(haystack: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos <= len(haystack):
        m = pattern.search(haystack, pos)
        if m is None:
            break
        if m.end() == m.start():  # zero-width safety valve
            break
        spans.append((m.start(), m.end()))
        pos = m.end()
    return spans


def find_occurrences(haystack: str, needle: str) -> tuple[MatchTier, list[tuple[int, int]]]:
    """All occurrences of ``needle`` at the strictest tier that matches at all."""
    if not needle or not needle.strip():
        return MatchTier.UNRESOLVED, []
    exact = find_exact_occurrences(haystack, needle)
    if exact:
        return MatchTier.EXACT, exact
    for tier, collapse in ((MatchTier.UNICODE, False), (MatchTier.WHITESPACE, True)):
        pattern = _build_pattern(
            needle.strip() if collapse else needle, collapse_whitespace=collapse
        )
        if pattern is None:
            continue
        spans = _regex_occurrences(haystack, pattern)
        if spans:
            return tier, spans
    return MatchTier.UNRESOLVED, []


def occurrence_count(haystack: str, needle: str) -> int:
    """Occurrences at the strictest matching tier (0 when unresolved)."""
    _, spans = find_occurrences(haystack, needle)
    return len(spans)


def resolve_quote(haystack: str, needle: str, occurrence_index: int | None) -> ResolvedSpan:
    """Place one quotation. ``occurrence_index`` is 1-based; ``None`` means first.

    An out-of-range index is ``unresolved`` — never silently clamped to a wrong
    occurrence.
    """
    tier, spans = find_occurrences(haystack, needle)
    if tier is MatchTier.UNRESOLVED:
        return ResolvedSpan(MatchTier.UNRESOLVED, -1, -1)
    idx = 1 if occurrence_index is None else occurrence_index
    if idx < 1 or idx > len(spans):
        return ResolvedSpan(MatchTier.UNRESOLVED, -1, -1)
    start, end = spans[idx - 1]
    return ResolvedSpan(tier, start, end)


def contains_quote(haystack: str, needle: str) -> bool:
    """True when the quotation occurs in ``haystack`` at any reliable tier."""
    tier, _ = find_occurrences(haystack, needle)
    return tier is not MatchTier.UNRESOLVED
