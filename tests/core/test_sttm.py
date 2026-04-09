"""Tests for STTM query detection and hop parsing."""

from m365_langchain_agent.core.sttm import is_sttm_query, detect_hops


def test_sttm_keyword_detection():
    assert is_sttm_query("What is the STTM for this table?")
    assert is_sttm_query("Show me the data lineage")
    assert is_sttm_query("raw to int mapping for customer table")
    assert not is_sttm_query("What is the refund policy?")
    assert not is_sttm_query("Hello")


def test_detect_explicit_hops():
    hops = detect_hops("Show me the raw to int mapping")
    assert hops == [("raw", "int")]


def test_detect_multiple_hops():
    hops = detect_hops("raw to int and int to cur mapping")
    assert ("raw", "int") in hops
    assert ("int", "cur") in hops


def test_detect_multihop_signals():
    hops = detect_hops("Show end-to-end lineage for this field")
    assert len(hops) == 4  # all hops


def test_no_hops_for_general_sttm():
    hops = detect_hops("What is STTM?")
    assert hops == []
