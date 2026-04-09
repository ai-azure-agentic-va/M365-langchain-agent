"""Azure Key Vault integration with fallback to environment variables.

Fetches secrets from Azure Key Vault first, falling back to direct
environment variable values if Key Vault is unavailable.
"""

import logging

from azure.keyvault.secrets import SecretClient

from m365_langchain_agent.config import settings, credential

logger = logging.getLogger(__name__)

_secret_cache: dict[str, str] = {}


def get_secret_with_fallback(
    secret_name_env: str,
    fallback_env: str,
    vault_url: str | None = None,
) -> str:
    """Fetch secret from Key Vault with fallback to environment variable.

    Args:
        secret_name_env: Attribute name on settings for the Key Vault secret name.
        fallback_env: Attribute name on settings for the fallback value.
        vault_url: Optional Key Vault URL override.

    Returns:
        The secret value (from Key Vault or fallback).

    Raises:
        ValueError: If neither Key Vault nor fallback secret is available.
    """
    cache_key = f"{secret_name_env}:{fallback_env}"
    if cache_key in _secret_cache:
        return _secret_cache[cache_key]

    vault_url = vault_url or settings.keyvault_url
    secret_name = getattr(settings, secret_name_env, "")
    fallback_secret = getattr(settings, fallback_env, "")

    if vault_url and secret_name:
        try:
            client = SecretClient(vault_url=vault_url, credential=credential)
            logger.info("Fetching secret '%s' from Key Vault", secret_name)
            secret = client.get_secret(secret_name)

            if secret and secret.value:
                logger.info("Retrieved secret '%s' from Key Vault", secret_name)
                _secret_cache[cache_key] = secret.value
                return secret.value
            else:
                logger.warning("Secret '%s' found but has no value", secret_name)
        except Exception as e:
            logger.warning(
                "Failed to retrieve '%s' from Key Vault: %s. Using fallback.",
                secret_name, e,
            )

    if fallback_secret:
        _secret_cache[cache_key] = fallback_secret
        return fallback_secret

    raise ValueError(
        f"No secret available: Key Vault failed and {fallback_env} is not set"
    )


def get_entra_client_secret() -> str:
    """Get Entra ID client secret from Key Vault or fallback."""
    return get_secret_with_fallback(
        secret_name_env="entra_client_secret_name",
        fallback_env="entra_client_secret",
    )


def clear_cache() -> None:
    """Clear the secret cache. Useful for testing or forcing refresh."""
    _secret_cache.clear()
