"""Azure Key Vault integration with fallback to hardcoded secrets.

Provides secure secret management by attempting to fetch secrets from Azure Key Vault
first, with fallback to environment variables if Key Vault is unavailable.

Environment variables:
    KEYVAULT_URL: Azure Key Vault URL (e.g., https://kv-name.vault.azure.net/)
    ENTRA_CLIENT_SECRET_NAME: Name of the secret in Key Vault
    ENTRA_CLIENT_SECRET: Fallback hardcoded secret value
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_secret_cache = {}


def get_secret_with_fallback(
    secret_name_env: str,
    fallback_env: str,
    vault_url: Optional[str] = None
) -> str:
    """Fetch secret from Key Vault with fallback to environment variable.

    Attempts to retrieve the secret from Azure Key Vault. If Key Vault is unavailable
    or the secret is not found, falls back to the hardcoded value from environment.

    Args:
        secret_name_env: Environment variable containing the Key Vault secret name
        fallback_env: Environment variable containing the fallback hardcoded secret
        vault_url: Optional Key Vault URL (defaults to KEYVAULT_URL env var)

    Returns:
        The secret value (from Key Vault or fallback)

    Raises:
        ValueError: If neither Key Vault nor fallback secret is available
    """
    # Check cache first
    cache_key = f"{secret_name_env}:{fallback_env}"
    if cache_key in _secret_cache:
        return _secret_cache[cache_key]

    # Get configuration
    vault_url = vault_url or os.environ.get("KEYVAULT_URL", "")
    secret_name = os.environ.get(secret_name_env, "")
    fallback_secret = os.environ.get(fallback_env, "")

    # Try Key Vault first if configured
    if vault_url and secret_name:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_url, credential=credential)

            logger.info(f"[KeyVault] Attempting to fetch secret '{secret_name}' from {vault_url}")
            secret = client.get_secret(secret_name)

            if secret and secret.value:
                logger.info(f"[KeyVault] Successfully retrieved secret '{secret_name}' from Key Vault")
                _secret_cache[cache_key] = secret.value
                return secret.value
            else:
                logger.warning(f"[KeyVault] Secret '{secret_name}' found but has no value")

        except Exception as e:
            logger.warning(
                f"[KeyVault] Failed to retrieve secret '{secret_name}' from Key Vault: {e}. "
                "Falling back to hardcoded secret."
            )
    else:
        if not vault_url:
            logger.info(f"[KeyVault] KEYVAULT_URL not configured, using hardcoded secret")
        if not secret_name:
            logger.info(f"[KeyVault] {secret_name_env} not configured, using hardcoded secret")

    # Fallback to hardcoded secret
    if fallback_secret:
        logger.info(f"[KeyVault] Using fallback secret from {fallback_env}")
        _secret_cache[cache_key] = fallback_secret
        return fallback_secret

    raise ValueError(
        f"No secret available: Key Vault failed and {fallback_env} environment variable is not set"
    )


def get_entra_client_secret() -> str:
    """Get Entra ID client secret from Key Vault or fallback.

    Returns:
        Entra ID client secret
    """
    return get_secret_with_fallback(
        secret_name_env="ENTRA_CLIENT_SECRET_NAME",
        fallback_env="ENTRA_CLIENT_SECRET"
    )


def clear_cache():
    """Clear the secret cache. Useful for testing or forcing refresh."""
    global _secret_cache
    _secret_cache = {}
