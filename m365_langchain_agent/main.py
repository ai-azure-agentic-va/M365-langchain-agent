"""Entry point — routes to either Chainlit UI or Bot Service based on USER_INTERFACE.

Usage:
    USER_INTERFACE=CHAINLIT_UI  python -m m365_langchain_agent.main   → Chainlit web chat
    USER_INTERFACE=BOT_SERVICE  python -m m365_langchain_agent.main   → FastAPI + Bot Framework
    (default)                   python -m m365_langchain_agent.main   → FastAPI + Bot Framework

Both modes share the same FastAPI app with /health, /readiness, /test/query.
CHAINLIT_UI mounts the chat UI at "/chat" and redirects "/" → "/chat".
BOT_SERVICE exposes /api/messages for Bot Framework.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

USER_INTERFACE = os.environ.get("USER_INTERFACE", "BOT_SERVICE").upper().strip()
DEPLOY_TARGET = os.environ.get("DEPLOY_TARGET", "CONTAINER_APPS").upper().strip()


def main():
    port = int(os.environ.get("PORT", "8000"))
    print(f"[Main] DEPLOY_TARGET={DEPLOY_TARGET}, USER_INTERFACE={USER_INTERFACE}")

    if USER_INTERFACE == "CHAINLIT_UI":
        print(f"[Main] Starting Chainlit UI on port {port} ({DEPLOY_TARGET})")
        from chainlit.utils import mount_chainlit
        from fastapi.responses import RedirectResponse
        from m365_langchain_agent.app import app

        chainlit_target = os.path.join(
            os.path.dirname(__file__), "chainlit_app.py"
        )
        mount_chainlit(app=app, target=chainlit_target, path="/chat")

        # Override root to redirect to Chainlit UI
        # Remove the existing root route and add redirect
        app.routes[:] = [r for r in app.routes if not (hasattr(r, 'path') and r.path == '/')]

        @app.get("/", include_in_schema=False)
        async def root_redirect():
            return RedirectResponse(url="/chat/")

        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=port)

    elif USER_INTERFACE == "BOT_SERVICE":
        print(f"[Main] Starting Bot Service (FastAPI) on port {port} ({DEPLOY_TARGET})")
        import uvicorn
        uvicorn.run(
            "m365_langchain_agent.app:app",
            host="0.0.0.0",
            port=port,
        )

    else:
        print(
            f"[Main] ERROR: Unknown USER_INTERFACE='{USER_INTERFACE}'. "
            f"Use CHAINLIT_UI or BOT_SERVICE."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
