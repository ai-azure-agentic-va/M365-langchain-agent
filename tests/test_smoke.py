"""Smoke tests — verify imports and basic structures work."""


def test_package_import():
    import m365_langchain_agent
    assert m365_langchain_agent.__version__


def test_config_loads():
    from m365_langchain_agent.config import settings
    assert settings.port > 0
    assert settings.azure_openai_endpoint


def test_exceptions_hierarchy():
    from m365_langchain_agent.exceptions import (
        AgentError, RetrievalError, GenerationError,
        AuthenticationError, ConfigurationError, CosmosError,
    )
    assert issubclass(RetrievalError, AgentError)
    assert issubclass(GenerationError, AgentError)
    assert issubclass(CosmosError, AgentError)


def test_models_validation():
    from m365_langchain_agent.models import TestQueryRequest
    req = TestQueryRequest(query="test query")
    assert req.query == "test query"
    assert req.conversation_id == "test-session"


def test_prompts_not_empty():
    from m365_langchain_agent.core.prompts import SYSTEM_PROMPT, STTM_SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) > 100
    assert len(STTM_SYSTEM_PROMPT) > 100
