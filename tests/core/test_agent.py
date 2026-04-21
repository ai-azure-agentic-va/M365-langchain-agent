"""Tests for agent helper functions (no LLM calls needed)."""

from m365_langchain_agent.core.agent import (
    _filter_cited_sources,
    _get_unique_source_names,
    _extract_section_label,
    _is_reasoning_model,
    Source,
)


def test_filter_cited_sources():
    sources = [
        Source(index=1, title="Doc A"),
        Source(index=2, title="Doc B"),
        Source(index=3, title="Doc C"),
    ]
    answer = "According to [1] and [3], the policy states..."
    result = _filter_cited_sources(answer, sources)
    assert len(result) == 2
    assert result[0]["index"] == 1
    assert result[1]["index"] == 3


def test_filter_no_citations_returns_all():
    sources = [Source(index=1, title="Doc A"), Source(index=2, title="Doc B")]
    answer = "The policy states that refunds are processed within 30 days."
    result = _filter_cited_sources(answer, sources)
    assert len(result) == 2


def test_unique_source_names():
    docs = [
        {"document_title": "Policy A", "file_name": "a.pdf"},
        {"document_title": "Policy B", "file_name": "b.pdf"},
        {"document_title": "Policy A", "file_name": "a.pdf"},
    ]
    names = _get_unique_source_names(docs)
    assert names == ["Policy A", "Policy B"]


def test_extract_section_label_sheet():
    content = "Sheet: RAW to INT\nField mappings..."
    label = _extract_section_label(content)
    assert label == "Sheet: RAW to INT"


def test_extract_section_label_heading():
    content = "# Refund Policy\nRefunds are processed..."
    label = _extract_section_label(content)
    assert label == "Section: Refund Policy"


def test_extract_section_label_none():
    content = "Just some text without any headers or sheet names."
    label = _extract_section_label(content)
    assert label is None


def test_is_reasoning_model():
    assert _is_reasoning_model("o3-mini")
    assert _is_reasoning_model("o1-preview")
    assert not _is_reasoning_model("gpt-4.1")
    assert not _is_reasoning_model("gpt-4.1-mini")
