"""Quotation resolution: tiers, occurrence indices, and the no-wrong-match guarantee."""

from __future__ import annotations

from isai.textmatch import (
    MatchTier,
    find_occurrences,
    occurrence_count,
    resolve_quote,
    word_count,
)


def test_exact_match_wins() -> None:
    tier, spans = find_occurrences("alpha beta gamma", "beta")
    assert tier is MatchTier.EXACT
    assert spans == [(6, 10)]


def test_smart_quotes_resolve_at_unicode_tier() -> None:
    doc = "He said “hello world” yesterday."
    span = resolve_quote(doc, '"hello world"', None)
    assert span.tier is MatchTier.UNICODE
    assert doc[span.start : span.end] == "“hello world”"


def test_apostrophe_variant() -> None:
    doc = "the model’s output"
    span = resolve_quote(doc, "the model's output", None)
    assert span.tier is MatchTier.UNICODE
    assert doc[span.start : span.end] == doc


def test_en_dash_vs_hyphen() -> None:
    doc = "pages 10–20 were cited"
    span = resolve_quote(doc, "pages 10-20 were cited", None)
    assert span.tier is MatchTier.UNICODE


def test_whitespace_normalization_tier() -> None:
    doc = "results  were\n significant"
    span = resolve_quote(doc, "results were significant", None)
    assert span.tier is MatchTier.WHITESPACE
    assert span.start == 0 and span.end == len(doc)


def test_nbsp_matches_space() -> None:
    doc = "value added tax"
    span = resolve_quote(doc, "value added tax", None)
    assert span.tier in (MatchTier.UNICODE, MatchTier.WHITESPACE)


def test_repeated_text_occurrence_index() -> None:
    doc = "significant results. more significant results."
    assert occurrence_count(doc, "significant results") == 2
    first = resolve_quote(doc, "significant results", 1)
    second = resolve_quote(doc, "significant results", 2)
    assert first.start == 0
    assert second.start == 26
    assert doc[second.start : second.end] == "significant results"


def test_out_of_range_occurrence_is_unresolved_not_clamped() -> None:
    doc = "only one occurrence here"
    span = resolve_quote(doc, "occurrence", 3)
    assert span.tier is MatchTier.UNRESOLVED
    assert span.start == -1


def test_absent_text_is_unresolved() -> None:
    span = resolve_quote("some paragraph text", "fabricated evidence", None)
    assert span.tier is MatchTier.UNRESOLVED


def test_empty_and_whitespace_needles_unresolved() -> None:
    assert resolve_quote("text", "", None).tier is MatchTier.UNRESOLVED
    assert resolve_quote("text", "   ", None).tier is MatchTier.UNRESOLVED


def test_no_partial_word_false_positive_via_normalization() -> None:
    # The needle must not match a shorter, different string.
    doc = "the cat sat"
    span = resolve_quote(doc, "the cataclysm", None)
    assert span.tier is MatchTier.UNRESOLVED


def test_hebrew_text_resolves() -> None:
    doc = "המחקר הראה כי התוצאות היו מובהקות סטטיסטית."
    span = resolve_quote(doc, "התוצאות היו מובהקות", None)
    assert span.tier is MatchTier.EXACT
    assert doc[span.start : span.end] == "התוצאות היו מובהקות"


def test_combining_diacritics_tolerated() -> None:
    # Document uses decomposed e + combining acute; quote uses composed é.
    doc = "café culture"
    span = resolve_quote(doc, "café culture", None)
    assert span.tier is MatchTier.UNICODE
    assert span.start == 0


def test_ellipsis_equivalence() -> None:
    doc = "and so on… finally"
    span = resolve_quote(doc, "and so on... finally", None)
    assert span.tier is MatchTier.UNICODE


def test_word_count_unicode() -> None:
    assert word_count("שלום עולם hello") == 3
    assert word_count("") == 0
    assert word_count("   ") == 0
