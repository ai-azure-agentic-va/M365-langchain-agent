"""Entry point: python -m m365_langchain_agent

Creates the FastAPI app and runs it under uvicorn with multi-worker support.
"""

import uvicorn

from m365_langchain_agent.config import settings
from m365_langchain_agent.web.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "m365_langchain_agent.__main__:app",
        host="0.0.0.0",
        port=settings.port,
        workers=settings.workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
