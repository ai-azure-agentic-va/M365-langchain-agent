"""Azure Key Vault secrets manager for ENTRA_CLIENT_SECRET.

This module fetches the Entra ID client secret from Azure Key Vault
instead of hardcoding it in environment variables.

Environment variables required:
    KEY_VAULT_URL: Key Vault URL (e.g., https://kv-nfcu-ai-foundry.vault.azure.net/)
    ENTRA_CLIENT_SECRET_NAME: Secret name in Key Vault (e.g., ETSVA-ContainerApp-ETSVA-DEV)
    USE_KEY_VAULT: Set to "true" to enable Key Vault (default: false, uses env vars)

For local development (USE_KEY_VAULT=false):
    Falls back to reading ENTRA_CLIENT_SECRET from environment variables (.env file)

For production (USE_KEY_VAULT=true):
    Fetches ENTRA_CLIENT_SECRET from Azure Key Vault using Managed Identity authentication
"""

import logging
import os
from typing import Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

# Key Vault configuration
KEY_VAULT_URL = os.environ.get("KEY_VAULT_URL", "")
ENTRA_CLIENT_SECRET_NAME = os.environ.get("ENTRA_CLIENT_SECRET_NAME", "")
USE_KEY_VAULT = os.environ.get("USE_KEY_VAULT", "false").lower() == "true"

# Lazy-initialized Key Vault client
_keyvault_client = None


def get_keyvault_client():
    """Get or create the Azure Key Vault SecretClient.

    Uses DefaultAzureCredential which automatically detects:
    - Managed Identity in Azure (AKS, App Service, Container Apps, etc.)
    - Azure CLI credentials for local development
    - Environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)

    Returns:
        SecretClient instance

    Raises:
        ValueError: If Key Vault URL is not configured
        ImportError: If azure-keyvault-secrets or azure-identity not installed
    """
    global _keyvault_client

    if _keyvault_client is None:
        if not KEY_VAULT_URL:
            raise ValueError(
                "Azure Key Vault URL not configured. Set KEY_VAULT_URL environment variable."
            )

        try:
            from azure.keyvault.secrets import SecretClient
            from azure.identity import DefaultAzureCredential
        except ImportError as e:
            raise ImportError(
                "Azure Key Vault SDK not installed. "
                "Install with: pip install azure-keyvault-secrets azure-identity"
            ) from e

        # DefaultAzureCredential tries multiple authentication methods in order:
        # 1. Environment variables (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET)
        # 2. Managed Identity (in Azure: AKS, App Service, Container Apps, VM, etc.)
        # 3. Azure CLI (for local development)
        # 4. Visual Studio Code
        # 5. Azure PowerShell
        credential = DefaultAzureCredential()

        _keyvault_client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
        logger.info(f"[SecretsManager] Azure Key Vault client initialized: {KEY_VAULT_URL}")

    return _keyvault_client


@lru_cache(maxsize=1)
def get_entra_client_secret() -> str:
    """Get Entra ID client secret from Azure Key Vault or environment variables.

    If USE_KEY_VAULT=true:
        Fetches the secret from Azure Key Vault using ENTRA_CLIENT_SECRET_NAME
    Otherwise:
        Falls back to reading ENTRA_CLIENT_SECRET from environment variables (for local development)

    The result is cached in memory to minimize Key Vault API calls.

    Returns:
        Entra client secret as string

    Example:
        # Production mode (USE_KEY_VAULT=true, KEY_VAULT_URL and ENTRA_CLIENT_SECRET_NAME set)
        client_secret = get_entra_client_secret()
        # Fetches from Key Vault: https://kv-nfcu-ai-foundry.vault.azure.net/secrets/ETSVA-ContainerApp-ETSVA-DEV

        # Local dev mode (USE_KEY_VAULT=false)
        client_secret = get_entra_client_secret()
        # Falls back to: os.environ.get("ENTRA_CLIENT_SECRET")
    """
    if not USE_KEY_VAULT:
        # Local development mode: read from environment variables
        value = os.environ.get("ENTRA_CLIENT_SECRET", "")
        if value:
            logger.debug("[SecretsManager] Using ENTRA_CLIENT_SECRET from environment variables")
        else:
            logger.warning("[SecretsManager] ENTRA_CLIENT_SECRET not found in environment variables")
        return value

    # Production mode: fetch from Azure Key Vault
    if not ENTRA_CLIENT_SECRET_NAME:
        logger.error("[SecretsManager] ENTRA_CLIENT_SECRET_NAME not configured. Cannot fetch from Key Vault.")
        return ""

    try:
        client = get_keyvault_client()
        secret = client.get_secret(ENTRA_CLIENT_SECRET_NAME)
        logger.info(f"[SecretsManager] Retrieved ENTRA_CLIENT_SECRET from Key Vault: {ENTRA_CLIENT_SECRET_NAME}")
        return secret.value

    except Exception as e:
        logger.error(
            f"[SecretsManager] Failed to retrieve ENTRA_CLIENT_SECRET from Key Vault "
            f"(secret name: {ENTRA_CLIENT_SECRET_NAME}): {e}",
            exc_info=True
        )
        # Fallback to environment variable even in Key Vault mode if fetch fails
        fallback_value = os.environ.get("ENTRA_CLIENT_SECRET", "")
        if fallback_value:
            logger.warning("[SecretsManager] Falling back to ENTRA_CLIENT_SECRET from environment variables")
        return fallback_value


def clear_cache():
    """Clear the in-memory secret cache.

    Use this if you need to force a refresh from Key Vault
    (e.g., after rotating the Entra client secret).
    """
    get_entra_client_secret.cache_clear()
    logger.info("[SecretsManager] Secret cache cleared")


# Example usage and testing
if __name__ == "__main__":
    # Configure logging for testing
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    print("=" * 60)
    print("ENTRA CLIENT SECRET - Key Vault Integration Test")
    print("=" * 60)
    print(f"USE_KEY_VAULT: {USE_KEY_VAULT}")
    print(f"KEY_VAULT_URL: {KEY_VAULT_URL or 'Not configured'}")
    print(f"ENTRA_CLIENT_SECRET_NAME: {ENTRA_CLIENT_SECRET_NAME or 'Not configured'}")
    print()

    # Test secret retrieval
    print("Fetching ENTRA_CLIENT_SECRET...")
    client_secret = get_entra_client_secret()

    if client_secret:
        print(f"✓ ENTRA_CLIENT_SECRET retrieved successfully")
        print(f"  Length: {len(client_secret)} characters")
        print(f"  Preview: {client_secret[:4]}{'*' * 20}{client_secret[-4:] if len(client_secret) > 8 else ''}")
    else:
        print("✗ ENTRA_CLIENT_SECRET not found")

    print()
    print("=" * 60)
    print("Test complete")
    print("=" * 60)
