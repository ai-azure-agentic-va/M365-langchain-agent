"""Exception hierarchy for the agent."""


class AgentError(Exception):
    pass


class RetrievalError(AgentError):
    pass


class GenerationError(AgentError):
    pass


class AuthenticationError(AgentError):
    pass


class ConfigurationError(AgentError):
    pass


class CosmosError(AgentError):
    pass
