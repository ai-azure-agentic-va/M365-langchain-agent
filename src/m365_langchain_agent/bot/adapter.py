"""Bot Framework adapter with Managed Identity support.

botbuilder-python SDK doesn't support UserAssignedMSI — this module
overrides token acquisition to use ManagedIdentityCredential.
"""

import logging

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botframework.connector.auth import AppCredentials, MicrosoftAppCredentials

from m365_langchain_agent.config import settings

logger = logging.getLogger(__name__)


class MsiAppCredentials(AppCredentials):

    def __init__(self, app_id: str, tenant_id: str | None = None):
        super().__init__(app_id=app_id, channel_auth_tenant=tenant_id)
        from azure.identity import ManagedIdentityCredential

        self._msi = ManagedIdentityCredential(client_id=app_id)
        logger.info("MsiAppCredentials: app_id=%s", app_id)

    def get_access_token(self, force_refresh: bool = False) -> str:
        token = self._msi.get_token("https://api.botframework.com/.default")
        self.token = {"access_token": token.token, "token_type": "Bearer"}
        return token.token


class MsiBotFrameworkAdapter(BotFrameworkAdapter):
    # Monkey-patches __get_app_credentials (name-mangled) to use MSI instead of client secret

    def __init__(self, adapter_settings: BotFrameworkAdapterSettings):
        super().__init__(adapter_settings)
        self._BotFrameworkAdapter__get_app_credentials = self._msi_get_app_credentials

    async def _msi_get_app_credentials(self, app_id, scope, force=False):
        if not app_id:
            return MicrosoftAppCredentials.empty()

        cache_key = f"{app_id}{scope}"
        if cache_key in self._app_credential_map and not force:
            return self._app_credential_map[cache_key]

        credentials = MsiAppCredentials(
            app_id=app_id,
            tenant_id=self.settings.channel_auth_tenant,
        )
        self._app_credential_map[cache_key] = credentials
        return credentials


def create_adapter() -> BotFrameworkAdapter:
    adapter_settings = BotFrameworkAdapterSettings(
        app_id=settings.bot_app_id,
        app_password=settings.bot_app_password,
        channel_auth_tenant=settings.bot_auth_tenant or None,
    )

    if settings.bot_app_id and not settings.bot_app_password:
        logger.info("UserAssignedMSI mode — using MsiBotFrameworkAdapter")
        adapter = MsiBotFrameworkAdapter(adapter_settings)
    else:
        logger.info("Standard mode — using BotFrameworkAdapter")
        adapter = BotFrameworkAdapter(adapter_settings)

    async def on_error(context, error):
        logger.error("Adapter unhandled error: %s", error, exc_info=True)
        try:
            await context.send_activity("Sorry, something went wrong. Please try again.")
        except Exception:
            logger.warning("Could not send error message back to user")

    adapter.on_turn_error = on_error
    return adapter
