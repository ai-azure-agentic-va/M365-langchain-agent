"""Centralized configuration — validated at startup, not on first request.

All environment variables are defined here. A missing required variable
crashes the process immediately on import, before any user request arrives.
"""

import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- App ---
    port: int = 8080
    log_level: str = "INFO"
    user_interface: str = "BOT_SERVICE"
    workers: int = 4

    # --- Azure OpenAI (required) ---
    azure_openai_endpoint: str
    azure_openai_deployment_name: str = "gpt-4.1"
    azure_openai_api_version: str = "2024-05-01-preview"
    azure_openai_available_models: str = ""
    azure_openai_embedding_deployment: str = "text-embedding-3-large"
    azure_openai_embedding_dimensions: int = 3072

    # --- Azure AI Search (required) ---
    azure_search_endpoint: str
    azure_search_index_name: str
    azure_search_semantic_config_name: str = ""
    azure_search_embedding_field: str = "content_vector"
    search_exhaustive_knn: bool = False

    # --- CosmosDB (required) ---
    azure_cosmos_endpoint: str
    azure_cosmos_database: str = "m365-langchain-agent"
    azure_cosmos_container: str = "conversations"
    cosmos_ttl_seconds: int = 86400
    cosmos_max_messages: int = 20

    # --- Bot Framework ---
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_auth_tenant: str = ""

    # --- Entra ID SSO ---
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_redirect_uri: str = ""
    session_secret: str = ""
    ai_va_admins_group_id: str = ""
    enable_sso: bool = True
    session_max_age: int = 28800
    session_idle_timeout: int = 900
    chainlit_auth_cookie_name: str = "access_token"

    # --- Key Vault ---
    keyvault_url: str = ""
    entra_client_secret_name: str = ""

    # --- Agent ---
    default_top_k: int = 5
    default_temperature: float = 0.2
    retrieval_score_threshold: float = 1.2
    sttm_top_k: int = 20
    sttm_system_prompt_override: str = ""

    # --- UI ---
    show_chat_settings: bool = True
    show_debug_panels: bool = False
    show_suggested_prompts: bool = True
    show_starter_prompts: bool = True
    starter_prompts: str = ""
    disable_data_layer: bool = False
    chainlit_public_prefix: str = "/public"

    # --- Scaling ---
    llm_request_timeout: int = 60
    search_request_timeout: int = 30

    # --- Foundry (optional — only for registration script) ---
    azure_foundry_endpoint: str = ""
    azure_foundry_subscription_id: str = ""
    azure_foundry_resource_group: str = ""
    azure_foundry_workspace: str = ""
    azure_foundry_search_connection: str = "aisearch-connection"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def session_cookie_secure(self) -> bool:
        return self.entra_redirect_uri.startswith("https://")

    @property
    def available_models_list(self) -> list[str]:
        if self.azure_openai_available_models.strip():
            return [m.strip() for m in self.azure_openai_available_models.split(",") if m.strip()]
        defaults = [self.azure_openai_deployment_name]
        for m in ["gpt-4.1", "gpt-4.1-mini", "o3-mini"]:
            if m not in defaults:
                defaults.append(m)
        return defaults


settings = Settings()

# Shared credential — single instance across all Azure SDK clients
credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
