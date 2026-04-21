"""Azure Key Vault integration — runtime secret fetching with env-var fallback."""

import logging

from azure.keyvault.secrets import SecretClient

from m365_langchain_agent.config import settings, credential

logger = logging.getLogger(__name__)

_client: SecretClient | None = None
_cache: dict[str, str] = {}


def _get_client() -> SecretClient | None:
    global _client
    if _client is None and settings.keyvault_url:
        _client = SecretClient(vault_url=settings.keyvault_url, credential=credential)
    return _client


def get_secret(secret_name: str, fallback: str = "") -> str:
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
    # Prefer settings.entra_client_secret — already resolved from Key Vault at startup
    if settings.entra_client_secret:
        return settings.entra_client_secret
    return get_secret(
        settings.entra_client_secret_name,
        fallback=settings.entra_client_secret,
    )


def clear_cache() -> None:
    _cache.clear()
