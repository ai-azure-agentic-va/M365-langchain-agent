"""Structured JSON logging with request ID correlation.

Every log line carries a request_id so a single user's request
can be traced across CosmosDB, AI Search, and Azure OpenAI in App Insights.
"""

import json
import logging
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    return request_id_var.get("")


def set_request_id(rid: str | None = None) -> str:
    rid = rid or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid


class StructuredFormatter(logging.Formatter):
    """JSON log formatter with request ID injection."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(""),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO", structured: bool = True) -> None:
    """Configure root logger with structured or plain formatting."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler()
    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s",
            defaults={"request_id": ""},
        ))
    root.addHandler(handler)

    # Silence noisy third-party loggers
    for name in (
        "azure.cosmos._cosmos_http_logging_policy",
        "azure.cosmos.aio._cosmos_client_connection_async",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "httpx",
        "httpcore",
        "engineio.server",
        "engineio.client",
        "socketio.server",
        "socketio.client",
        "chainlit",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
