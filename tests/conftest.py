"""Shared test fixtures."""

import os

import pytest


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch):
    """Ensure required env vars are set for Settings validation."""
    defaults = {
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_SEARCH_ENDPOINT": "https://test.search.windows.net",
        "AZURE_SEARCH_INDEX_NAME": "test-index",
        "AZURE_COSMOS_ENDPOINT": "https://test.documents.azure.com:443",
    }
    for key, val in defaults.items():
        monkeypatch.setenv(key, os.environ.get(key, val))
