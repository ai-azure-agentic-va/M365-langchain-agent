"""Centralized configuration — validated at startup, not on first request.

Sources: Key Vault (secrets) → env vars / .env → defaults.
Missing required vars crash on import, before any request arrives.
"""

import logging

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from pydantic import model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

# (kv_secret_name_attr, target_attr) — resolved from Key Vault when keyvault_url is set
_KV_SECRET_MAP: list[tuple[str, str]] = [
    ("entra_client_secret_name", "entra_client_secret"),
    ("bot_app_password_name", "bot_app_password"),
    ("session_secret_name", "session_secret"),
]


def _resolve_from_keyvault(vault_url: str, secret_name: str) -> str | None:
    try:
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=vault_url, credential=credential)
        secret = client.get_secret(secret_name)
        if secret and secret.value:
            logger.info("Resolved secret '%s' from Key Vault", secret_name)
            return secret.value
        logger.warning("Secret '%s' exists in Key Vault but has no value", secret_name)
    except Exception as e:
        logger.warning("Key Vault lookup failed for '%s': %s", secret_name, e)
    return None


class Settings(BaseSettings):

    # App
    port: int = 8080
    log_level: str = "INFO"
    user_interface: str = "BOT_SERVICE"
    workers: int = 4

    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_deployment_name: str = "gpt-4.1"
    azure_openai_api_version: str = "2024-05-01-preview"
    azure_openai_available_models: str = ""
    azure_openai_embedding_deployment: str = "text-embedding-3-large"
    azure_openai_embedding_dimensions: int = 3072

    # Azure AI Search
    azure_search_endpoint: str
    azure_search_index_name: str
    azure_search_semantic_config_name: str = "custom-kb-semantic-config"
    azure_search_embedding_field: str = "content_vector"
    search_exhaustive_knn: bool = False

    # CosmosDB
    azure_cosmos_endpoint: str
    azure_cosmos_database: str = "m365-langchain-agent"
    azure_cosmos_container: str = "conversations"
    cosmos_ttl_seconds: int = 86400
    cosmos_max_messages: int = 20

    # Bot Framework
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_auth_tenant: str = ""

    # Entra ID SSO
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

    # Key Vault — *_name fields reference secret names in Key Vault
    keyvault_url: str = ""
    entra_client_secret_name: str = ""
    bot_app_password_name: str = ""
    session_secret_name: str = ""

    # Agent
    default_top_k: int = 8
    default_temperature: float = 0.2
    retrieval_score_threshold: float = 1.2
    sttm_top_k: int = 20
    system_prompt_override: str = ""
    sttm_system_prompt_override: str = ""
    suggested_prompts_prompt_override: str = ""
    query_rewrite_prompt_override: str = ""
    query_refine_prompt_override: str = ""
    out_of_scope_answer_override: str = ""

    # UI
    app_display_name: str = "ETS VA Assistant"
    show_chat_settings: bool = True
    show_debug_panels: bool = False
    show_suggested_prompts: bool = True
    show_starter_prompts: bool = True
    starter_prompts: str = ""
    disable_data_layer: bool = False
    chainlit_public_prefix: str = "/chat/public"
    greeting_words: str = "hello,hi,hey,greetings,good morning,good afternoon,good evening,howdy,hola"
    greeting_response: str = "Hello! I'm the **ETS Virtual Assistant**. How can I help you today?"
    thanks_words: str = "thank you,thanks,thankyou,ty,thx"
    thanks_response: str = "You're welcome! If you have any other questions, feel free to ask."

    # Test endpoint
    test_query_token: str = ""

    # Scaling
    llm_request_timeout: int = 60
    search_request_timeout: int = 30

    # Foundry (optional — registration script only)
    azure_foundry_endpoint: str = ""
    azure_foundry_subscription_id: str = ""
    azure_foundry_resource_group: str = ""
    azure_foundry_workspace: str = ""
    azure_foundry_search_connection: str = "aisearch-connection"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def _resolve_secrets_from_keyvault(self) -> "Settings":
        if not self.keyvault_url:
            return self

        logger.info("Key Vault configured (%s) — resolving secrets", self.keyvault_url)
        for kv_name_attr, target_attr in _KV_SECRET_MAP:
            secret_name = getattr(self, kv_name_attr, "")
            if not secret_name:
                continue
            value = _resolve_from_keyvault(self.keyvault_url, secret_name)
            if value:
                object.__setattr__(self, target_attr, value)

        return self

    @property
    def session_cookie_secure(self) -> bool:
        return self.entra_redirect_uri.startswith("https://")

    @property
    def available_models_list(self) -> list[str]:
        if self.azure_openai_available_models.strip():
            return [m.strip() for m in self.azure_openai_available_models.split(",") if m.strip()]
        return [self.azure_openai_deployment_name]


settings = Settings()
