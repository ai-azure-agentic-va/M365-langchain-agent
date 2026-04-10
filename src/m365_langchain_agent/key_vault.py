"""Azure Key Vault integration — runtime secret fetching with fallback.

Config-level secrets (entra_client_secret, bot_app_password, session_secret)
are resolved automatically at startup via Settings._resolve_secrets_from_keyvault().

This module provides get_secret() for any additional runtime secret lookups
that modules need beyond what's in config (e.g., API keys for new integrations).
"""

import logging

from azure.keyvault.secrets import SecretClient

from m365_langchain_agent.config import settings, credential

logger = logging.getLogger(__name__)

_client: SecretClient | None = None
_cache: dict[str, str] = {}


def _get_client() -> SecretClient | None:
    """Lazy-init Key Vault client. Returns None if keyvault_url is not set."""
    global _client
    if _client is None and settings.keyvault_url:
        _client = SecretClient(vault_url=settings.keyvault_url, credential=credential)
    return _client


def get_secret(secret_name: str, fallback: str = "") -> str:
    """Fetch a secret from Key Vault with env-var fallback.

    Args:
        secret_name: The secret name in Key Vault.
        fallback: Value to return if Key Vault is unavailable or not configured.

    Returns:
        The secret value, or fallback.

    Raises:
        ValueError: If neither Key Vault nor fallback provides a value
            and fallback is empty string.
    """
    if secret_name in _cache:
        return _cache[secret_name]

    client = _get_client()
    if client:
        try:
            secret = client.get_secret(secret_name)
            if secret and secret.value:
                logger.info("Retrieved secret '%s' from Key Vault", secret_name)
                _cache[secret_name] = secret.value
                return secret.value
        except Exception as e:
            logger.warning("Key Vault lookup failed for '%s': %s", secret_name, e)

    if fallback:
        _cache[secret_name] = fallback
        return fallback

    raise ValueError(
        f"No secret available: Key Vault failed for '{secret_name}' and no fallback provided"
    )


def get_entra_client_secret() -> str:
    """Get Entra ID client secret — Key Vault with env var fallback.

    Prefer settings.entra_client_secret which is already resolved from
    Key Vault at startup. This function exists for backward compatibility.
    """
    if settings.entra_client_secret:
        return settings.entra_client_secret
    return get_secret(
        settings.entra_client_secret_name,
        fallback=settings.entra_client_secret,
    )


def clear_cache() -> None:
    """Clear the secret cache. Useful for testing or forcing refresh."""
    _cache.clear()
