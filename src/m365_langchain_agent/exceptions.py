"""Typed exception hierarchy for structured error handling and alerting."""


class AgentError(Exception):
    """Base exception for all agent errors."""


class RetrievalError(AgentError):
    """Azure AI Search query failed."""


class GenerationError(AgentError):
    """LLM call failed."""


class AuthenticationError(AgentError):
    """SSO or Bot Framework authentication failed."""


class ConfigurationError(AgentError):
    """Missing or invalid configuration."""


class CosmosError(AgentError):
    """CosmosDB operation failed."""
